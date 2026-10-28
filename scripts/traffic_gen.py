#!/usr/bin/env python3
import socket
import time
import argparse
import random

PROFILES = {
    'voip':      {'size_range': (60, 80),     'iat_range': (0.018, 0.022), 'desc': 'VoIP (Tiny packets, steady 20ms spacing)'},
    'streaming': {'size_range': (550, 850),   'iat_range': (0.003, 0.007), 'desc': 'Video Stream (Medium packets, high rate)'},
    'web':       {'size_range': (200, 700),   'iat_range': (0.010, 0.050), 'desc': 'Web/Interactive (Variable bursts)'},
    'bulk':      {'size_range': (1380, 1450), 'iat_range': (0.0001, 0.0005), 'desc': 'Bulk File Transfer (Max MTU, near-zero IAT)'}
}

def run_server(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', port))
    print(f"[*] Server listening on UDP port {port}...")
    
    total_bytes = 0
    start_time = None
    last_report = time.time()
    
    while True:
        data, addr = sock.recvfrom(2048)
        now = time.time()
        if start_time is None:
            start_time = now
            last_report = now
        total_bytes += len(data)
        
        if now - last_report >= 1.0:
            mbps = (total_bytes * 8) / ((now - last_report) * 1_000_000)
            print(f"[{time.strftime('%H:%M:%S')}] Incoming from {addr[0]}:{addr[1]} -> Throughput: {mbps:.3f} Mbps")
            total_bytes = 0
            last_report = now

def run_client(target_ip, port, profile_name, duration):
    profile = PROFILES[profile_name]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[+] Starting '{profile_name.upper()}' flow -> {target_ip}:{port} for {duration}s")
    print(f"    Behavior: {profile['desc']}")
    
    end_time = time.time() + duration
    pkts_sent = 0
    bytes_sent = 0
    
    while time.time() < end_time:
        size = random.randint(*profile['size_range'])
        iat = random.uniform(*profile['iat_range'])
        
        payload = b'X' * size
        sock.sendto(payload, (target_ip, port))
        pkts_sent += 1
        bytes_sent += size
        time.sleep(iat)
        
    mbps = (bytes_sent * 8) / (duration * 1_000_000)
    print(f"[+] Done! Sent {pkts_sent} packets ({bytes_sent/1024:.1f} KB) | Avg Rate: {mbps:.3f} Mbps")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['server', 'client'], required=True)
    parser.add_argument('--ip', default='10.0.0.4', help='Destination IP (for client)')
    parser.add_argument('--port', type=int, default=5001, help='UDP Port')
    parser.add_argument('--profile', choices=['voip', 'streaming', 'web', 'bulk'], default='voip')
    parser.add_argument('--duration', type=int, default=15)
    args = parser.parse_args()

    if args.mode == 'server':
        run_server(args.port)
    else:
        run_client(args.ip, args.port, args.profile, args.duration)