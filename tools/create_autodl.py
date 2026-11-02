#!/usr/bin/env python3
"""Create a MURAD-ComfyUI (app 313) pay-as-you-go instance and wait for it.

Prints the instance uuid and its ComfyUI base URL on success, so the caller can
run one batch and then hand the uuid to tools/release_autodl.py.

    python3 tools/create_autodl.py --gpu "RTX 5090"
    python3 tools/create_autodl.py --gpu "RTX 3090" --data-center westDC3
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
APP = {
    "cg_application_uuid": "app-image-8a23e8ee9e",
    "cg_application_info": {
        "image_id": 313,
        "image_name": "MURAD-ComfyUI",
        "current_version": "2.0",
        "current_version_id": 962,
    },
}
DISK = 32212254720
READY = {"running"}
FAILED = {"shutdown", "create_failed", "pull_image_failed", "shutdown_by_starting_error"}


def call(path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Authorization": token, **({"Content-Type": "application/json"} if data else {})},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=40, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode())


def instances(token: str) -> list[dict]:
    payload = call("/api/v1/adl_server/instance/pro?page_index=1&page_size=50", token)
    return payload.get("data", {}).get("list", [])


def find_slot(token: str, data_center: str, gpu: str) -> dict | None:
    payload = call("/api/v1/adl_server/machine_pro/gpu_type", token, {"data_center_list": [data_center]})
    listed = payload.get("data") or []
    if not any(item.get("gpu_type") == gpu and item.get("gpu_idle_num", 0) > 0 for item in listed):
        return None
    specs = call("/api/v1/adl_server/machine_pro/gpu_spec_config", token, {"gpu_name": gpu, "charge_type": "payg"})
    for item in specs.get("data") or []:
        if item.get("payg"):
            return item
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gpu", default="RTX 5090")
    parser.add_argument("--data-center", default="westDC3")
    parser.add_argument("--token-file", default=str(TOKEN_PATH))
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    token = Path(args.token_file).read_text().strip()
    for item in instances(token):
        if item.get("status") not in ("shutdown",):
            print(f"note: {item.get('uuid')} is {item.get('status')}", file=sys.stderr)

    slot = find_slot(token, args.data_center, args.gpu)
    if not slot:
        print(f"no {args.gpu} slot in {args.data_center}", file=sys.stderr)
        return 1
    spec_id = slot.get("id")
    print(f"[create] {args.gpu} spec {spec_id} in {args.data_center}")

    price = {
        "coupon_id_list": [],
        "charge_type": "payg",
        "duration": 1,
        "num": 1,
        "expand_system_disk": DISK,
        "gpu_spec_info": {"id": spec_id},
    }
    body = {
        "instance_info": {
            "charge_type": "payg",
            "req_gpu_amount": 1,
            "data_center_list": [args.data_center],
            "expand_system_disk": DISK,
            "gpu_spec_info": {"id": spec_id},
            **APP,
        },
        "price_info": price,
    }
    answer = call("/api/v1/adl_server/order/instance/pro/create/payg", token, body)
    uuid = answer.get("data")
    if not uuid or not isinstance(uuid, str):
        print(f"create failed: {answer}", file=sys.stderr)
        return 1
    print(f"[create] {uuid}")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        record = next((i for i in instances(token) if i.get("uuid") == uuid), None)
        status = record.get("status") if record else None
        print(f"[create] {uuid} {status}")
        if status in READY and record:
            domain = record.get("service_6006_domain") or ""
            host = domain.replace("https://", "").replace("http://", "")
            print(f"[ready] {uuid} {host}")
            print(f"[ready] ssh {record.get('ssh_command')}")
            from servers import sync

            sync()
            print("[ready] server recorded in servers.json")
            return 0
        if status in FAILED:
            print(f"[create] {uuid} failed with {status}", file=sys.stderr)
            return 1
        time.sleep(10)

    print(f"[create] {uuid} not running after {args.timeout}s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
