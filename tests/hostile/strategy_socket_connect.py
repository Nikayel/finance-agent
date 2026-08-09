"""strategy_socket_connect -- outbound TCP egress fixture.

Attempts: Open outbound TCP connections to a public IP:port and to localhost ports.
Cell must: Make every outbound connection attempt fail (no network egress).
"""
import socket
import sys

TARGETS = [("1.1.1.1", 53), ("8.8.8.8", 443), ("127.0.0.1", 80), ("127.0.0.1", 22)]
for host, port in TARGETS:
    try:
        conn = socket.create_connection((host, port), timeout=3)
    except OSError:
        continue  # blocked as expected
    conn.close()
    print("CONNECTED %s:%d" % (host, port), flush=True)
    sys.exit(97)  # egress succeeded -> containment broken

sys.exit(0)  # every attempt was refused
