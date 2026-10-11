"""Lesson 1: which parts of the internet can this sandbox reach?

Every HTTPS connection goes through the workspace's egress proxy, which allows only the hosts
the workspace was started with. Everything else is refused, and every attempt is logged.
"""
import urllib.request

SITES = [
    "https://pypi.org/simple/requests/",       # allowed when the workspace has the "pypi" preset
    "https://api.anthropic.com/",              # allowed with the "llm" preset (lessons 2 and 3)
    "https://example.com/",                    # not on any list: refused
    "http://169.254.169.254/latest/meta-data/",  # the cloud server's metadata service: never reachable
]

for url in SITES:
    try:
        with urllib.request.urlopen(url, timeout=8) as response:
            print(f"OPEN     {url}  (HTTP {response.status})")
    except urllib.error.HTTPError as e:
        # The host answered (with an error page): the connection itself was allowed.
        print(f"OPEN     {url}  (HTTP {e.code})")
    except Exception as e:
        print(f"BLOCKED  {url}  ({type(e).__name__}: {str(e)[:60]})")
