"""ComfyUI output gallery: list generated assets and proxy their bytes.

ComfyUI keeps every prompt in `/history`; each output node lists files under
`images[]` (images, gifs, videos). The box is only reachable through its
`*.seetacloud.com` domain, so the browser never talks to it directly: the
server lists the files and streams them back through `/api/assets/file`.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
AUDIO_EXT = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
FILE_TYPES = {"output", "temp", "input"}


def asset_kind(item: dict) -> str:
    kind = str((item.get("metadata") or {}).get("kind") or "").lower()
    if kind in {"image", "video", "audio"}:
        return kind
    name = str(item.get("filename") or "").lower()
    if name.endswith(VIDEO_EXT):
        return "video"
    if name.endswith(AUDIO_EXT):
        return "audio"
    return "image"


def collect_assets(history: dict, limit: int) -> list[dict]:
    assets = []
    for prompt_id, job in history.items():
        status = job.get("status") or {}
        finished = None
        for message in status.get("messages") or []:
            if isinstance(message, list) and len(message) > 1 and message[0] in {"execution_success", "execution_error"}:
                finished = (message[1] or {}).get("timestamp") or finished
        for node_id, output in (job.get("outputs") or {}).items():
            for item in output.get("images") or []:
                filename = item.get("filename")
                if not filename:
                    continue
                meta = item.get("metadata") or {}
                assets.append(
                    {
                        "id": f"{prompt_id}:{node_id}:{filename}",
                        "prompt_id": prompt_id,
                        "node": str(node_id),
                        "filename": filename,
                        "subfolder": item.get("subfolder") or "",
                        "type": item.get("type") or "output",
                        "kind": asset_kind(item),
                        "width": meta.get("width"),
                        "height": meta.get("height"),
                        "created_at": finished,
                    }
                )
    assets.sort(key=lambda asset: asset.get("created_at") or 0, reverse=True)
    return assets[:limit]


def build_router(comfy_url) -> APIRouter:
    """comfy_url(uuid) -> base https URL for a running instance, or None."""
    router = APIRouter(prefix="/api/assets", tags=["assets"])

    async def base(uuid: str) -> str:
        url = await comfy_url(uuid)
        if not url:
            raise HTTPException(400, "instance has no ComfyUI endpoint (stopped or unknown)")
        return url

    @router.get("")
    async def list_assets(uuid: str, limit: int = Query(60, ge=1, le=500)) -> dict:
        url = await base(uuid)
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.get(f"{url}/history?max_items=200")
                response.raise_for_status()
                history = response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"ComfyUI history unavailable: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise HTTPException(502, "ComfyUI history returned non-JSON") from exc
        assets = collect_assets(history, limit)
        counts: dict[str, int] = {}
        for asset in assets:
            counts[asset["kind"]] = counts.get(asset["kind"], 0) + 1
        return {"uuid": uuid, "base": url, "total": len(assets), "kinds": counts, "assets": assets}

    @router.get("/file")
    async def asset_file(
        uuid: str,
        filename: str,
        subfolder: str = "",
        type: str = "output",  # noqa: A002 - mirrors ComfyUI's query name
    ) -> StreamingResponse:
        if type not in FILE_TYPES:
            raise HTTPException(400, f"type must be one of {', '.join(sorted(FILE_TYPES))}")
        if "/" in filename or "\\" in filename or ".." in filename or ".." in subfolder:
            raise HTTPException(400, "bad filename")
        url = await base(uuid)
        client = httpx.AsyncClient(timeout=None)
        request = client.build_request(
            "GET", f"{url}/view", params={"filename": filename, "subfolder": subfolder, "type": type}
        )
        try:
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            raise HTTPException(502, f"asset fetch failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            await response.aclose()
            await client.aclose()
            raise HTTPException(502, f"box answered {response.status_code} for {filename}")

        async def stream():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(
            stream(),
            media_type=response.headers.get("content-type", "application/octet-stream"),
            headers={"Cache-Control": "private, max-age=300"},
        )

    return router
