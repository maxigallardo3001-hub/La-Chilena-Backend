import os
import ssl
import sys
import urllib.request

tls = os.getenv("INTERNAL_TLS", "true").lower() == "true"
context = ssl.create_default_context(cafile=os.getenv("CA_FILE")) if tls else None
url = f"{'https' if tls else 'http'}://localhost:{os.environ['PORT']}/health"
try:
    with urllib.request.urlopen(url, timeout=3, context=context) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
