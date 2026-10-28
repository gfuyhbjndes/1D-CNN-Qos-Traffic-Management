#!/usr/bin/env python3
import time
import os
from mininet.topo import Topo
from mininet.net import Mininet
from mininet.node import RemoteController, OVSKernelSwitch
from mininet.cli import CLI
from mininet.log import setLogLevel, info

class DumbbellTopo(Topo):
    def build(self):
        # Two switches: s1 (Client Edge) and s2 (Server Edge)
        s1 = self.addSwitch('s1', protocols='OpenFlow13')
        s2 = self.addSwitch('s2', protocols='OpenFlow13')

        # Client Hosts on Left Edge (s1)
        h1 = self.addHost('h1', ip='10.0.0.1/24', mac='00:00:00:00:00:01') # VoIP Client
        h2 = self.addHost('h2', ip='10.0.0.2/24', mac='00:00:00:00:00:02') # Video Client
        h3 = self.addHost('h3', ip='10.0.0.3/24', mac='00:00:00:00:00:03') # Bulk Client

        # Server Hosts on Right Edge (s2)
        h4 = self.addHost('h4', ip='10.0.0.4/24', mac='00:00:00:00:00:04') # VoIP Server
        h5 = self.addHost('h5', ip='10.0.0.5/24', mac='00:00:00:00:00:05') # Video Server
        h6 = self.addHost('h6', ip='10.0.0.6/24', mac='00:00:00:00:00:06') # Bulk Server

        # Ports 1, 2, 3 on s1 and s2 connect to hosts
        self.addLink(h1, s1, port1=1, port2=1)
        self.addLink(h2, s1, port1=1, port2=2)
        self.addLink(h3, s1, port1=1, port2=3)

        self.addLink(h4, s2, port1=1, port2=1)
        self.addLink(h5, s2, port1=1, port2=2)
        self.addLink(h6, s2, port1=1, port2=3)

        # Port 4 on s1 and s2 is the shared Inter-Switch Trunk Link!
        self.addLink(s1, s2, port1=4, port2=4)

def configure_queues(net):
    info("[+] Disabling TCP offloading on hosts to prevent HTB drops...\n")
    for h in net.hosts:
        intf = h.defaultIntf()
        h.cmd(f'ethtool -K {intf} tso off gso off gro off')

    info("[+] Configuring Hardware QoS Queues on Backbone Link (s1-eth4 & s2-eth4)...\n")
    # Apply strict HTB queues across the inter-switch trunk ports (s1-eth4 and s2-eth4)
    # Total Trunk Capacity: 50 Mbps
    # Queue 0 (VoIP): Guaranteed 15 Mbps, Max 50 Mbps (Priority 0 - Highest)
    # Queue 1 (Streaming): Guaranteed 20 Mbps, Max 40 Mbps (Priority 1)
    # Queue 2 (Standard): Guaranteed 10 Mbps, Max 25 Mbps (Priority 2)
    # Queue 3 (Bulk/P2P): Guaranteed 1 Mbps, Capped at 5 Mbps (Priority 3 - Throttled!)
    cmd = """
    sudo ovs-vsctl -- \
      set port s1-eth4 qos=@newqos -- \
      set port s2-eth4 qos=@newqos -- \
      --id=@newqos create qos type=linux-htb other-config:max-rate=50000000 queues=0=@q0,1=@q1,2=@q2,3=@q3 -- \
      --id=@q0 create queue other-config:min-rate=15000000 other-config:max-rate=50000000 other-config:priority=0 -- \
      --id=@q1 create queue other-config:min-rate=20000000 other-config:max-rate=40000000 other-config:priority=1 -- \
      --id=@q2 create queue other-config:min-rate=10000000 other-config:max-rate=25000000 other-config:priority=2 -- \
      --id=@q3 create queue other-config:min-rate=1000000 other-config:max-rate=5000000 other-config:priority=3
    """
    os.system(cmd)
    info("[+] Backbone QoS Queues Ready!\n")

if __name__ == '__main__':
    setLogLevel('info')
    topo = DumbbellTopo()
    net = Mininet(topo=topo, switch=OVSKernelSwitch, build=False, autoSetMacs=True)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    net.build()
    net.start()
    
    time.sleep(2)
    configure_queues(net)
    
    info("\n*** Dumbbell Network Ready! Use traffic_gen.py on hosts to test.\n")
    CLI(net)
    net.stop()