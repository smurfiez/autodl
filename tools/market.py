#!/usr/bin/env python3
"""Crawl the AutoDL application marketplace into market.db and query it.

    python3 tools/market.py sync                 # list + detail for every app
    python3 tools/market.py sync --no-detail     # list only, faster
    python3 tools/market.py fetch --pages 1 --out /tmp/a.json   # crawl one page slice
    python3 tools/market.py load /tmp/a.json /tmp/b.json         # merge slices into market.db
    python3 tools/market.py find comfy           # free text over name/describe/readme/tags
    python3 tools/market.py find --tag 视频 --framework PyTorch --sort star
    python3 tools/market.py show 273             # one app, its versions, readme head
    python3 tools/market.py sql "select name, star_num from applications order by star_num desc limit 5"

Token is read from ~/.config/adl/token (raw JWT, no `Bearer`), same rule as
tools/servers.py. market.db is disposable: rerun `sync` to refresh.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://www.autodl.art"
TOKEN_PATH = Path.home() / ".config" / "adl" / "token"
DB = Path(os.environ.get("ADL_MARKET_DB") or Path(__file__).resolve().parent.parent / "market.db")
PAGE_SIZE = 100

SCHEMA = """
create table if not exists applications (
  id integer primary key,
  uuid text,
  name text,
  username text,
  nickname text,
  describe text,
  author_note text,
  readme text,
  logo text,
  avatar text,
  tags text,
  star_num integer,
  download_num integer,
  use_num integer,
  runtime integer,
  popular_type text,
  is_star integer,
  is_top integer,
  is_base_image integer,
  latest_version text,
  latest_version_updated_at text,
  created_at text,
  updated_at text
);
create table if not exists versions (
  id integer primary key,
  application_id integer references applications(id),
  image_version text,
  framework text,
  framework_v text,
  cuda_v text,
  chip_corp text,
  cpu_arch text,
  image_size integer,
  private_image_uuid text,
  private_image_name text,
  start_command text,
  review_status text,
  image_status text,
  download_num integer,
  runtime integer,
  describe text,
  created_at text,
  updated_at text
);
create index if not exists versions_app on versions(application_id);
"""

APP_COLUMNS = [
    "id", "uuid", "name", "username", "nickname", "describe", "author_note",
    "readme", "logo", "avatar", "tags", "star_num", "download_num", "use_num", "runtime",
    "popular_type", "is_star", "is_top", "is_base_image", "latest_version",
    "latest_version_updated_at", "created_at", "updated_at",
]

VERSION_COLUMNS = [
    "id", "application_id", "image_version", "framework", "framework_v", "cuda_v",
    "chip_corp", "cpu_arch", "image_size", "private_image_uuid",
    "private_image_name", "start_command", "review_status", "image_status",
    "download_num", "runtime", "describe", "created_at", "updated_at",
]


def call(path: str, token: str, payload: dict, attempts: int = 4) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=40, context=ssl.create_default_context()) as response:
                body = json.loads(response.read().decode())
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:  # connection resets observed mid-crawl
            if attempt == attempts:
                raise SystemExit(f"{BASE}{path}: {exc}") from exc
            print(f"  retry {attempt}/{attempts - 1} after {exc}", file=sys.stderr)
            time.sleep(2 * attempt)
    if body.get("code") != "Success":
        raise SystemExit(f"{path}: {body.get('code')} {body.get('message') or body.get('msg') or ''}".strip())
    return body["data"]


def fetch_pages(token: str, pages: list[int], detail: bool) -> list[dict]:
    """Crawl the given list pages and return app records (detail merged over list)."""
    apps = []
    for page in pages:
        listed = call("/api/v1/application", token, {"page_index": page, "page_size": PAGE_SIZE, "name": "", "unified_tag_ids": []})["list"]
        print(f"page {page}: {len(listed)} app(s)", file=sys.stderr)
        for index, item in enumerate(listed, 1):
            app = dict(item)
            if detail:
                full = call("/api/v1/application/square/detail", token, {"application_id": item["id"]})
                app.update({key: full[key] for key in full if key not in ("version_list", "id")})
                app["readme"] = full.get("readme")
            app["tags"] = json.dumps([t.get("tag_name") for t in (app.get("tag_list") or [])], ensure_ascii=False)
            apps.append(app)
            if index % 50 == 0:
                print(f"  detail {index}/{len(listed)} on page {page}", file=sys.stderr)
    return apps


def max_page(token: str) -> int:
    return call("/api/v1/application", token, {"page_index": 1, "page_size": PAGE_SIZE, "name": "", "unified_tag_ids": []})["max_page"]


def fetch(out: Path, pages: list[int] | None = None, detail: bool = True) -> int:
    token = TOKEN_PATH.read_text().strip()
    apps = fetch_pages(token, pages or list(range(1, max_page(token) + 1)), detail)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(apps, ensure_ascii=False))
    print(f"{out}: {len(apps)} app(s)")
    return len(apps)


def load(paths: list[Path]) -> int:
    apps: dict[int, dict] = {}
    for path in paths:
        for app in json.loads(path.read_text()):
            if app.get("tag_list") is not None:
                app["tags"] = json.dumps([t.get("tag_name") for t in app["tag_list"]], ensure_ascii=False)
            apps[app["id"]] = app
    db = connect()
    with db:
        db.execute("delete from versions")
        db.execute("delete from applications")
        for app in apps.values():
            db.execute(
                f"insert into applications ({','.join(APP_COLUMNS)}) values ({','.join('?' * len(APP_COLUMNS))})",
                [app.get(column) for column in APP_COLUMNS],
            )
            for version in app.get("version_list") or []:
                db.execute(
                    f"insert or replace into versions ({','.join(VERSION_COLUMNS)}) values ({','.join('?' * len(VERSION_COLUMNS))})",
                    [version.get(column) for column in VERSION_COLUMNS],
                )
    db.close()
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"market.db: {len(apps)} app(s), refreshed {stamp}")
    return len(apps)


def sync(detail: bool = True) -> int:
    tmp = Path(f"/tmp/autodl-market-{int(time.time())}.json")
    fetch(tmp, detail=detail)
    try:
        return load([tmp])
    finally:
        tmp.unlink(missing_ok=True)


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def find(text: str | None, tag: str | None, framework: str | None, cuda: str | None,
         chip: str | None, cpu_arch: str | None, sort: str, limit: int, as_json: bool) -> None:
    where, params = [], []
    if text:
        where.append("(a.name like ? or a.describe like ? or a.readme like ? or a.tags like ?)")
        params += [f"%{text}%"] * 4
    if tag:
        where.append("a.tags like ?")
        params.append(f'%"{tag}"%')
    if framework:
        where.append("v.framework like ?")
        params.append(f"%{framework}%")
    if cuda:
        where.append("v.cuda_v like ?")
        params.append(f"%{cuda}%")
    if chip:
        where.append("v.chip_corp = ?")
        params.append(chip)
    if cpu_arch:
        where.append("v.cpu_arch = ?")
        params.append(cpu_arch)
    order = {"star": "a.star_num", "download": "a.download_num", "runtime": "a.runtime", "name": "a.name"}.get(sort)
    if not order:
        raise SystemExit("--sort: star|download|runtime|name")

    sql = f"""
      select a.id, a.name, a.star_num, a.download_num, a.runtime, a.latest_version,
             v.framework, v.framework_v, v.cuda_v, v.image_size, a.tags
      from applications a
      left join versions v on v.application_id = a.id and v.image_version = a.latest_version
      {('where ' + ' and '.join(where)) if where else ''}
      order by {order} desc, a.id
      limit ?
    """
    db = connect()
    rows = [dict(row) for row in db.execute(sql, params + [limit])]
    db.close()
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print("no match")
        return
    print(f"{'id':>4}  {'star':>5}  {'dl':>7}  {'ver':<6} {'framework':<12} {'cuda':<5} {'GB':>5}  name")
    for row in rows:
        size = f"{row['image_size'] / 1e9:.0f}" if row["image_size"] else "-"
        framework = f"{row['framework'] or '-'} {row['framework_v'] or ''}".strip()
        print(
            f"{row['id']:>4}  {row['star_num'] or 0:>5}  {row['download_num'] or 0:>7}  "
            f"{row['latest_version'] or '-':<6} {framework:<12} {row['cuda_v'] or '-':<5} {size:>5}  {row['name']}"
        )


def show(app_id: int) -> None:
    db = connect()
    app = db.execute("select * from applications where id = ?", (app_id,)).fetchone()
    if not app:
        raise SystemExit(f"no app {app_id} in market.db — run sync first")
    print(f"{app['id']}  {app['name']}  ({app['username']} / {app['nickname']})")
    print(f"stars {app['star_num']}  downloads {app['download_num']}  runs {app['use_num']}  runtime {app['runtime'] or '-'}s")
    print(f"latest {app['latest_version']}  tags {app['tags']}  logo {app['logo']}")
    print(f"describe: {app['describe']}")
    if app["author_note"]:
        print(f"author_note: {app['author_note']}")
    print("versions:")
    for version in db.execute("select * from versions where application_id = ? order by image_version desc", (app_id,)):
        size = f"{version['image_size'] / 1e9:.1f} GB" if version["image_size"] else "-"
        print(
            f"  {version['image_version']:<6} {version['framework']} {version['framework_v']} cuda {version['cuda_v']} "
            f"{version['chip_corp']}/{version['cpu_arch']} {size} image={version['private_image_uuid']} "
            f"downloads={version['download_num']} status={version['image_status']}"
        )
    print(f"readme: {(app['readme'] or '')[:600]}")
    db.close()


def sql(statement: str) -> None:
    statements = [part.strip() for part in statement.split(";") if part.strip()]
    if not statements or not all(part.lower().startswith(("select", "with", "pragma")) for part in statements):
        raise SystemExit("read-only: select/with/pragma only, ';' separated")
    db = connect()
    for part in statements:
        rows = db.execute(part).fetchall()
        if not rows:
            print("no rows")
            continue
        print(" | ".join(rows[0].keys()))
        for row in rows:
            print(" | ".join("" if value is None else str(value) for value in tuple(row)))
    db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="AutoDL marketplace image database")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_parser = sub.add_parser("sync", help="refresh market.db from the API")
    sync_parser.add_argument("--no-detail", action="store_true", help="list endpoint only, skips readme/tags")

    fetch_parser = sub.add_parser("fetch", help="crawl list pages to a JSON file (for parallel shards)")
    fetch_parser.add_argument("--pages", help="comma list, e.g. 1,2 (default: every page)")
    fetch_parser.add_argument("--out", type=Path, required=True)
    fetch_parser.add_argument("--no-detail", action="store_true")

    load_parser = sub.add_parser("load", help="merge fetched JSON files into market.db")
    load_parser.add_argument("files", nargs="+", type=Path)

    find_parser = sub.add_parser("find", help="search apps")
    find_parser.add_argument("text", nargs="?", help="matches name, describe, readme, tags")
    find_parser.add_argument("--tag")
    find_parser.add_argument("--framework")
    find_parser.add_argument("--cuda")
    find_parser.add_argument("--chip", help="nvidia|amd|...")
    find_parser.add_argument("--cpu-arch", help="x86|arm64")
    find_parser.add_argument("--sort", default="star", help="star|download|runtime|name")
    find_parser.add_argument("--limit", type=int, default=20)
    find_parser.add_argument("--json", action="store_true")

    show_parser = sub.add_parser("show", help="one app with all versions")
    show_parser.add_argument("app_id", type=int)

    sql_parser = sub.add_parser("sql", help="raw read-only query")
    sql_parser.add_argument("statement")

    args = parser.parse_args()
    if args.command == "sync":
        sync(detail=not args.no_detail)
    elif args.command == "fetch":
        pages = [int(page) for page in args.pages.split(",")] if args.pages else None
        fetch(args.out, pages, detail=not args.no_detail)
    elif args.command == "load":
        load(args.files)
    elif args.command == "find":
        find(args.text, args.tag, args.framework, args.cuda, args.chip, args.cpu_arch, args.sort, args.limit, args.json)
    elif args.command == "show":
        show(args.app_id)
    else:
        sql(args.statement)


if __name__ == "__main__":
    main()
