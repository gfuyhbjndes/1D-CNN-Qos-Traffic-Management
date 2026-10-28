import collections
import collections.abc
collections.MutableMapping = collections.abc.MutableMapping

import os
import time
import joblib
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, tcp, udp, ether_types

# 1. Exact 1D CNN Architecture
class cnn1d(nn.Module):
    def __init__(self, num_classes=4):
        super(cnn1d, self).__init__()
        self.conv1 = nn.Conv1d(1, 32, 3, padding=1)
        self.bn1 = nn.BatchNorm1d(32)
        self.conv2 = nn.Conv1d(32, 64, 3, padding=1)
        self.bn2 = nn.BatchNorm1d(64)
        self.pool = nn.MaxPool1d(2)
        self.dropout = nn.Dropout(0.3)
        self.fc1 = nn.Linear(64 * 5, 128)
        self.fc2 = nn.Linear(128, num_classes)

    def forward(self, x):
        x = self.pool(F.relu(self.bn1(self.conv1(x))))
        x = self.pool(F.relu(self.bn2(self.conv2(x))))
        x = x.view(x.size(0), -1)
        x = self.dropout(F.relu(self.fc1(x)))
        return self.fc2(x)

QUEUE_NAMES = {
    0: "Queue 0 [CRITICAL - VoIP]",
    1: "Queue 1 [HIGH - Streaming]",
    2: "Queue 2 [STANDARD - Web]",
    3: "Queue 3 [LOW - Bulk Throttled]"
}

class MLQoSController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    WINDOW_SIZE = 10  # Collect 10 packets before classifying

    def __init__(self, *args, **kwargs):
        super(MLQoSController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.flow_windows = {}  # Tracks {(src_ip, dst_ip, src_port, dst_port, proto): {'times': [], 'sizes': []}}

        # Load PyTorch 1D CNN
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = cnn1d(num_classes=4).to(self.device)
        self.model.load_state_dict(torch.load('models/cnn1d.pth', map_location=self.device))
        self.model.eval()
        self.logger.info("[+] 1D CNN Model Loaded Successfully!")

        # Load Scaler if available
        self.scaler = None
        if os.path.exists('models/scaler.pkl'):
            self.scaler = joblib.load('models/scaler.pkl')
            self.logger.info("[+] StandardScaler Loaded Successfully!")
        else:
            self.logger.warning("[!] models/scaler.pkl not found! Using heuristic fallback normalization.")

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self.logger.info(f"[+] Switch s{datapath.id} connected and table-miss installed.")

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                idle_timeout=idle_timeout, match=match, instructions=inst)
        datapath.send_msg(mod)

    def extract_23_features(self, times, sizes):
        """Computes 23 statistical flow metrics in CIC-VPN2016 format (microseconds)."""
        times_us = np.array(times) * 1e6
        sizes_arr = np.array(sizes, dtype=np.float32)
        
        duration = max(times_us[-1] - times_us[0], 1.0)
        iats = np.diff(times_us) if len(times_us) > 1 else np.array([1.0])
        
        total_fiat = np.sum(iats)
        min_fiat = np.min(iats)
        max_fiat = np.max(iats)
        mean_fiat = np.mean(iats)
        std_fiat = np.std(iats)
        
        flow_pkts_s = (len(sizes_arr) / duration) * 1e6
        flow_bytes_s = (np.sum(sizes_arr) / duration) * 1e6
        
        min_flowiat = min_fiat
        max_flowiat = max_fiat
        mean_flowiat = mean_fiat
        std_flowiat = std_fiat

        # Assemble 23-feature vector matching CIC-VPN2016 statistical distributions
        raw_features = np.array([
            duration,
            total_fiat,
            0.0,
            min_fiat,
            0.0,
            max_fiat,
            0.0,
            mean_fiat,
            0.0,
            flow_pkts_s,
            flow_bytes_s,
            min_flowiat,
            max_flowiat,
            mean_flowiat,
            std_flowiat,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
            0.0
        ], dtype=np.float32).reshape(1, -1)

        if self.scaler is not None:
            scaled = self.scaler.transform(raw_features)
        else:
            scaled = (raw_features - np.mean(raw_features)) / (np.std(raw_features) + 1e-5)

        return torch.tensor(scaled, dtype=torch.float32).unsqueeze(1).to(self.device), mean_fiat, np.mean(sizes_arr)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']
        dpid = datapath.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP or eth.ethertype == 0x86dd:
            return

        # Learn MAC address for clean L2 switching across multi-switch topology
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port
        out_port = self.mac_to_port[dpid].get(eth.dst, ofproto.OFPP_FLOOD)

        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if not ip_pkt:
            # Handle ARP or non-IP cleanly
            actions = [parser.OFPActionOutput(out_port)]
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions,
                                      data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None)
            datapath.send_msg(out)
            return

        # Extract 5-Tuple for IPv4 TCP/UDP
        src_ip, dst_ip, proto = ip_pkt.src, ip_pkt.dst, ip_pkt.proto
        src_port, dst_port = 0, 0
        if proto == 6:  # TCP
            t = pkt.get_protocol(tcp.tcp)
            src_port, dst_port = t.src_port, t.dst_port
        elif proto == 17:  # UDP
            u = pkt.get_protocol(udp.udp)
            src_port, dst_port = u.src_port, u.dst_port
        else:
            # ICMP (Ping) -> Fast path without waiting 10 packets
            actions = [parser.OFPActionOutput(out_port)]
            if out_port != ofproto.OFPP_FLOOD:
                match = parser.OFPMatch(eth_type=0x0800, ipv4_src=src_ip, ipv4_dst=dst_ip, ip_proto=proto)
                self.add_flow(datapath, 5, match, actions, idle_timeout=30)
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions,
                                      data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None)
            datapath.send_msg(out)
            return

        # Track 10-packet window per switch & 5-tuple
        flow_key = (dpid, src_ip, dst_ip, src_port, dst_port, proto)
        if flow_key not in self.flow_windows:
            self.flow_windows[flow_key] = {'times': [], 'sizes': []}

        window = self.flow_windows[flow_key]
        window['times'].append(time.time())
        window['sizes'].append(len(msg.data))

        # If we have fewer than 10 packets, forward current packet and keep observing
        if len(window['times']) < self.WINDOW_SIZE:
            actions = [parser.OFPActionOutput(out_port)]
            out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                      in_port=in_port, actions=actions,
                                      data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None)
            datapath.send_msg(out)
            return

        # --- PACKET #10 REACHED: RUN 1D CNN INFERENCE ---
        features_tensor, mean_iat_us, mean_pkt_size = self.extract_23_features(window['times'], window['sizes'])
        del self.flow_windows[flow_key]  # Clear memory once classified

        with torch.no_grad():
            outputs = self.model(features_tensor)
            _, predicted = torch.max(outputs.data, 1)
            queue_id = predicted.item()

        self.logger.info(
            f"\n[Switch s{dpid}] Flow Classified after {self.WINDOW_SIZE} pkts: {src_ip}:{src_port} -> {dst_ip}:{dst_port}\n"
            f"   ├─ Observed Stats : Mean Pkt Size = {mean_pkt_size:.0f} B | Mean IAT = {mean_iat_us/1000:.2f} ms\n"
            f"   └─ 1D CNN Decision: {QUEUE_NAMES[queue_id]} (Egress Port {out_port})"
        )

        # Install 5-Tuple Hardware Flow Rule with QoS Queue
        actions = [parser.OFPActionSetQueue(queue_id), parser.OFPActionOutput(out_port)]
        if proto == 17:
            match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_src=src_ip, ipv4_dst=dst_ip,
                                    udp_src=src_port, udp_dst=dst_port)
        else:
            match = parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=src_ip, ipv4_dst=dst_ip,
                                    tcp_src=src_port, tcp_dst=dst_port)

        if out_port != ofproto.OFPP_FLOOD:
            self.add_flow(datapath, 20, match, actions, idle_timeout=60)

        # Forward the 10th packet
        out = parser.OFPPacketOut(datapath=datapath, buffer_id=msg.buffer_id,
                                  in_port=in_port, actions=actions,
                                  data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None)
        datapath.send_msg(out)