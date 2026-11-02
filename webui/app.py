#!/usr/bin/env python3
"""AutoDL.Art deploy console.

A local FastAPI page that lists the account's pro instances, creates new
pay-as-you-go ones and polls them until they are running, and powers off /
releases them. The API token never reaches the browser: it is read from
ADL_TOKEN or ~/.config/adl/token and attached server-side.

    uvicorn app:app --port 8765
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import paramiko
from fastapi import FastAPI, HTTPException, Path as PathParam, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from assets import build_router
from collector import COLLECTOR
from comfy_queue import build_queue_router

BASE = "https://www.autodl.art"
TOKEN_PATH = Path.home() / ".config" / "adl" / "token"
KNOWN_HOSTS = Path.home() / ".config" / "adl" / "known_hosts"
MARKET_DB = Path(os.environ.get("ADL_MARKET_DB") or Path(__file__).resolve().parent.parent / "market.db").expanduser()
INSTANCES = "/api/v1/adl_server/instance/pro?page_index=1&page_size=200"
POWER_ON = "/api/v1/adl_server/instance/pro/power_on"
POWER_OFF = "/api/v1/adl_server/instance/pro/power_off"
RELEASE = "/api/v1/adl_server/instance/pro/release"

DISK_BASE_GB = 30
DISK_BYTES = 1024**3
READY = {"running"}
FAILED = {"create_failed", "pull_image_failed", "shutdown_by_starting_error"}

# Documented slot ids, used when the live spec lookup does not answer.
GPU_SLOTS = [
    {"id": 3, "name": "vGPU-32GB", "card": "4080(S)-32G", "list_price": 1.77},
    {"id": 2, "name": "vGPU-48GB-350W", "card": "3090-48G", "list_price": 1.87},
    {"id": 7, "name": "RTX 4090D", "card": "4090D-24G", "list_price": 1.98},
    {"id": 8, "name": "RTX 5090 D", "card": "5090D-32G", "list_price": 2.93},
    {"id": 1, "name": "RTX 5090", "card": "5090-32G", "list_price": 3.03},
    {"id": 5, "name": "vGPU-48GB", "card": "4090-48G", "list_price": 3.03},
    {"id": 10, "name": "RTX PRO 5000", "card": "PRO5000-72G", "list_price": 5.56},
    {"id": 9, "name": "RTX 6000D", "card": "6000D-84G", "list_price": 6.80},
    {"id": 4, "name": "RTX PRO 6000", "card": "PRO6000-96G", "list_price": 7.35},
    {"id": 6, "name": "H800", "card": "H800-80G", "list_price": 10.50},
]
SLOT_BY_NAME = {slot["name"]: slot for slot in GPU_SLOTS}
DATA_CENTERS = ["westDC3", "beijingDC2"]

HEALTH_INTERVAL = 10
HOST_TIMEOUT = 10
COMFY_TIMEOUT = 8
STALE_HOST_S = 60
GPU_HOT_C = 85
DISK_FULL = 0.90
SWAP_HIGH = 0.50
DOWN_AFTER = 2

DEPLOY_POLL = 15
DEPLOY_TTL = 6 * 3600
jobs: dict[str, dict] = {}
job_tasks: dict[str, asyncio.Task] = {}
health: dict[str, dict] = {}


def market(query: str, params: tuple = ()) -> list[dict]:
    if not MARKET_DB.exists():
        raise HTTPException(503, f"{MARKET_DB.name} not found next to the repo root")
    with sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, params)]


DEPLOYABLE = "image_status = 'success' and review_status = 'pass'"

CATALOG_SORTS = ("stars", "name", "updated")
CATALOG_COLUMNS = (
    "a.id, a.uuid, a.name, a.describe, a.tags, a.logo, a.latest_version, "
    "a.star_num, a.use_num, a.download_num, a.created_at, a.updated_at"
)
TRANSLATION_COLUMNS = ", tn.text as name_en, td.text as describe_en, tt.text as tags_en"
TRANSLATION_JOINS = (
    "\nleft join translations tn on tn.app_id = a.id and tn.field = 'name' and tn.lang = 'en'"
    "\nleft join translations td on td.app_id = a.id and td.field = 'describe' and td.lang = 'en'"
    "\nleft join translations tt on tt.app_id = a.id and tt.field = 'tags' and tt.lang = 'en'"
)


def has_translations() -> bool:
    """The text corpus is optional: skip the join while it is absent."""
    try:
        return bool(market("select 1 from sqlite_master where type = 'table' and name = 'translations'"))
    except sqlite3.Error:
        return False


def with_translations(build) -> list[dict]:
    """Run build(joined) -> (sql, params); degrade to the Chinese-only query if the
    translations table is missing or does not match the expected columns yet."""
    if has_translations():
        try:
            sql, params = build(True)
            return market(sql, params)
        except sqlite3.OperationalError:
            pass  # table vanished or half-created by the writer; the rows are still worth serving
    sql, params = build(False)
    return market(sql, params)


def parse_tags(value) -> list[str]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []


def english(row: dict, field: str):
    value = row.get(f"{field}_en")
    return value if isinstance(value, str) and value.strip() else row.get(field)


def app_object(row: dict) -> dict:
    tags = parse_tags(row.get("tags"))
    return {
        "id": row.get("id"),
        "uuid": row.get("uuid"),
        "name": row.get("name"),
        "name_en": english(row, "name"),
        "describe": row.get("describe"),
        "describe_en": english(row, "describe"),
        "tags": tags,
        "tags_en": parse_tags(row.get("tags_en")) or tags,
        "logo": row.get("logo"),
        "latest_version": row.get("latest_version"),
        "star_num": row.get("star_num"),
        "use_num": row.get("use_num"),
        "download_num": row.get("download_num"),
        "versions": row.get("versions"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def search_filter(q: str, joined: bool) -> tuple[str, tuple]:
    if not q:
        return "1 = 1", ()
    like = f"%{q}%"
    columns = ["a.name", "a.describe", "tn.text", "td.text"] if joined else ["a.name", "a.describe"]
    return "(" + " or ".join(f"lower({column}) like lower(?)" for column in columns) + ")", (like,) * len(columns)


def sort_clause(sort: str, joined: bool) -> str:
    if sort == "name":
        name = "coalesce(nullif(tn.text, ''), a.name)" if joined else "a.name"
        return f"{name} collate nocase asc, a.id"
    if sort == "updated":
        return "a.updated_at desc, a.id"
    return "a.star_num desc, a.id"


def resolve_image(app_id: int, version_id: int) -> dict:
    """The create/payg fields for one market image version, straight from market.db."""
    apps = market("select id, uuid, name from applications where id = ?", (app_id,))
    versions = market(
        f"select id, image_version, private_image_uuid, image_size, image_status, review_status "
        f"from versions where id = ? and application_id = ?",
        (version_id, app_id),
    )
    if not apps or not versions:
        raise HTTPException(400, f"unknown application {app_id} / version {version_id}")
    version = versions[0]
    if version["image_status"] != "success" or version["review_status"] != "pass":
        raise HTTPException(409, f"image version {version_id} is not deployable ({version['image_status']}/{version['review_status']})")
    return {
        "app_uuid": version["private_image_uuid"] or apps[0]["uuid"],
        "image_id": apps[0]["id"],
        "image_name": apps[0]["name"],
        "version": version["image_version"],
        "version_id": version["id"],
    }


def token() -> str:
    value = os.environ.get("ADL_TOKEN", "").strip()
    if not value and TOKEN_PATH.exists():
        value = TOKEN_PATH.read_text().strip()
    if not value:
        raise HTTPException(503, f"no API token: set ADL_TOKEN or write one to {TOKEN_PATH}")
    return value


async def call(path: str, body: dict | None = None) -> dict:
    headers = {"Authorization": token()}
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=45) as client:
            if body is None:
                response = await client.get(path, headers=headers)
            else:
                response = await client.post(path, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"AutoDL unreachable: {type(exc).__name__}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(502, f"AutoDL answered {response.status_code} with non-JSON") from exc
    if payload.get("code") != "Success":
        reason = payload.get("msg") or payload.get("code") or f"HTTP {response.status_code}"
        raise HTTPException(502, str(reason)[:300])
    return payload


async def instance_list() -> list[dict]:
    payload = await call(INSTANCES)
    return payload.get("data", {}).get("list", [])


async def instance_status(uuid: str) -> str | None:
    for item in await instance_list():
        if item.get("uuid") == uuid:
            return item.get("status")
    return None


async def wait_status(uuid: str, wanted: set[str], timeout: int) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await instance_status(uuid)
        if status in wanted or (status is None and "absent" in wanted):
            return status
        await asyncio.sleep(5)
    return await instance_status(uuid)


def set_job(job_id: str, state: str, message: str = "", kind: str = "release", uuid: str | None = None) -> None:
    jobs[job_id] = {
        "id": job_id,
        "uuid": uuid if uuid is not None else (job_id if kind == "release" else None),
        "kind": kind,
        "state": state,
        "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def release_instance(uuid: str) -> None:
    try:
        set_job(uuid, "powering_off", "power_off sent")
        await call(POWER_OFF, {"instance_uuid": uuid})
        status = await wait_status(uuid, {"shutdown", "absent"}, 900)
        if status == "absent":
            set_job(uuid, "released", "instance disappeared before release")
            return
        if status != "shutdown":
            set_job(uuid, "failed", f"power_off ended as {status}")
            return
        set_job(uuid, "releasing", "shutdown reached, releasing")
        await call(RELEASE, {"instance_uuid": uuid})
        status = await wait_status(uuid, {"absent"}, 600)
        if status is not None:
            set_job(uuid, "failed", f"still listed as {status} after release")
            return
        set_job(uuid, "released", "released")
    except HTTPException as exc:
        set_job(uuid, "failed", str(exc.detail))
    except Exception as exc:
        set_job(uuid, "failed", f"{type(exc).__name__}")


def normalize(item: dict) -> dict:
    price = item.get("payg_price") or 0
    application = item.get("cg_application_info") or {}
    return {
        "uuid": item.get("uuid"),
        "name": item.get("instance_name") or item.get("name") or "",
        "status": item.get("status"),
        "sub_status": item.get("sub_status") or "",
        "gpu": item.get("snapshot_gpu_alias_name") or "",
        "region": item.get("region_name") or item.get("region_sign") or "",
        "price_per_hour": round(price / 1000, 3),
        "comfy": safe_url(item.get("service_6006_domain")),
        "panel": safe_url(item.get("service_6008_domain")),
        "jupyter": safe_url(item.get("jupyter_domain")),
        "ssh": item.get("ssh_command") or "",
        "app": application.get("application_name") or "",
        "version": application.get("current_version") or "",
        "created_at": item.get("created_at") or "",
    }


# Not every image ships python3 on PATH (only some do); find one, else say why.
COLLECTOR_RUN = (
    'for p in python3 /usr/bin/python3 /usr/local/bin/python3 python '
    '/opt/conda/bin/python /root/miniconda3/bin/python /usr/bin/python; do '
    'if command -v "$p" >/dev/null 2>&1; then exec "$p" -; fi; done; '
    'echo "no python interpreter on PATH" >&2; exit 127'
)


def ssh_target(item: dict) -> tuple[str, int, str]:
    parts = (item.get("ssh_command") or "").split()
    host = parts[-1].split("@")[-1] if parts else ""
    port = 22
    if "-p" in parts:
        try:
            port = int(parts[parts.index("-p") + 1])
        except (IndexError, ValueError):
            pass
    return host, port, item.get("root_password") or ""


ssh_clients: dict[str, paramiko.SSHClient] = {}
ssh_locks: dict[str, threading.Lock] = {}


def close_ssh(uuid: str) -> None:
    client = ssh_clients.pop(uuid, None)
    if client is not None:
        with contextlib.suppress(Exception):
            client.close()


def open_ssh(host: str, port: int, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
    if KNOWN_HOSTS.exists():
        client.load_host_keys(str(KNOWN_HOSTS))
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # first sight only; later mismatches raise
    client.connect(
        host,
        port=port,
        username="root",
        password=password,
        timeout=HOST_TIMEOUT,
        banner_timeout=HOST_TIMEOUT,
        auth_timeout=HOST_TIMEOUT,
        allow_agent=False,
        look_for_keys=False,
    )
    client.save_host_keys(str(KNOWN_HOSTS))
    return client


def run_collector(client: paramiko.SSHClient) -> dict:
    stdin, stdout, stderr = client.exec_command(COLLECTOR_RUN, timeout=25)
    stdin.write(COLLECTOR)
    stdin.channel.shutdown_write()
    raw = stdout.read().decode("utf-8", "replace")
    if not raw.strip():
        # no python3 on some images (LTX/musetalk builds); keep the reason, it is remote stderr
        reason = stderr.read().decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError((reason[-1] if reason else "collector printed nothing")[:160])
    return json.loads(raw)


def host_stats(uuid: str, host: str, port: int, password: str) -> dict:
    """Blocking: run collector.py on the box, reusing one SSH session per instance.

    AutoDL's shared sshd occasionally drops a handshake ("Error reading SSH
    protocol banner"), so a fresh connect every 10 s was both slow and flaky.
    Call via to_thread; the per-uuid lock keeps cycles from overlapping.
    """
    lock = ssh_locks.setdefault(uuid, threading.Lock())
    with lock:
        for attempt in (0, 1):
            client = ssh_clients.get(uuid)
            try:
                if client is None or client.get_transport() is None or not client.get_transport().is_active():
                    close_ssh(uuid)
                    client = open_ssh(host, port, password)
                    ssh_clients[uuid] = client
                return run_collector(client)
            except Exception:
                close_ssh(uuid)
                if attempt:
                    raise
                time.sleep(0.5)
        raise RuntimeError("unreachable")


def disk_label(path: str) -> str:
    return {"/": "root", "/root/autodl-fs": "autodl-fs"}.get(path, path.rsplit("/", 1)[-1])


def host_problems(host: dict) -> list[str]:
    problems = []
    gpu = (host.get("gpus") or [{}])[0]
    if isinstance(gpu.get("temp"), (int, float)) and gpu["temp"] >= GPU_HOT_C:
        problems.append(f"GPU {gpu['temp']:.0f}°C")
    if gpu.get("mem_total") and isinstance(gpu.get("mem_used"), (int, float)):
        ratio = gpu["mem_used"] / gpu["mem_total"]
        if ratio > 0.95:
            problems.append(f"VRAM {ratio:.0%}")
    for path, disk in (host.get("disks") or {}).items():
        if disk.get("total") and disk["used"] / disk["total"] >= DISK_FULL:
            problems.append(f"disk {disk_label(path)} {disk['used'] / disk['total']:.0%}")
    swap = host.get("swap") or {}
    if swap.get("total") and swap["used"] / swap["total"] > SWAP_HIGH:
        problems.append(f"swap {swap['used'] / swap['total']:.0%}")
    load = host.get("load") or []
    if load and host.get("cpus") and load[0] > host["cpus"] * 2:
        problems.append(f"load {load[0]:.0f}/{host['cpus']}")
    return problems


async def comfy_state(client: httpx.AsyncClient, base: str) -> dict:
    """Read ComfyUI's own queue and system stats. Public endpoint, no auth needed."""
    try:
        queue_response, stats_response = await asyncio.gather(
            client.get(f"{base}/queue"),
            client.get(f"{base}/system_stats"),
        )
        queue = queue_response.json()
        stats = stats_response.json()
    except Exception as exc:
        return {"error": f"comfy {type(exc).__name__}"}
    running = queue.get("queue_running") or []
    pending = queue.get("queue_pending") or []
    start = None
    if running and len(running[0]) > 3 and isinstance(running[0][3], dict):
        start = running[0][3].get("create_time")
    device = (stats.get("devices") or [{}])[0]
    system = stats.get("system") or {}
    return {
        "running": len(running),
        "pending": len(pending),
        "started_at": start,
        "vram_total": device.get("vram_total"),
        "vram_free": device.get("vram_free"),
        "ram_total": system.get("ram_total"),
        "ram_free": system.get("ram_free"),
        "version": system.get("comfyui_version"),
    }


async def refresh_health() -> None:
    rows = await instance_list()
    running = [row for row in rows if row.get("status") == "running" and row.get("ssh_command")]
    semaphore = asyncio.Semaphore(4)

    async def poll(client: httpx.AsyncClient, row: dict) -> None:
        uuid = row.get("uuid")
        host, port, password = ssh_target(row)
        entry = dict(health.get(uuid) or {})
        base = safe_url(row.get("service_6006_domain"))

        if not host or not password:
            health[uuid] = {
                **entry,
                "host_error": "no ssh credentials on the instance record",
                "fails": entry.get("fails", 0) + 1,
            }
            return

        stats_task = asyncio.to_thread(host_stats, uuid, host, port, password)
        comfy_task = comfy_state(client, base) if base else None

        async with semaphore:
            stats = None
            stats_error = None
            try:
                stats = await stats_task
            except paramiko.AuthenticationException:
                stats_error = "ssh auth rejected"
            except Exception as exc:
                detail = str(exc).strip().splitlines()
                stats_error = f"ssh {type(exc).__name__}: {detail[-1][:120]}" if detail else f"ssh {type(exc).__name__}"
            comfy = None
            if comfy_task is not None:
                comfy = await comfy_task

        if stats is None:
            health[uuid] = {
                **entry,
                "host_error": stats_error or "host stats failed",
                "checked_at": time.time(),
                "fails": entry.get("fails", 0) + 1,
                "comfy": comfy if comfy and "error" not in comfy else None,
                "comfy_error": (comfy or {}).get("error"),
            }
            return
        health[uuid] = {
            "host": stats,
            "host_at": time.time(),
            "host_error": None,
            "fails": 0,
            "last_ok": (stats or {}).get("time") or time.time(),
            "problems": host_problems(stats or {}),
            "comfy": comfy if comfy and "error" not in comfy else None,
            "comfy_error": (comfy or {}).get("error"),
        }

    async with httpx.AsyncClient(timeout=COMFY_TIMEOUT) as client:
        await asyncio.gather(*(poll(client, row) for row in running))
    alive = {row.get("uuid") for row in running}
    for uuid in list(health):
        if uuid not in alive:
            health.pop(uuid, None)
    for uuid in list(ssh_clients):
        if uuid not in alive:
            close_ssh(uuid)


def health_level(entry: dict) -> str:
    if entry.get("fails", 0) >= DOWN_AFTER:
        return "down"
    if not entry.get("host"):
        return "unknown"
    return "warn" if entry.get("problems") else "ok"


async def health_loop() -> None:
    while True:
        try:
            await refresh_health_once()
        except Exception:
            pass  # a bad cycle must not kill the loop; next tick retries
        await asyncio.sleep(HEALTH_INTERVAL)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(health_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="AutoDL deploy console", lifespan=lifespan)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
refresh_lock = asyncio.Lock()
tasks: set[asyncio.Task] = set()


def spawn(coro) -> None:
    """Keep a strong reference: a bare create_task can be garbage-collected mid-flight."""
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


@app.middleware("http")
async def local_only(request, call_next):
    """Block cross-origin/DNS-rebinding writes: the token sits behind these routes."""
    host = request.headers.get("host", "")
    hostname = urlsplit(f"//{host}").hostname or ""
    if hostname not in LOCAL_HOSTS:
        return JSONResponse({"detail": "forbidden host"}, status_code=403)
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin is not None and (urlsplit(origin).hostname or "") not in LOCAL_HOSTS:
            return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": "json body required"}, status_code=415)
    return await call_next(request)


async def refresh_health_once() -> None:
    if refresh_lock.locked():
        return
    async with refresh_lock:
        await refresh_health()


async def comfy_url(uuid: str) -> str | None:
    """ComfyUI base URL for one instance uuid, only while it is running."""
    for item in await instance_list():
        if item.get("uuid") == uuid and item.get("status") == "running":
            return safe_url(item.get("service_6006_domain"))
    return None


def safe_url(value: str | None) -> str | None:
    """Only trust https://<*.seetacloud.com>[:port] from the upstream record."""
    if not value:
        return None
    host = value.split("/")[0].rsplit(":", 1)[0]
    return f"https://{value}" if host.endswith(".seetacloud.com") else None



class CreateRequest(BaseModel):
    gpu: str = Field(min_length=1, max_length=64)
    data_centers: list[str] = Field(default_factory=lambda: list(DATA_CENTERS), min_length=1, max_length=4)
    name: str = Field("", max_length=64)
    app_id: int = Field(ge=1)
    version_id: int = Field(ge=1)
    disk_extra_gb: int = Field(0, ge=0, le=500)
    wait: bool = True  # queued deploy: poll inventory until the slot frees


class ConfirmRequest(BaseModel):
    confirm: bool = False


Uuid = Annotated[str, PathParam(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


app.include_router(build_router(comfy_url))
app.include_router(build_queue_router(comfy_url, instance_list))


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(Path(__file__).with_name("index.html"), headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
async def config() -> dict:
    try:
        token()
        has_token = True
    except HTTPException:
        has_token = False
    return {"has_token": has_token, "data_centers": DATA_CENTERS}


async def idle_by_dc(data_centers: list[str]) -> dict[str, dict[str, int]]:
    """{dc: {gpu_name: idle_count}}; a failing data center counts as empty, not fatal."""

    async def one(dc: str) -> tuple[str, dict[str, int]]:
        try:
            payload = await call("/api/v1/adl_server/machine_pro/gpu_type", {"data_center_list": [dc]})
        except HTTPException:
            return dc, {}
        return dc, {item.get("gpu_type"): item.get("gpu_idle_num", 0) for item in payload.get("data") or []}

    return dict(await asyncio.gather(*(one(dc) for dc in data_centers)))


@app.get("/api/apps")
async def apps(q: str = "", sort: str = "stars", limit: int = 50, offset: int = 0) -> dict:
    q = (q or "").strip()
    sort = sort if sort in CATALOG_SORTS else "stars"
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    def build_rows(joined: bool) -> tuple[str, tuple]:
        where, params = search_filter(q, joined)
        columns = CATALOG_COLUMNS + (TRANSLATION_COLUMNS if joined else "")
        sql = (
            f"select {columns}, "
            f"(select count(*) from versions v where v.application_id = a.id and {DEPLOYABLE}) as versions "
            f"from applications a{TRANSLATION_JOINS if joined else ''} "
            f"where {where} order by {sort_clause(sort, joined)} limit ? offset ?"
        )
        return sql, (*params, limit, offset)

    def build_total(joined: bool) -> tuple[str, tuple]:
        where, params = search_filter(q, joined)
        return (
            f"select count(*) as n from applications a{TRANSLATION_JOINS if joined else ''} where {where}",
            params,
        )

    rows = with_translations(build_rows)
    total = (with_translations(build_total) or [{"n": 0}])[0]["n"]
    return {
        "db": MARKET_DB.name,
        "total": total,
        "offset": offset,
        "limit": limit,
        "apps": [app_object(row) for row in rows],
    }


@app.get("/api/apps/{app_id}")
async def app_versions(app_id: int) -> dict:
    def build_app(joined: bool) -> tuple[str, tuple]:
        columns = CATALOG_COLUMNS + (TRANSLATION_COLUMNS if joined else "")
        sql = (
            f"select {columns}, "
            f"(select count(*) from versions v where v.application_id = a.id and {DEPLOYABLE}) as versions "
            f"from applications a{TRANSLATION_JOINS if joined else ''} where a.id = ?"
        )
        return sql, (app_id,)

    apps = with_translations(build_app)
    if not apps:
        raise HTTPException(404, "unknown application")
    versions = market(
        f"""select id, image_version, framework, framework_v, cuda_v, image_size,
                   private_image_uuid, image_status, review_status, start_command, created_at
            from versions where application_id = ? and {DEPLOYABLE}
            order by id desc""",
        (app_id,),
    )
    return {"app": app_object(apps[0]), "versions": versions}


@app.get("/api/gpus")
async def gpus(dc: list[str] = Query(default=["westDC3"])) -> dict:
    data_centers = [item for item in dict.fromkeys(dc) if item in DATA_CENTERS] or ["westDC3"]
    idle = await idle_by_dc(data_centers)
    names = {slot["name"] for slot in GPU_SLOTS}
    rows = [{**slot, "id": slot["id"], "card": slot["card"]} for slot in GPU_SLOTS]
    rows += [
        {"id": None, "name": name, "card": "", "list_price": None}
        for name in {name for counts in idle.values() for name in counts} - names
    ]
    for row in rows:
        by_dc = {name: counts.get(row["name"], 0) for name, counts in idle.items()}
        row["idle_by_dc"] = by_dc
        row["idle"] = sum(by_dc.values())
        row["available"] = row["idle"] > 0
    rows.sort(key=lambda row: row["idle"], reverse=True)
    return {"data_centers": data_centers, "gpus": rows}


@app.get("/api/instances")
async def instances() -> dict:
    live = await instance_list()
    rows = [normalize(item) for item in live]
    rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    alive = {item.get("uuid") for item in live}
    for uuid in [key for key, job in jobs.items() if job.get("state") == "released" and key not in alive]:
        jobs.pop(uuid, None)
    return {"instances": rows, "jobs": list(jobs.values())}


@app.get("/api/health")
async def health_api() -> dict:
    boxes = []
    missing = False
    for item in await instance_list():
        if item.get("status") != "running":
            continue
        uuid = item.get("uuid")
        entry = health.get(uuid)
        if entry is None:
            missing = True
            entry = {}
        age = round(time.time() - entry["host_at"], 1) if entry.get("host_at") else None
        problems = list(entry.get("problems") or [])
        if age is not None and age > STALE_HOST_S:
            problems.append(f"host stats {age:.0f}s old")
        if entry.get("comfy_error"):
            problems.append("ComfyUI unreachable")
        level = health_level(entry) if entry else "unknown"
        if level == "ok" and problems:
            level = "warn"
        boxes.append(
            {
                "uuid": uuid,
                "name": item.get("name") or uuid,
                "status": item.get("status"),
                "gpu": item.get("snapshot_gpu_alias_name") or "",
                "comfy": safe_url(item.get("service_6006_domain")),
                "host": entry.get("host"),
                "comfy_state": entry.get("comfy"),
                "host_error": entry.get("host_error"),
                "host_age_s": age,
                "last_ok": entry.get("last_ok"),
                "problems": problems,
                "level": level,
            }
        )
    if missing:
        spawn(refresh_health_once())  # first paint before the 10 s loop has run
    return {"now": int(time.time() * 1000), "boxes": boxes}


def create_body(request: CreateRequest, image: dict, spec_id: int, disk: int, data_center: str) -> dict:
    return {
        "instance_info": {
            "charge_type": "payg",
            "req_gpu_amount": 1,
            "data_center_list": [data_center],
            "expand_system_disk": disk,
            "gpu_spec_info": {"id": spec_id},
            "cg_application_uuid": image["app_uuid"],
            "cg_application_info": {
                "image_id": image["image_id"],
                "image_name": image["image_name"],
                "current_version": image["version"],
                "current_version_id": image["version_id"],
            },
            **({"instance_name": request.name} if request.name else {}),
        },
        "price_info": {
            "coupon_id_list": [],
            "charge_type": "payg",
            "duration": 1,
            "num": 1,
            "expand_system_disk": disk,
            "gpu_spec_info": {"id": spec_id},
        },
    }


async def try_create(request: CreateRequest, image: dict, spec_id: int, disk: int, order: list[str]) -> tuple[str, str, list[str]]:
    """Create in the first data center that accepts; returns (uuid, dc, earlier failures)."""
    failures: list[str] = []
    for data_center in order:
        try:
            payload = await call(
                "/api/v1/adl_server/order/instance/pro/create/payg",
                create_body(request, image, spec_id, disk, data_center),
            )
        except HTTPException as exc:
            failures.append(f"{data_center}: {exc.detail}")
            continue
        uuid = payload.get("data")
        if isinstance(uuid, str) and uuid:
            return uuid, data_center, failures
        failures.append(f"{data_center}: accepted without an instance id")
    raise HTTPException(502, "; ".join(failures) or "create failed in every data center")


async def deploy_when_ready(job_id: str, request: CreateRequest, image: dict, spec_id: int, disk: int) -> None:
    deadline = time.monotonic() + DEPLOY_TTL
    while time.monotonic() < deadline:
        idle = await idle_by_dc(request.data_centers)
        order = sorted(request.data_centers, key=lambda dc: idle.get(dc, {}).get(request.gpu, 0), reverse=True)
        if any(idle.get(dc, {}).get(request.gpu, 0) > 0 for dc in order):
            try:
                uuid, data_center, _ = await try_create(request, image, spec_id, disk, order)
            except HTTPException as exc:
                set_job(job_id, "retrying", f"stock seen but create failed: {exc.detail}"[:200], "deploy")
                await asyncio.sleep(DEPLOY_POLL)
                continue
            set_job(job_id, "created", f"{uuid} in {data_center}", "deploy", uuid=uuid)
            return
        set_job(job_id, "waiting", f"no {request.gpu} idle in {' / '.join(request.data_centers)} for {image['image_name']}", "deploy")
        await asyncio.sleep(DEPLOY_POLL)
    set_job(job_id, "failed", f"gave up after {DEPLOY_TTL // 3600}h without stock", "deploy")


@app.post("/api/instances")
async def create(request: CreateRequest) -> dict:
    data_centers = [dc for dc in dict.fromkeys(request.data_centers) if dc in DATA_CENTERS]
    if not data_centers:
        raise HTTPException(400, f"data_centers must be a subset of {', '.join(DATA_CENTERS)}")
    request.data_centers = data_centers
    image = resolve_image(request.app_id, request.version_id)

    spec_id = SLOT_BY_NAME.get(request.gpu, {}).get("id")
    try:
        specs = await call(
            "/api/v1/adl_server/machine_pro/gpu_spec_config",
            {"gpu_name": request.gpu, "charge_type": "payg"},
        )
        for item in specs.get("data") or []:
            if item.get("payg") and item.get("id") is not None:
                spec_id = item["id"]
                break
    except HTTPException:
        if spec_id is None:
            raise
    if spec_id is None:
        raise HTTPException(400, f"unknown GPU {request.gpu!r}; refresh the inventory")

    disk = (DISK_BASE_GB + request.disk_extra_gb) * DISK_BYTES
    idle = await idle_by_dc(data_centers)
    order = sorted(data_centers, key=lambda dc: idle.get(dc, {}).get(request.gpu, 0), reverse=True)
    try:
        uuid, data_center, failures = await try_create(request, image, spec_id, disk, order)
    except HTTPException as exc:
        if not request.wait:
            raise
        job_id = f"dep-{uuid4().hex[:8]}"
        set_job(job_id, "queued", f"queued: {exc.detail}"[:200], "deploy")
        task = asyncio.create_task(deploy_when_ready(job_id, request, image, spec_id, disk))
        job_tasks[job_id] = task
        task.add_done_callback(lambda _task, key=job_id: job_tasks.pop(key, None))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return {"queued": True, "job_id": job_id, "spec_id": spec_id, "image": image, "status": "waiting"}
    return {
        "uuid": uuid,
        "spec_id": spec_id,
        "data_center": data_center,
        "image": image,
        "attempts": failures,
        "status": "creating",
    }


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job or job.get("kind") != "deploy":
        raise HTTPException(404, "no such deploy job")
    if job.get("state") not in {"queued", "waiting", "retrying"}:
        raise HTTPException(409, f"job already {job.get('state')}")
    task = job_tasks.get(job_id)
    if task is not None:
        task.cancel()
    set_job(job_id, "cancelled", "cancelled by user", "deploy")
    return {"ok": True}


@app.post("/api/instances/{uuid}/power_on")
async def power_on(uuid: Uuid) -> dict:
    await call(POWER_ON, {"instance_uuid": uuid})
    return {"ok": True}


@app.post("/api/instances/{uuid}/power_off")
async def power_off(uuid: Uuid) -> dict:
    await call(POWER_OFF, {"instance_uuid": uuid})
    return {"ok": True}


@app.post("/api/instances/{uuid}/release")
async def release(uuid: Uuid, request: ConfirmRequest | None = None) -> dict:
    if not (request and request.confirm):
        raise HTTPException(400, "release needs confirm: true")
    if jobs.get(uuid, {}).get("state") in {"powering_off", "releasing"}:
        raise HTTPException(409, "release already running for this instance")
    set_job(uuid, "queued", "release queued")
    spawn(release_instance(uuid))
    return {"uuid": uuid, "state": "queued"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
