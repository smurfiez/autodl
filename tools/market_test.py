#!/usr/bin/env python3
"""Offline check for tools/market.py: load a fixture, then query it.

    python3 tools/market_test.py

Uses a throwaway DB via ADL_MARKET_DB, so it never touches market.db.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

FIXTURE = [
    {
        "id": 1,
        "name": "ComfyUI done",
        "username": "someone",
        "describe": "image and video studio",
        "readme": "internal workflow notes",
        "tag_list": [{"id": 1, "tag_name": "ComfyUI"}],
        "star_num": 10,
        "download_num": 99,
        "latest_version": "2.0",
        "version_list": [
            {"id": 11, "application_id": 1, "image_version": "2.0", "framework": "PyTorch", "framework_v": "2.13.0", "cuda_v": "13.0", "chip_corp": "nvidia", "cpu_arch": "x86", "image_size": 20_000_000_000, "image_status": "success"},
            {"id": 10, "application_id": 1, "image_version": "1.0", "framework": "PyTorch", "framework_v": "2.5.1", "cuda_v": "12.1", "chip_corp": "nvidia", "cpu_arch": "x86", "image_size": 10_000_000_000, "image_status": "success"},
        ],
    },
    {
        "id": 2,
        "name": "Bare app",
        "describe": "nothing else",
        "latest_version": "1",
        "version_list": [{"id": 20, "application_id": 2, "image_version": "1", "framework": "TensorFlow", "framework_v": "2.15", "cuda_v": "12.0", "chip_corp": "amd", "cpu_arch": "arm64", "image_size": 5_000_000_000, "image_status": "success"}],
    },
]

with tempfile.TemporaryDirectory() as tmp:
    os.environ["ADL_MARKET_DB"] = str(Path(tmp) / "test.db")
    import market

    shard = Path(tmp) / "shard.json"
    shard.write_text(json.dumps(FIXTURE, ensure_ascii=False))
    assert market.load([shard]) == 2

    db = market.connect()
    assert db.execute("select count(*) from applications").fetchone()[0] == 2
    assert db.execute("select count(*) from versions").fetchone()[0] == 3
    db.close()

    market.load([shard])  # rerun must not duplicate rows
    db = market.connect()
    assert db.execute("select count(*) from versions").fetchone()[0] == 3
    db.close()

    market.find("workflow", None, None, None, None, None, "star", 10, False)   # readme hit
    market.find("ComfyUI", "ComfyUI", "PyTorch", "13.0", "nvidia", "x86", "star", 10, False)
    market.find(None, None, "TensorFlow", None, "amd", None, "name", 10, True)
    market.show(1)
    market.sql("select count(*) apps from applications; select max(image_version) from versions")

    db = market.connect()
    latest = db.execute(
        "select v.cuda_v from applications a join versions v on v.application_id = a.id and v.image_version = a.latest_version where a.id = 1"
    ).fetchone()[0]
    tags = db.execute("select tags from applications where id = 1").fetchone()[0]
    db.close()
    assert latest == "13.0", latest
    assert tags == '["ComfyUI"]', tags

print("market_test: ok")
