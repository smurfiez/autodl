#!/usr/bin/env python3
"""Write the account's live AutoDL instances to servers.json.

Runs automatically at the end of tools/create_autodl.py and after a successful
tools/release_autodl.py, and can be called on its own:

    python3 tools/servers.py

Passwords are deliberately not written; they stay in the instance record
(`GET /api/v1/adl_server/instance/pro`), same rule as the API token.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.autodl.art"
TOKEN_PATH = Path.home() / ".config" / "adl" / "token"
OUT = Path(__file__).resolve().parent.parent / "servers.json"


def call(path: str, token: str) -> dict:
    request = urllib.request.Request(f"{BASE}{path}", headers={"Authorization": token})
    with urllib.request.urlopen(request, timeout=40, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode())


def sync() -> list[dict]:
    """Rewrite servers.json from the API. Returns the server list it wrote."""
    token = TOKEN_PATH.read_text().strip()
    payload = call("/api/v1/adl_server/instance/pro?page_index=1&page_size=50", token)
    rows = []
    for item in payload.get("data", {}).get("list", []):
        ssh = (item.get("ssh_command") or "").split()
        rows.append(
            {
                "uuid": item.get("uuid"),
                "status": item.get("status"),
                "gpu": item.get("snapshot_gpu_alias_name"),
                "region": item.get("region_sign"),
                "payg_price_cent": item.get("payg_price"),
                "comfy": f"https://{item.get('service_6006_domain')}" if item.get("service_6006_domain") else None,
                "panel": f"https://{item.get('service_6008_domain')}" if item.get("service_6008_domain") else None,
                "ssh": ssh[-1] if ssh else None,
                "ssh_port": item.get("ssh_port"),
                "created_at": item.get("created_at"),
            }
        )
    rows.sort(key=lambda r: r.get("created_at") or "")
    OUT.write_text(
        json.dumps({"updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "servers": rows}, indent=2)
        + "\n"
    )
    return rows


if __name__ == "__main__":
    servers = sync()
    print(f"servers.json: {len(servers)} instance(s)")
    for server in servers:
        print(f"  {server['uuid']} {server['status']} {server['gpu']} {server['region']} {server['comfy']}")
