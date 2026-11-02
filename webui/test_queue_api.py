#!/usr/bin/env python3
"""Contract checks for the Queues API, on fixture ComfyUI payloads (no network).

The fixtures mirror payloads captured live from the box: /queue rows are
`[number, prompt_id, prompt, extra, outputs]`, /history values carry `status`
messages and an `outputs` map whose lists are named per node (`images`, `gifs`,
`audio`).

    .venv/bin/python webui/test_queue_api.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import comfy_queue as queue_api

RUNNING_ROW = [
    12,
    "f06151a0-e8e7-496e-b33f-5045bbe55191",
    {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-dev.safetensors", "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "clip_l.safetensors"}},
        "3": {"class_type": "LoraLoader", "inputs": {"lora_name": "film_grain.safetensors"}},
        "45": {"class_type": "SaveImage", "inputs": {"filename_prefix": "openmontage/hero_angela_windup", "images": ["7", 0]}},
        "46": {"class_type": "SaveAnimatedWEBP", "inputs": {"filename_prefix": "openmontage/"} , },
    },
    {"client_id": "103d1838", "create_time": 1793637430804},
    ["45"],
]

PENDING_ROW = [
    13,
    "1aca360e-a8d7-4673-bdb4-a52077228fdb",
    {
        "1": {"class_type": "OmniVoiceModelLoader", "inputs": {"device": "cuda:0"}},
        "3": {"class_type": "SaveAudioMP3", "inputs": {"filename_prefix": "openmontage/longan_vo_s8", "quality": "320k"}},
    },
    {"client_id": "0e89736b", "create_time": 1793637436614},
    ["3"],
]

SUCCESS_JOB = {
    "status": {
        "status_str": "success",
        "messages": [
            ["execution_start", {"prompt_id": "fccc", "timestamp": 1793636870161}],
            ["execution_cached", {"nodes": [], "prompt_id": "fccc", "timestamp": 1793636870177}],
            ["execution_success", {"prompt_id": "fccc", "timestamp": 1793636975090}],
        ],
    },
    "outputs": {
        "8": {
            "images": [
                {
                    "filename": "hero_angela_windup_00001_.png",
                    "subfolder": "openmontage",
                    "type": "output",
                    "metadata": {"kind": "image", "width": 1344, "height": 768},
                }
            ]
        }
    },
}

VIDEO_JOB = {
    "status": {
        "status_str": "success",
        "messages": [
            ["execution_start", {"prompt_id": "0610", "timestamp": 1793636975966}],
            ["execution_success", {"prompt_id": "0610", "timestamp": 1793637079050}],
        ],
    },
    "outputs": {
        "24": {
            "gifs": [
                {
                    "filename": "longan_s03a_00002-audio.mp4",
                    "subfolder": "LTX2.5",
                    "type": "output",
                    "format": "video/h264-mp4",
                    "metadata": {"kind": "video", "width": 1344, "height": 768},
                }
            ]
        }
    },
}

ERROR_JOB = {
    "status": {
        "status_str": "error",
        "messages": [
            ["execution_start", {"prompt_id": "6dc9", "timestamp": 1793636982965}],
            ["execution_error", {"prompt_id": "6dc9", "timestamp": 1793636999000, "exception_message": "CUDA OOM"}],
        ],
    },
    "outputs": {},
}

AUDIO_JOB = {
    "status": {"status_str": "success", "messages": []},
    "outputs": {"3": {"audio": [{"filename": "longan_vo_s8_00001.mp3", "subfolder": "openmontage", "type": "output"}]}},
}


def case_queue_contract() -> None:
    running, pending = queue_api.summarize_queue(
        {"queue_running": [RUNNING_ROW], "queue_pending": [PENDING_ROW]}
    )
    assert len(running) == 1 and len(pending) == 1, (running, pending)
    item = running[0]
    assert set(item) == {"id", "number", "started_at", "label", "models", "nodes"}, item
    assert item["id"] == "f06151a0-e8e7-496e-b33f-5045bbe55191", item
    assert item["number"] == 12, item
    assert item["started_at"] == 1793637430804, item
    assert item["label"] == "openmontage/hero_angela_windup", item
    assert item["models"] == ["flux1-dev.safetensors", "film_grain.safetensors"], item
    assert item["nodes"] == 5, item

    waiting = pending[0]
    assert set(waiting) == {"id", "number", "label", "models", "nodes"}, waiting
    assert waiting["id"] == "1aca360e-a8d7-4673-bdb4-a52077228fdb", waiting
    assert waiting["label"] == "openmontage/longan_vo_s8", waiting
    assert waiting["models"] == [], waiting
    assert waiting["nodes"] == 2, waiting

    # junk rows and a row without create_time must not raise
    running, pending = queue_api.summarize_queue(
        {"queue_running": [["x"], [9, "id-9", {"7": {"class_type": "KSampler", "inputs": {}}}, {}, []]], "queue_pending": None}
    )
    assert [row["id"] for row in running] == ["id-9"], running
    assert running[0]["started_at"] is None, running
    assert running[0]["nodes"] == 1, running


def case_label_rules() -> None:
    assert queue_api.prompt_label({"9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "a/b/"}}}) == "a/b"
    # a save node outside the documented set still names its output
    assert queue_api.prompt_label({"9": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": "LTX2.5/longan"}}}) == "LTX2.5/longan"
    # no prefix: first model-ish input
    assert queue_api.prompt_label({"1": {"class_type": "UNETLoader", "inputs": {"unet_name": "flux1-dev.safetensors"}}}) == "flux1-dev.safetensors"
    # nothing but plumbing: class_type of the last node
    assert queue_api.prompt_label({"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "hi"}}, "2": {"class_type": "KSampler", "inputs": {}}}) == "KSampler"
    assert queue_api.prompt_label({}) == "workflow"
    prompt = {str(n): {"class_type": "LoraLoader", "inputs": {"lora_name": f"lora{n}.safetensors"}} for n in range(6)}
    prompt["9"] = {"class_type": "LoraLoader", "inputs": {"lora_name": "lora0.safetensors"}}
    models = queue_api.prompt_models(prompt)
    assert models == [f"lora{n}.safetensors" for n in range(4)], models


def case_history_contract() -> None:
    finished = queue_api.summarize_history(
        {
            "fccc": SUCCESS_JOB,
            "0610": VIDEO_JOB,
            "6dc9": ERROR_JOB,
            "0003": AUDIO_JOB,
        }
    )
    assert [row["id"] for row in finished] == ["0610", "6dc9", "fccc", "0003"], finished
    for row in finished:
        assert set(row) == {"id", "status", "started_at", "ended_at", "files", "file_count"}, row
        assert row["file_count"] == len(row["files"]), row

    by_id = {row["id"]: row for row in finished}
    assert by_id["fccc"]["status"] == "success", by_id["fccc"]
    assert by_id["fccc"]["started_at"] == 1793636870161, by_id["fccc"]
    assert by_id["fccc"]["ended_at"] == 1793636975090, by_id["fccc"]
    file = by_id["fccc"]["files"][0]
    assert set(file) == {"filename", "subfolder", "type", "kind", "width", "height"}, file
    assert file == {
        "filename": "hero_angela_windup_00001_.png",
        "subfolder": "openmontage",
        "type": "output",
        "kind": "image",
        "width": 1344,
        "height": 768,
    }, file

    assert by_id["0610"]["files"][0]["kind"] == "video", by_id["0610"]
    assert by_id["6dc9"]["status"] == "error", by_id["6dc9"]
    assert by_id["6dc9"]["files"] == [] and by_id["6dc9"]["file_count"] == 0, by_id["6dc9"]
    # no start/success message: status falls back to status_str, stamps stay null
    assert by_id["0003"]["started_at"] is None and by_id["0003"]["ended_at"] is None, by_id["0003"]
    assert by_id["0003"]["status"] == "success", by_id["0003"]
    assert by_id["0003"]["files"][0]["kind"] == "audio", by_id["0003"]
    assert queue_api.summarize_history({}, limit=5) == []


def case_box_shape() -> None:
    row = {"uuid": "pro-abc", "name": "murad-01"}
    box = queue_api.box_header(row)
    assert set(box) == {"uuid", "name", "comfy", "error", "version", "counts", "running", "pending", "finished"}, box
    assert box["uuid"] == "pro-abc" and box["name"] == "murad-01", box
    assert box["error"] is None and box["comfy"] is None and box["version"] is None, box
    assert box["counts"] == {"running": 0, "pending": 0, "finished": 0}, box
    assert box["running"] == box["pending"] == box["finished"] == [], box

    failed = queue_api.box_header(row, error="comfy Timeout")
    assert failed["error"] == "comfy Timeout", failed
    assert failed["counts"] == {"running": 0, "pending": 0, "finished": 0}, failed
    assert failed["running"] == failed["pending"] == failed["finished"] == [], failed

    filled = queue_api.fill_box(
        queue_api.box_header(row),
        {"queue_running": [RUNNING_ROW], "queue_pending": [PENDING_ROW, PENDING_ROW]},
        {"fccc": SUCCESS_JOB, "0610": VIDEO_JOB},
        {"system": {"comfyui_version": "0.39.0"}},
    )
    assert filled["version"] == "0.39.0", filled
    assert filled["counts"] == {"running": 1, "pending": 2, "finished": 2}, filled
    assert filled["running"][0]["label"] == "openmontage/hero_angela_windup", filled

    bare = queue_api.fill_box(queue_api.box_header(row), {}, {}, None)
    assert bare["version"] is None and bare["counts"]["finished"] == 0, bare


def main() -> None:
    for case in (case_queue_contract, case_label_rules, case_history_contract, case_box_shape):
        case()
        print(f"ok {case.__name__}")


if __name__ == "__main__":
    main()
