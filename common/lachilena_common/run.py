import os
import sys

import uvicorn


def main() -> None:
    target = sys.argv[1]
    name = os.environ["SERVICE_NAME"]
    options = {}
    if os.getenv("INTERNAL_TLS", "true").lower() == "true":
        options["ssl_certfile"] = f"/certs/{name}.crt"
        options["ssl_keyfile"] = f"/certs/{name}.key"
    uvicorn.run(target, host="0.0.0.0", port=int(os.environ["PORT"]),
                log_config=None, access_log=False, **options)


if __name__ == "__main__":
    main()
