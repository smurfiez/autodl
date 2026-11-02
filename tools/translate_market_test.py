#!/usr/bin/env python3
"""Offline check for tools/translate_market.py: stub translator, temp DB.

    python3 tools/translate_market_test.py

No network, never touches market.db (ADL_MARKET_DB points at a temp file).
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

FIXTURE = [
    (1, "MURAD-ComfyUI", "纯ASCII名称保持原样", '["ComfyUI", "训练"]'),
    (2, "字字动画", "全自动-AI电影", '["二次元"]'),
    (3, "LoRANext云端训练器", "一键微调", "[]"),
]

CALLS: list[str] = []


def stub(text: str) -> str:
    """Deterministic stand-in for the gtx endpoint: 'ZH(<source>)' style text."""
    CALLS.append(text)
    return text.replace("字字动画", "Word by Word Animation").replace("训练器", "Trainer").replace(
        "云端", "Cloud"
    ).replace("全自动", "Fully automatic").replace("AI电影", "AI Movie").replace(
        "一键微调", "one-click fine-tuning"
    ).replace("纯ASCII名称保持原样", "pure ASCII names stay as written").replace(
        "二次元", "Anime"
    ).replace("训练", "Training")


with tempfile.TemporaryDirectory() as tmp:
    os.environ["ADL_MARKET_DB"] = str(Path(tmp) / "test.db")
    import translate_market as tm

    db = tm.connect()
    with db:
        db.executescript(
            "create table if not exists applications (id integer primary key, name text, describe text, tags text);"
        )
        for app_id, name, describe, tags in FIXTURE:
            db.execute("insert into applications (id, name, describe, tags) values (?, ?, ?, ?)", (app_id, name, describe, tags))
    db.close()

    # ascii pass-through: no provider call at all
    assert tm.translate_text("MURAD-ComfyUI") == "MURAD-ComfyUI"
    assert tm.translate_text("zealman-ComfyUI") == "zealman-ComfyUI"

    # tags JSON round-trip, separator kept, title-cased
    tags = json.loads(tm.translate_tags('["二次元", "训练"]', stub))
    assert tags == ["Anime", "Training"], tags
    assert json.loads(tm.translate_tags("[]", stub)) == []
    assert json.loads(tm.translate_tags("not json", stub)) == []

    # a provider failure must surface, never be cached as the Chinese original
    def refuse(text: str) -> str:
        raise tm.TranslateError("HTTP 429")

    # known chips are mapped locally, no provider call
    assert json.loads(tm.translate_tags('["音频"]', refuse)) == ["Audio"]

    try:
        tm.translate_tags('["某个未收录标签"]', refuse)
    except tm.TranslateError:
        pass
    else:
        raise AssertionError("translate_tags must not swallow provider errors")

    # mixed name: ascii brand survives, chinese part translated
    mixed = tm.translate_field("name", "LoRANext云端训练器", stub)
    assert mixed.startswith("LoRANext"), mixed
    assert "Trainer" in mixed, mixed
    assert "云端" not in mixed, mixed

    calls_before = len(CALLS)
    assert tm.sync(translator=stub, online=True) > 0
    db = tm.connect()
    first = db.execute("select count(*) from translations").fetchone()[0]
    assert first == 9, first  # 3 apps x 3 fields
    ascii_row = db.execute("select text from translations where app_id = 1 and field = 'name'").fetchone()[0]
    assert ascii_row == "MURAD-ComfyUI", ascii_row
    empty_tags = db.execute("select text from translations where app_id = 3 and field = 'tags'").fetchone()[0]
    assert empty_tags == "[]", empty_tags
    source = db.execute("select name from applications where id = 2").fetchone()[0]
    assert db.execute(
        "select source_hash from translations where app_id = 2 and field = 'name'"
    ).fetchone()[0] == tm.digest(source)
    db.close()

    # second sync: hash matches, nothing translated and no duplicate rows
    calls_before = len(CALLS)
    assert tm.sync(translator=stub) == 0
    assert len(CALLS) == calls_before, CALLS[calls_before:]
    # --refresh retranslates, still one row per (app, field, lang)
    assert tm.sync(refresh=True, translator=stub, online=True) == 9
    db = tm.connect()
    assert db.execute("select count(*) from translations").fetchone()[0] == 9
    assert len(CALLS) > calls_before
    db.close()

    # stale hash: source changed behind our back, sync picks it up
    db = tm.connect()
    with db:
        db.execute("update applications set describe = '新的训练器' where id = 2")
    db.close()
    assert tm.sync(translator=stub, online=True) == 1

    # curated seed file: imported without touching the network
    seed_path = Path(tmp) / "seed.json"
    seed_path.write_text(json.dumps({"2": {"name": "Word by Word Animation", "describe": "Curated text", "tags": ["Anime"]}}))
    tm.SEED = seed_path
    db = tm.connect()
    with db:
        db.execute("delete from translations where app_id = 2")
    db.close()
    calls_before = len(CALLS)
    assert tm.sync(translator=stub) == 3, "seed rows must be imported without --online"
    assert len(CALLS) == calls_before, CALLS[calls_before:]
    db = tm.connect()
    assert db.execute("select text from translations where app_id = 2 and field = 'describe'").fetchone()[0] == "Curated text"
    db.close()
    tm.SEED = Path(tmp) / "absent.json"

    # provider failure is reported, not written as garbage
    def broken(text: str) -> str:
        raise tm.TranslateError("HTTP 429 from translate.googleapis.com after 4 attempts")

    db = tm.connect()
    with db:
        db.execute("update applications set describe = '另一段描述' where id = 2")
    db.close()
    try:
        tm.sync(translator=broken, online=True)
    except SystemExit as exc:
        assert "failed" in str(exc), exc
    else:
        raise AssertionError("sync must exit non-zero when translation fails")
    db = tm.connect()
    assert db.execute("select count(*) from translations").fetchone()[0] == 9
    db.close()

    tm.show(2)
    tm.show(1)

print("translate_market_test: ok")
