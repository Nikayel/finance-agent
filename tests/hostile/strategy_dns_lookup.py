"""strategy_dns_lookup -- name-resolution fixture.

Attempts: Resolve public hostnames via socket.getaddrinfo.
Cell must: Prevent name resolution (no DNS, no network).
"""
import socket
import sys

HOSTS = ["example.com", "cloudflare.com", "google.com"]
for name in HOSTS:
    try:
        info = socket.getaddrinfo(name, 80)
    except OSError:
        continue  # resolution blocked as expected
    addr = info[0][4] if info else None
    print("RESOLVED %s -> %r" % (name, addr), flush=True)
    sys.exit(97)  # resolution succeeded -> containment broken

sys.exit(0)  # every lookup failed
