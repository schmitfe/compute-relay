"""Trusted bounded directory refresh: python -m ...identity.refresh [--interval N]."""

import argparse
import json
import os
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--interval", type=int, help="Repeat every 1–300 seconds; omission performs one bounded pass")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100 or (args.interval is not None and not 1 <= args.interval <= 300):
        parser.error("limit must be 1–100; interval must be 1–300 seconds")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "compute_broker.control.settings")
    import django
    django.setup()
    from .authority import refresh_due_identities
    while True:
        print(json.dumps(refresh_due_identities(limit=args.limit)), flush=True)
        if args.interval is None:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
