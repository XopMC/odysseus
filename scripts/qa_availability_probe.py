"""Bounded minute availability probe; it never reads chat content.

Run with --url http://LAN:port --samples 361. TLS checks here measure
availability only for a known self-signed development LAN endpoint.
"""
import argparse
import datetime
import json
import subprocess
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--samples", type=int, default=361)
    parser.add_argument("--interval", type=float, default=60)
    args = parser.parse_args()
    if not 1 <= args.samples <= 361 or args.interval < 1:
        parser.error("bounded samples/positive interval required")
    started = time.monotonic()
    failures = 0
    for index in range(args.samples):
        if index:
            time.sleep(max(0, started + index * args.interval - time.monotonic()))
        row = {"sample": index + 1, "utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        for scheme in ("http", "https"):
            url = scheme + "://" + args.url.split("://", 1)[-1].rstrip("/") + "/api/health"
            result = subprocess.run(
                ["curl", "--silent", "--insecure", "--output", "/dev/null",
                 "--connect-timeout", "4", "--max-time", "8",
                 "--write-out", "%{http_code}", url], capture_output=True, text=True,
            )
            row[scheme] = result.stdout.strip()
            row[scheme + "_exit"] = result.returncode
            failures += int(result.returncode != 0 or row[scheme] != "200")
        row["failures"] = failures
        print(json.dumps(row), flush=True)
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
