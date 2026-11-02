"""Queues tab: what each running ComfyUI box is processing, waiting on, and just finished.

ComfyUI keeps its own queue in `/queue` and the last few prompts in `/history`.
The browser cannot reach the box directly, so this router fans out over the
running instances, summarises both payloads, and returns one entry per box.
Nothing is cached: the page has its own refresh timer and every call is live.
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import APIRouter, HTTPException

from assets import asset_kind

SAVE_NODES = {"SaveImage", "PreviewImage", "SaveVideo", "SaveAnimatedWEBP"}
MODEL_INPUTS = ("ckpt_name", "unet_name", "lora_name")
OUTCOMES = {"execution_success": "success", "execution_error": "error"}
MAX_BOXES = 4
MAX_MODELS = 4
HISTORY_ITEMS = 20
QUEUE_TIMEOUT = 15


class ComfyError(Exception):
    """Short, page-safe text for one box's failed fetch."""


def nodes(prompt) -> list[dict]:
    return [node for node in (prompt or {}).values() if isinstance(node, dict)]


def prefix_label(prompt: dict, only: set[str] | None = None) -> str | None:
    for node in nodes(prompt):
        if only is not None and node.get("class_type") not in only:
            continue
        value = (node.get("inputs") or {}).get("filename_prefix")
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
    return None


def prompt_label(prompt: dict) -> str:
    """A save node's output prefix wins, then any node's prefix, then a model name,
    then the last node's class_type, then "workflow"."""
    label = prefix_label(prompt, SAVE_NODES) or prefix_label(prompt)
    if label:
        return label
    models = prompt_models(prompt)
    if models:
        return models[0]
    last = nodes(prompt)
    if last and isinstance(last[-1].get("class_type"), str):
        return last[-1]["class_type"]
    return "workflow"


def prompt_models(prompt: dict, cap: int = MAX_MODELS) -> list[str]:
    found: list[str] = []
    for node in nodes(prompt):
        inputs = node.get("inputs") or {}
        for key in MODEL_INPUTS:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip() and value not in found:
                found.append(value)
    return found[:cap]


def row_fields(row) -> tuple[int | None, str, dict, dict]:
    """(number, prompt_id, prompt, extra) from one /queue row, tolerant of junk."""
    row = row if isinstance(row, (list, tuple)) else []

    def at(index: int, kind):
        return row[index] if len(row) > index and isinstance(row[index], kind) else None

    return at(0, int), at(1, str) or "", at(2, dict) or {}, at(3, dict) or {}


def started_at(extra: dict):
    value = extra.get("create_time")
    return value if isinstance(value, (int, float)) else None


def queue_item(row, pending: bool) -> dict | None:
    number, prompt_id, prompt_dict, extra = row_fields(row)
    if not prompt_id:
        return None
    item = {
        "id": prompt_id,
        "number": number,
        "label": prompt_label(prompt_dict),
        "models": prompt_models(prompt_dict),
        "nodes": len(prompt_dict),
    }
    if pending:
        return item
    return {
        "id": prompt_id,
        "number": number,
        "started_at": started_at(extra),
        "label": item["label"],
        "models": item["models"],
        "nodes": item["nodes"],
    }


def summarize_queue(queue: dict) -> tuple[list[dict], list[dict]]:
    """(running, pending) from ComfyUI's GET /queue."""
    queue = queue if isinstance(queue, dict) else {}

    def one(rows, pending: bool) -> list[dict]:
        return [item for item in (queue_item(row, pending) for row in rows or []) if item]

    return one(queue.get("queue_running"), False), one(queue.get("queue_pending"), True)


def history_file(item: dict) -> dict | None:
    filename = item.get("filename")
    if not isinstance(filename, str) or not filename:
        return None
    meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return {
        "filename": filename,
        "subfolder": item.get("subfolder") or "",
        "type": item.get("type") or "output",
        "kind": asset_kind(item),
        "width": meta.get("width"),
        "height": meta.get("height"),
    }


def output_files(outputs) -> list[dict]:
    """Every saved file, whatever the node calls its list (images, gifs, audio, ...)."""
    files = []
    for output in (outputs or {}).values() if isinstance(outputs, dict) else []:
        if not isinstance(output, dict):
            continue
        for entries in output.values():
            for item in entries if isinstance(entries, list) else []:
                if isinstance(item, dict):
                    entry = history_file(item)
                    if entry:
                        files.append(entry)
    return files


def history_item(prompt_id: str, job: dict) -> dict:
    status = job.get("status") if isinstance(job.get("status"), dict) else {}
    start = ended = None
    outcome = None
    for message in status.get("messages") or []:
        if not isinstance(message, (list, tuple)) or len(message) < 2 or not isinstance(message[1], dict):
            continue
        kind = message[0]
        stamp = message[1].get("timestamp")
        if not isinstance(stamp, (int, float)):
            continue
        if kind == "execution_start" and start is None:
            start = stamp
        elif kind in {"execution_success", "execution_error"}:
            ended, outcome = stamp, kind
    files = output_files(job.get("outputs"))
    return {
        "id": prompt_id,
        "status": OUTCOMES.get(outcome or "") or str(status.get("status_str") or "unknown"),
        "started_at": start,
        "ended_at": ended,
        "files": files,
        "file_count": len(files),
    }


def summarize_history(history: dict, limit: int = HISTORY_ITEMS) -> list[dict]:
    """Newest first; a prompt still running or cancelled has no end stamp."""
    items = [
        history_item(prompt_id, job)
        for prompt_id, job in (history.items() if isinstance(history, dict) else [])
        if isinstance(job, dict)
    ]
    items.sort(key=lambda item: item.get("ended_at") or 0, reverse=True)
    return items[:limit]


def box_header(row: dict, error: str | None = None) -> dict:
    """One box's contract shape; an error keeps the lists empty instead of raising."""
    uuid = row.get("uuid")
    return {
        "uuid": uuid,
        "name": row.get("name") or uuid,
        "comfy": None,
        "error": error,
        "version": None,
        "counts": {"running": 0, "pending": 0, "finished": 0},
        "running": [],
        "pending": [],
        "finished": [],
    }


def fill_box(box: dict, queue: dict, history: dict, stats: dict | None) -> dict:
    running, pending = summarize_queue(queue)
    finished = summarize_history(history)
    system = (stats or {}).get("system") if isinstance(stats, dict) else None
    box["version"] = system.get("comfyui_version") if isinstance(system, dict) else None
    box["running"] = running
    box["pending"] = pending
    box["finished"] = finished
    box["counts"] = {"running": len(running), "pending": len(pending), "finished": len(finished)}
    return box


async def comfy_json(client: httpx.AsyncClient, url: str) -> dict:
    try:
        response = await client.get(url)
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException as exc:
        raise ComfyError("comfy Timeout") from exc
    except httpx.HTTPStatusError as exc:
        raise ComfyError(f"comfy HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise ComfyError(f"comfy {type(exc).__name__}") from exc
    except ValueError as exc:
        raise ComfyError("comfy non-JSON") from exc
    if not isinstance(payload, dict):
        raise ComfyError("comfy non-JSON")
    return payload


async def soft(coro):
    """system_stats is decoration: a slow box still shows its queue."""
    try:
        return await coro
    except ComfyError:
        return None


def build_queue_router(comfy_url, instance_list) -> APIRouter:
    """comfy_url(uuid) -> base https URL for a running instance, or None;
    instance_list() -> AutoDL instance records, for names and the running set."""
    router = APIRouter(prefix="/api/queues", tags=["queues"])

    async def box_snapshot(client: httpx.AsyncClient, row: dict) -> dict:
        box = box_header(row)
        base = await comfy_url(row.get("uuid"))
        if not base:
            box["error"] = "no ComfyUI endpoint"
            return box
        box["comfy"] = base
        try:
            queue, history, stats = await asyncio.wait_for(
                asyncio.gather(
                    comfy_json(client, f"{base}/queue"),
                    comfy_json(client, f"{base}/history?max_items={HISTORY_ITEMS}"),
                    soft(comfy_json(client, f"{base}/system_stats")),
                ),
                QUEUE_TIMEOUT,
            )
        except (ComfyError, asyncio.TimeoutError) as exc:
            box["error"] = str(exc) or "comfy Timeout"
            return box
        return fill_box(box, queue, history, stats)

    @router.get("")
    async def queues(uuid: str | None = None) -> dict:
        rows = [row for row in await instance_list() if row.get("status") == "running"]
        if uuid is not None:
            rows = [row for row in rows if row.get("uuid") == uuid]
            if not rows:
                raise HTTPException(400, "unknown or stopped instance")
        semaphore = asyncio.Semaphore(MAX_BOXES)

        async def one(client: httpx.AsyncClient, row: dict) -> dict:
            async with semaphore:
                try:
                    return await box_snapshot(client, row)
                except Exception as exc:  # one box must not sink the page
                    return box_header(row, error=f"comfy {type(exc).__name__}")

        async with httpx.AsyncClient(timeout=QUEUE_TIMEOUT) as client:
            boxes = await asyncio.gather(*(one(client, row) for row in rows))
        return {"now": int(time.time() * 1000), "boxes": list(boxes)}

    return router
