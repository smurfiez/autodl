#!/usr/bin/env python3
"""Cache English names/descriptions/tags for market.db apps.

The curated translations in `tools/translations_en.json` are the primary
source: they are committed, reviewable, and need no network. `sync` copies
them into market.db and only calls the provider (Google's free gtx endpoint)
for apps missing from that file, and only with --online.

    python3 tools/translate_market.py sync                  # import the curated file
    python3 tools/translate_market.py sync --online         # also translate apps missing from it
    python3 tools/translate_market.py sync --limit 20       # translate only 20 rows, then stop
    python3 tools/translate_market.py sync --refresh        # retranslate everything
    python3 tools/translate_market.py sync --workers 6      # more parallel requests
    python3 tools/translate_market.py sync --refresh --workers 2 --delay 0.3   # gentle, after a 429
    python3 tools/translate_market.py show 313              # stored English for one app

Rows live in market.db so the web UI never calls a translator per page view.
Each row stores source_hash = sha256 of the Chinese text it was built from, so
`sync` is idempotent, resumable and cheap to rerun after a market refresh.
Provider is Google's free gtx endpoint; no key, no network in the tests.

    python3 tools/translate_market_test.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.environ.get("ADL_MARKET_DB") or Path(__file__).resolve().parent.parent / "market.db")
SEED = Path(__file__).resolve().parent / "translations_en.json"
LANG = "en"
ENDPOINT = "https://translate.googleapis.com/translate_a/single"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
FIELDS = ("name", "describe", "tags")
TAG_SEP = " | "
ATTEMPTS = 4

SCHEMA = """
create table if not exists translations (
  app_id integer not null,
  field text not null,
  lang text not null,
  text text not null,
  source_hash text not null,
  updated_at text not null,
  primary key (app_id, field, lang)
);
"""

# machine translation gets these wrong (transliterates "Tusheng video", "Dan furnace");
# longest phrase first so 炼丹炉 wins over 炼丹.
GLOSSARY = sorted([
    ("训练丹炉", "trainer"), ("训练炼炉", "trainer"),
    ("炼丹炉", "trainer"), ("丹炉", "trainer"), ("炼炉", "trainer"), ("炼丹", "AI training"),
    ("千问图像", "Qwen Image"), ("千问编辑", "Qwen Edit"),
    ("图生视频", "image-to-video"), ("文生视频", "text-to-video"),
    ("图生图", "image-to-image"), ("文生图", "text-to-image"),
    ("图生3D", "image-to-3D"), ("首尾帧", "first/last frame"), ("对口型", "lip-sync"), ("生图", "image generation"),
    ("字字动画", "ZiZi Animation"), ("灵光短剧", "Lingguang Short Drama"), ("龟派气功", "Kamehameha"),
    ("千问", "Qwen"), ("镜像", "Docker image"), ("数字人", "digital human"),
    ("开机即用", "ready to use"), ("开机即食", "ready to use"), ("魔改版", "Remixed Edition"),
    ("打标", "annotation"), ("反推", "captioning"), ("短剧", "short drama"),
    ("屾哥", "Shen Ge"), ("文生", "text-to-video"), ("图生", "image-to-video"),
], key=lambda pair: -len(pair[0]))

TAG_GLOSSARY = {
    "音频": "Audio", "训练": "Training", "视频": "Video", "语音": "Voice", "漫剧": "Comic Drama",
    "短剧": "Short Drama", "二次元": "Anime", "混元": "Hunyuan", "智源GLM": "Zhipu GLM",
    "小说": "Novel", "电商": "E-commerce", "图像": "Image", "模型": "Model", "打标": "Annotation",
}

CJK = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[._+-][A-Za-z0-9]+)*")
SMALL_WORDS = {"a", "an", "the", "and", "or", "of", "for", "with", "in", "on", "to", "by", "at", "from", "vs"}


class TranslateError(RuntimeError):
    """The provider refused to give a usable translation."""


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def load_seed() -> dict[tuple[int, str], str]:
    """{(app_id, field): text} from the committed translations file."""
    if not SEED.exists():
        return {}
    raw = json.loads(SEED.read_text())
    seed = {}
    for key, entry in raw.items():
        app_id = int(key)
        for field in FIELDS:
            value = entry.get(field)
            if field == "tags":
                value = json.dumps(value or [], ensure_ascii=False)
            if isinstance(value, str) and value.strip():
                seed[(app_id, field)] = value
    return seed


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def is_ascii(text: str) -> bool:
    return all(ord(char) < 128 for char in text)


def call_provider(text: str, attempts: int = ATTEMPTS) -> str:
    query = urllib.parse.urlencode({"client": "gtx", "sl": "zh-CN", "tl": "en", "dt": "t", "q": text})
    request = urllib.request.Request(f"{ENDPOINT}?{query}", headers={"User-Agent": USER_AGENT})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 429, 500, 502, 503, 504):
                raise TranslateError(f"HTTP {exc.code} from translate.googleapis.com") from exc
            if attempt == attempts:
                raise TranslateError(
                    f"HTTP {exc.code} from translate.googleapis.com after {attempts} attempts — "
                    "the free endpoint is rate limiting; rerun sync later"
                ) from exc
            time.sleep(1.5 * 2 ** (attempt - 1))
            continue
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            if attempt == attempts:
                raise TranslateError(f"translate.googleapis.com unreachable: {exc}") from exc
            time.sleep(1.5 * 2 ** (attempt - 1))
            continue
        segments = (body or [[]])[0] or []
        translated = "".join(part[0] for part in segments if part and part[0]).strip()
        if not translated:
            raise TranslateError(f"empty translation for {text[:40]!r}")
        return translated
    raise TranslateError(f"no translation for {text[:40]!r}")


def keeps_ascii_tokens(source: str, candidate: str) -> bool:
    """True when every brand/version token in the source survived translation."""
    lowered = candidate.lower()
    return all(token.lower() in lowered for token in ASCII_TOKEN.findall(source))


def segments(text: str) -> list[tuple[bool, str]]:
    parts: list[tuple[bool, str]] = []
    for char in text:
        cjk = bool(CJK.match(char))
        if parts and parts[-1][0] == cjk:
            parts[-1] = (cjk, parts[-1][1] + char)
        else:
            parts.append((cjk, char))
    return [part for part in parts if part[1].strip()]


def rebuild(text: str, translator) -> str:
    """Translate only the Chinese runs, keep ASCII runs exactly as written."""
    out = ""
    for cjk, run in segments(text):
        piece = translator(run).strip() if cjk else run
        if out and piece and out[-1].isalnum() and piece[0].isalnum():
            out += " "
        out += piece
    return out


def prepare(text: str) -> str:
    """Swap terms the provider mangles for their English equivalent before translating."""
    for chinese, english in GLOSSARY:
        text = text.replace(chinese, f" {english} ")
    return " ".join(text.split())


def translate_text(text: str, translator=None) -> str:
    """Translate one source string. ASCII-only text is copied through untouched."""
    text = prepare((text or "").strip())
    if not text or is_ascii(text):
        return text
    translator = translator or call_provider
    candidate = translator(text)
    return candidate if keeps_ascii_tokens(text, candidate) else rebuild(text, translator)


def capitalise(word: str, first: bool) -> str:
    bare = word.strip("_+/,()[]!?")
    if not bare or any(char.isupper() for char in bare[1:]) or any(char.isdigit() for char in bare):
        return word
    if not first and bare.lower() in SMALL_WORDS:
        return word.lower()
    position = next((i for i, char in enumerate(word) if char.isalpha()), 0)
    return word[:position] + word[position].upper() + word[position + 1:]


def title_case(text: str) -> str:
    words = []
    for index, word in enumerate(text.split()):
        parts = re.split(r"([-/])", word)
        words.append("".join(
            part if part in "-/" else capitalise(part, index == 0 and position == 0)
            for position, part in enumerate(parts)
        ))
    return " ".join(words)


def sentence_case(text: str) -> str:
    position = next((i for i, char in enumerate(text) if char.isalpha()), None)
    return text if position is None else text[:position] + text[position].upper() + text[position + 1:]


def parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        tags = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(tag).strip() for tag in tags if str(tag).strip()] if isinstance(tags, list) else []


def translate_tags(raw: str | None, translator=None) -> str:
    tags = parse_tags(raw)
    if not tags:
        return "[]"
    source = TAG_SEP.join(tags)
    if is_ascii(source):
        return json.dumps(tags, ensure_ascii=False)
    if all(is_ascii(tag) or tag in TAG_GLOSSARY for tag in tags):
        # every chip is already known: no provider round-trip needed
        return json.dumps([TAG_GLOSSARY.get(tag, tag) for tag in tags], ensure_ascii=False)
    else:
        translated = translate_text(source, translator)
        english = [part.strip() for part in translated.split("|") if part.strip()]
        if len(english) != len(tags):  # separator lost in translation: keep the originals
            return json.dumps(tags, ensure_ascii=False)
        english = [TAG_GLOSSARY.get(tag, title_case(part)) for tag, part in zip(tags, english)]
    return json.dumps(english, ensure_ascii=False)


def translate_field(field: str, source: str, translator=None) -> str:
    """English text for one field. ASCII sources are stored as a verbatim copy."""
    if field == "tags":
        return translate_tags(source, translator)
    if is_ascii(source):  # brand names like ComfyUI, so-vits-svc: keep the author's casing
        return source.strip()
    english = translate_text(source, translator) or source.strip()
    return title_case(english) if field == "name" else sentence_case(english)


def pending(db: sqlite3.Connection, refresh: bool, limit: int | None,
            seed: dict[tuple[int, str], str] | None = None) -> tuple[list[tuple[int, str, str]], int]:
    stored = {
        (row["app_id"], row["field"]): (row["source_hash"], row["text"])
        for row in db.execute("select app_id, field, source_hash, text from translations where lang = ?", (LANG,))
    }
    tasks, skipped = [], 0
    for app in db.execute("select id, name, describe, tags from applications order by id"):
        for field in FIELDS:
            source = (app[field] or "").strip()
            if field != "tags" and not source:
                continue
            fresh = not refresh and stored.get((app["id"], field), ("", ""))[0] == digest(source)
            if fresh:
                curated = (seed or {}).get((app["id"], field))
                if curated is None or stored[(app["id"], field)][1] == curated:
                    skipped += 1
                    continue
            tasks.append((app["id"], field, source))
    return tasks[:limit] if limit else tasks, skipped


def write_row(db: sqlite3.Connection, app_id: int, field: str, source: str, text: str, stamp: str) -> None:
    with db:
        db.execute(
            "insert into translations (app_id, field, lang, text, source_hash, updated_at) "
            "values (?, ?, ?, ?, ?, ?) "
            "on conflict (app_id, field, lang) do update set "
            "text = excluded.text, source_hash = excluded.source_hash, updated_at = excluded.updated_at",
            (app_id, field, LANG, text, digest(source), stamp),
        )


def sync(limit: int | None = None, refresh: bool = False, workers: int = 4,
         translator=None, delay: float = 0.0, online: bool = False) -> int:
    translator = translator or call_provider
    db = connect()
    seed = load_seed()
    tasks, skipped = pending(db, refresh, limit, seed)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not tasks:
        print(f"translations: up to date, {skipped} row(s) skipped", file=sys.stderr)
        db.close()
        return 0

    seeded = [task for task in tasks if (task[0], task[1]) in seed]
    remaining = [task for task in tasks if (task[0], task[1]) not in seed]
    for app_id, field, source in seeded:
        write_row(db, app_id, field, source, seed[(app_id, field)], stamp)
    if seeded:
        print(f"import: {len(seeded)} row(s) from {SEED.name}, {skipped} up to date", file=sys.stderr)
    if remaining and not online:
        print(
            f"missing: {len(remaining)} row(s) have no entry in {SEED.name} "
            f"(apps {sorted({task[0] for task in remaining})[:8]}); "
            "add them to that file or rerun with --online",
            file=sys.stderr,
        )
    tasks = remaining if online else []
    if not tasks:
        counts = {row["field"]: row["n"] for row in db.execute(
            "select field, count(*) n from translations where lang = ? group by field", (LANG,))}
        db.close()
        summary = ", ".join(f"{field} {counts.get(field, 0)}" for field in FIELDS)
        print(f"translations: {summary}  (this run: {len(seeded)} imported, 0 translated)")
        return len(seeded)
    print(f"translate: {len(tasks)} row(s) to do, {workers} worker(s)", file=sys.stderr)

    def work(task: tuple[int, str, str]) -> tuple[tuple[int, str, str], str | None, str | None]:
        app_id, field, source = task
        try:
            if delay:
                time.sleep(delay)
            return task, translate_field(field, source, translator), None
        except TranslateError as exc:
            return task, None, str(exc)

    done, failed = 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for task, english, error in pool.map(work, tasks):
            app_id, field, source = task
            done += 1
            if error:
                failed += 1
                print(f"  fail {app_id}/{field}: {error}", file=sys.stderr)
            else:
                write_row(db, app_id, field, source, english, stamp)
            if done % 25 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)}  written {done - failed}  failed {failed}", file=sys.stderr)

    counts = {row["field"]: row["n"] for row in db.execute(
        "select field, count(*) n from translations where lang = ? group by field", (LANG,))}
    db.close()
    summary = ", ".join(f"{field} {counts.get(field, 0)}" for field in FIELDS)
    print(f"translations: {summary}  (this run: {len(seeded)} imported, {done - failed} translated, {failed} failed)")
    if failed:
        raise SystemExit(f"{failed} row(s) failed — rerun `sync` to retry the rest")
    return done - failed


def show(app_id: int) -> None:
    db = connect()
    app = db.execute("select id, name, describe, tags from applications where id = ?", (app_id,)).fetchone()
    if not app:
        db.close()
        raise SystemExit(f"no app {app_id} in market.db — run `python3 tools/market.py sync` first")
    rows = {row["field"]: row for row in db.execute(
        "select * from translations where app_id = ? and lang = ?", (app_id, LANG))}
    db.close()
    print(f"{app['id']}  {app['name']}  ->  {rows['name']['text'] if 'name' in rows else '-'}")
    print(f"describe (zh): {app['describe']}")
    print(f"describe (en): {rows['describe']['text'] if 'describe' in rows else '-'}")
    print(f"tags (zh): {app['tags']}")
    print(f"tags (en): {rows['tags']['text'] if 'tags' in rows else '-'}")
    for field in FIELDS:
        if field in rows:
            print(f"  {field}: hash {rows[field]['source_hash'][:12]}  updated {rows[field]['updated_at']}")
        else:
            print(f"  {field}: missing — run `python3 tools/translate_market.py sync`")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cache English translations for market.db apps")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_parser = sub.add_parser("sync", help="translate missing or stale rows")
    sync_parser.add_argument("--limit", type=int, help="stop after N rows (resume later)")
    sync_parser.add_argument("--refresh", action="store_true", help="retranslate even when the hash matches")
    sync_parser.add_argument("--workers", type=int, default=4, help="parallel requests (default 4)")
    sync_parser.add_argument("--delay", type=float, default=0.0, help="seconds to sleep before each row (default 0)")
    sync_parser.add_argument("--online", action="store_true",
                             help="translate apps missing from translations_en.json (network)")

    show_parser = sub.add_parser("show", help="stored English for one app")
    show_parser.add_argument("app_id", type=int)

    args = parser.parse_args()
    if args.command == "sync":
        if args.workers < 1:
            raise SystemExit("--workers: must be at least 1")
        sync(limit=args.limit, refresh=args.refresh, workers=args.workers, delay=args.delay, online=args.online)
    else:
        show(args.app_id)


if __name__ == "__main__":
    main()
