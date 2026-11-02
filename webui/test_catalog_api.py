#!/usr/bin/env python3
"""Checks the /api/apps catalog shape against a throwaway market.db.

No HTTP server, no network: the endpoint coroutines are called directly.

    .venv/bin/python webui/test_catalog_api.py
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCHEMA = """
create table applications (
  id integer primary key,
  uuid text,
  name text,
  describe text,
  logo text,
  tags text,
  star_num integer,
  download_num integer,
  use_num integer,
  latest_version text,
  created_at text,
  updated_at text
);
create table versions (
  id integer primary key,
  application_id integer,
  image_version text,
  framework text,
  framework_v text,
  cuda_v text,
  image_size integer,
  private_image_uuid text,
  start_command text,
  review_status text,
  image_status text,
  created_at text
);
create table translations (
  app_id integer,
  field text,
  lang text,
  text text
);
"""

APPS = [
    # id, name, describe, tags, star_num, updated_at
    (1, "Qwen3一键推理微调", "中文描述一", '["Qwen", "推理"]', 50, "2026-01-01T00:00:00+08:00"),
    (2, "ComfyUI基础镜像", "中文描述二", "not json", 90, "2026-03-01T00:00:00+08:00"),
    (3, "SD模型训练", "中文描述三", '["训练"]', 10, "2026-02-01T00:00:00+08:00"),
]

TRANSLATIONS = [
    (1, "name", "en", "MURAD ComfyUI"),
    (1, "describe", "en", "9 categories of workflow templates"),
    (1, "tags", "en", json.dumps(["Qwen", "ComfyUI"])),
    # app 2 only gets a description: name_en/tags_en must fall back to Chinese
    (2, "describe", "en", "ComfyUI base image with texture tools"),
]

VERSIONS = [
    (11, 1, "1.0", "success", "pass"),
    (12, 1, "1.1", "success", "pass"),
    (13, 1, "0.9", "failed", "pass"),
]


def build_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.executemany(
            "insert into applications (id, uuid, name, describe, tags, star_num, download_num, use_num, "
            "latest_version, created_at, updated_at) values (?, ?, ?, ?, ?, ?, 0, 0, '1.0', ?, ?)",
            [(id_, f"uuid{id_}", name, describe, tags, stars, updated, updated)
             for id_, name, describe, tags, stars, updated in APPS],
        )
        connection.executemany(
            "insert into translations (app_id, field, lang, text) values (?, ?, ?, ?)", TRANSLATIONS
        )
        connection.executemany(
            "insert into versions (id, application_id, image_version, image_status, review_status, created_at) "
            "values (?, ?, ?, ?, ?, '2026-01-01T00:00:00+08:00')",
            VERSIONS,
        )


def ids(payload: dict) -> list[int]:
    return [app["id"] for app in payload["apps"]]


def by_id(payload: dict, app_id: int) -> dict:
    return next(app for app in payload["apps"] if app["id"] == app_id)


def main() -> None:
    directory = Path(tempfile.mkdtemp(prefix="adl-catalog-"))
    db = directory / "market.db"
    build_db(db)
    os.environ["ADL_MARKET_DB"] = str(db)

    import app as market_app  # noqa: E402  (import after the env override)

    assert market_app.MARKET_DB == db, market_app.MARKET_DB

    async def run() -> None:
        catalog = market_app.apps
        detail = market_app.app_versions

        page = await catalog()
        assert page["db"] == "market.db", page
        assert page["total"] == 3 and page["limit"] == 50 and page["offset"] == 0, page
        assert ids(page) == [2, 1, 3], ids(page)  # stars desc

        murad = by_id(page, 1)
        assert murad["name"] == "Qwen3一键推理微调" and murad["name_en"] == "MURAD ComfyUI", murad
        assert murad["describe_en"] == "9 categories of workflow templates", murad
        assert murad["tags"] == ["Qwen", "推理"] and murad["tags_en"] == ["Qwen", "ComfyUI"], murad
        assert murad["versions"] == 2, murad  # only deployable versions counted
        assert set(murad) == {
            "id", "uuid", "name", "name_en", "describe", "describe_en", "tags", "tags_en", "logo",
            "latest_version", "star_num", "use_num", "download_num", "versions", "created_at", "updated_at",
        }, sorted(murad)

        partial = by_id(page, 2)
        assert partial["name_en"] == partial["name"] == "ComfyUI基础镜像", partial
        assert partial["describe_en"] == "ComfyUI base image with texture tools", partial
        assert partial["tags"] == [] and partial["tags_en"] == [], partial  # bad JSON -> empty list

        untranslated = by_id(page, 3)
        assert untranslated["name_en"] == untranslated["name"], untranslated
        assert untranslated["describe_en"] == untranslated["describe"], untranslated
        assert untranslated["tags"] == ["训练"] and untranslated["tags_en"] == ["训练"], untranslated

        # English search, case-insensitive, name and describe
        assert ids(await catalog(q="texture")) == [2], "describe_en search"
        assert ids(await catalog(q="mUrAd")) == [1], "name_en search"
        assert ids(await catalog(q="中文描述三")) == [3], "Chinese describe search"

        hi = await catalog(q="comfyui")
        assert ids(hi) == [2, 1] and hi["total"] == 2, hi

        # sort orders
        assert ids(await catalog(sort="name")) == [2, 1, 3], ids(await catalog(sort="name"))
        assert ids(await catalog(sort="updated")) == [2, 3, 1], ids(await catalog(sort="updated"))
        assert ids(await catalog(sort="stars")) == [2, 1, 3]

        # limit / offset / total stay consistent with the filter
        window = await catalog(limit=1, offset=1)
        assert ids(window) == [1] and window["total"] == 3 and window["limit"] == 1, window
        assert window["offset"] == 1, window
        capped = await catalog(limit=9999)
        assert capped["limit"] == 500 and len(capped["apps"]) == 3, capped
        assert (await catalog(offset=-5))["offset"] == 0
        assert (await catalog(q="comfyui", limit=1, offset=1))["total"] == 2

        # sort is validated, never interpolated
        injected = await catalog(sort="star_num; drop table applications")
        assert injected["total"] == 3 and ids(injected) == [2, 1, 3], injected

        # detail: same app object plus the deployable version rows
        one = await detail(1)
        assert one["app"]["name_en"] == "MURAD ComfyUI", one["app"]
        assert one["app"]["tags_en"] == ["Qwen", "ComfyUI"], one["app"]
        assert [version["id"] for version in one["versions"]] == [12, 11], one["versions"]

        try:
            await detail(404)
        except market_app.HTTPException as exc:
            assert exc.status_code == 404, exc
        else:
            raise AssertionError("unknown app must 404")

        # the table disappearing mid-run is a fallback, not a 500
        with sqlite3.connect(db) as connection:
            connection.execute("drop table translations")
        degraded = await catalog(q="texture")
        assert degraded["total"] == 0 and degraded["apps"] == [], degraded
        fallback = by_id(await catalog(), 1)
        assert fallback["name_en"] == "Qwen3一键推理微调", fallback
        assert fallback["tags_en"] == ["Qwen", "推理"], fallback
        assert (await detail(1))["app"]["describe_en"] == "中文描述一"

    asyncio.run(run())
    print("ok catalog list, search, sort, paging, tags, detail, fallback")


if __name__ == "__main__":
    main()
