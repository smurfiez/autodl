#!/usr/bin/env python3
"""Checks the queued-deploy path with a stubbed AutoDL client (no real API calls).

    python3 webui/test_deploy_queue.py
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import app  # noqa: E402


def job(gpu: str = "vGPU-32GB") -> app.CreateRequest:
    return app.CreateRequest(
        gpu=gpu,
        data_centers=["westDC3", "beijingDC2"],
        app_uuid="app-image-test",
        image_id=313,
        image_name="MURAD-ComfyUI",
        version="2.0",
        version_id=962,
    )


async def case_stock_arrives_later() -> None:
    polls = {"n": 0}

    async def idle_by_dc(data_centers):
        polls["n"] += 1
        return {"westDC3": {"vGPU-32GB": 0}, "beijingDC2": {"vGPU-32GB": 0 if polls["n"] < 3 else 2}}

    async def call(path, body=None):
        assert "create/payg" in path
        assert body["instance_info"]["data_center_list"] == ["beijingDC2"], body["instance_info"]
        return {"code": "Success", "data": "pro-test123"}

    app.idle_by_dc, app.call = idle_by_dc, call
    app.jobs.clear()
    await app.deploy_when_ready("dep-a", job(), 3, 0)
    entry = app.jobs["dep-a"]
    assert entry["state"] == "created", entry
    assert entry["uuid"] == "pro-test123", entry
    assert entry["message"].endswith("in beijingDC2"), entry
    assert polls["n"] >= 3, polls


async def case_create_retries() -> None:
    attempts = {"n": 0}

    async def idle_by_dc(data_centers):
        return {"westDC3": {"vGPU-32GB": 1}}

    async def call(path, body=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise app.HTTPException(502, "当前算力规格暂无库存")
        return {"code": "Success", "data": "pro-test456"}

    app.idle_by_dc, app.call = idle_by_dc, call
    app.jobs.clear()
    await app.deploy_when_ready("dep-b", job(), 3, 0)
    entry = app.jobs["dep-b"]
    assert entry["state"] == "created", entry
    assert entry["uuid"] == "pro-test456", entry
    assert attempts["n"] == 2, attempts


async def case_gives_up() -> None:
    async def idle_by_dc(data_centers):
        return {"westDC3": {"vGPU-32GB": 0}, "beijingDC2": {"vGPU-32GB": 0}}

    async def call(path, body=None):  # must never run
        raise AssertionError("create called without stock")

    app.idle_by_dc, app.call = idle_by_dc, call
    app.jobs.clear()
    app.DEPLOY_TTL = 0.05
    await app.deploy_when_ready("dep-c", job(), 3, 0)
    entry = app.jobs["dep-c"]
    assert entry["state"] == "failed", entry


async def main() -> None:
    app.DEPLOY_POLL = 0.01
    for case in (case_stock_arrives_later, case_create_retries, case_gives_up):
        await case()
        print(f"ok {case.__name__}")
    app.DEPLOY_TTL = 6 * 3600


if __name__ == "__main__":
    asyncio.run(main())
