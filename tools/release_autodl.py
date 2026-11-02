#!/usr/bin/env python3
"""Power off and release an AutoDL instance.

Destructive: release deletes the instance and its system disk and ends the
meter. `power_off` alone does not stop billing.

    python3 tools/release_autodl.py --uuid pro-79353237873e --confirm
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.request
from pathlib import Path

BASE = "https://www.autodl.art"
TOKEN_PATH = Path.home() / ".config" / "adl" / "token"


def call(path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Authorization": token, **({"Content-Type": "application/json"} if data else {})},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode())


def instance_status(token: str, uuid: str) -> str | None:
    payload = call("/api/v1/adl_server/instance/pro?page_index=1&page_size=50", token)
    for item in payload.get("data", {}).get("list", []):
        if item.get("uuid") == uuid:
            return item.get("status")
    return None


def wait_for(token: str, uuid: str, wanted: set[str], timeout: int) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = instance_status(token, uuid)
        if status in wanted or (status is None and "absent" in wanted):
            return status
        time.sleep(5)
    return instance_status(token, uuid)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uuid", action="append", required=True)
    parser.add_argument("--confirm", action="store_true", help="required: release is destructive")
    parser.add_argument("--token-file", default=str(TOKEN_PATH))
    args = parser.parse_args()

    if not args.confirm:
        print("refusing to release without --confirm", file=sys.stderr)
        return 1

    token = Path(args.token_file).read_text().strip()
    for uuid in args.uuid:
        status = instance_status(token, uuid)
        if status is None:
            print(f"{uuid} not found; nothing to do")
            continue

        print(f"[release] {uuid} status={status} -> power_off")
        print(call("/api/v1/adl_server/instance/pro/power_off", token, {"instance_uuid": uuid}))
        status = wait_for(token, uuid, {"shutdown"}, timeout=900)
        if status != "shutdown":
            print(f"[release] power_off did not reach shutdown (status={status}); aborting release", file=sys.stderr)
            return 1

        print(f"[release] {uuid} shutdown -> release")
        print(call("/api/v1/adl_server/instance/pro/release", token, {"instance_uuid": uuid}))
        status = wait_for(token, uuid, {"absent"}, timeout=600)
        if status is not None:
            print(f"[release] instance still listed after release (status={status})", file=sys.stderr)
            return 1
        print(f"[release] {uuid} released")

    from servers import sync

    sync()
    print("[release] servers.json refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
