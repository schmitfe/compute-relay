"""Installed loopback WSGI server for the supervised local application."""

import os
import sys


def main():
    sys.dont_write_bytecode = True
    os.umask(0o077)
    if os.environ.get("BROKER_LOCAL_RUNTIME") != "1":
        raise SystemExit("Start this server with compute-relay.")
    port = int(os.environ["BROKER_PORT"])
    if not 1 <= port <= 65535:
        raise SystemExit("Choose a valid local port.")
    os.environ["DJANGO_SETTINGS_MODULE"] = "compute_relay.control.settings"
    from django.core.wsgi import get_wsgi_application
    from waitress import serve
    serve(get_wsgi_application(), host="127.0.0.1", port=port, threads=4,
          expose_tracebacks=False, max_request_body_size=16 * 1024 * 1024,
          ident="Compute Relay")


if __name__ == "__main__":
    main()
