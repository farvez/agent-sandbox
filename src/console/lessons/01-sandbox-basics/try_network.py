"""Lesson 1: which parts of the internet can this sandbox reach?

Two layers keep the sandbox in:
1. Its only way out is the workspace's egress proxy, which allows HTTPS to the hosts the
   workspace was started with, refuses everything else (and plain HTTP), and logs every attempt.
2. Code that ignores the proxy and connects directly has no route to anywhere.
"""
import socket
import urllib.error
import urllib.request

THROUGH_THE_PROXY = [
    "https://pypi.org/simple/requests/",          # allowed with the "pypi" preset
    "https://api.anthropic.com/",                 # allowed with the "llm" preset (lessons 2 and 3)
    "https://example.com/",                       # on no list: refused
    "http://169.254.169.254/latest/meta-data/",   # the cloud server's metadata service, over plain HTTP
]
DIRECT = [
    ("1.1.1.1", 443),          # a public address, skipping the proxy
    ("169.254.169.254", 80),   # the metadata service, skipping the proxy
]

print("Through the egress proxy:")
for url in THROUGH_THE_PROXY:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            print(f"  OPEN     {url}  (HTTP {response.status})")
    except urllib.error.HTTPError as e:
        if url.startswith("http://") and e.code == 405:
            print(f"  BLOCKED  {url}  (the proxy refuses plain HTTP)")
        else:
            # The host itself answered (with an error page): the connection was allowed.
            print(f"  OPEN     {url}  (HTTP {e.code})")
    except Exception as e:
        print(f"  BLOCKED  {url}  ({str(getattr(e, 'reason', e))[:60]})")

print("\nDirectly, ignoring the proxy:")
for host, port in DIRECT:
    try:
        socket.create_connection((host, port), timeout=4).close()
        print(f"  OPEN     {host}:{port}")
    except OSError as e:
        print(f"  BLOCKED  {host}:{port}  ({e.strerror or type(e).__name__})")
