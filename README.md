# autodl — instance tooling for AutoDL.Art

Copy of the provisioning code from `~/Projects/wzry`, minus that repo's sprite
guards. Token is read from `~/.config/adl/token` (raw JWT, no `Bearer`).

```bash
# 1. create a MURAD-ComfyUI (app 313) pay-as-you-go box and wait for `running`
python3 tools/create_autodl.py --gpu "vGPU-32GB" --data-center westDC3

# 2. patch the panel RAM sentinel and START ComfyUI (SERVING: is the only success line)
SSH_PORT=29399 SSH_HOST=connect.weste.seetacloud.com PW='<password>' \
  PANEL='https://uu1241700-<uuid>.weste.seetacloud.com:8443' tools/patch_murad_box.exp

# 3. instance list, also written to ../servers.json
python3 tools/servers.py

# 4. end the meter (power_off alone keeps disk billing)
python3 tools/release_autodl.py --uuid <uuid> --confirm
```

## Web console

Local FastAPI page for the same account: pro instance list, deploy form,
power/release, per-box health.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd webui && ../.venv/bin/python -m uvicorn app:app --port 8765
# or: python webui/app.py   (binds 127.0.0.1:8765)
```

Open <http://localhost:8765>. The token is read from `ADL_TOKEN` or
`~/.config/adl/token` (raw JWT) and attached server-side; it never reaches
the browser.

| endpoint | role |
|---|---|
| `GET /` | `webui/index.html`. |
| `GET /api/config` | whether a token was found and the allowed data centers. |
| `GET /api/apps?q=&sort=stars|name|updated&limit=&offset=` | market catalog from `market.db`: id, name, latest version, stars, deployable version count. |
| `GET /api/apps/{app_id}` | that app plus every version with `image_status=success` / `review_status=pass` (framework, CUDA, size, image uuid, start command). |
| `GET /api/assets?uuid=&limit=` | ComfyUI `/history` flattened to output files, newest first (filename, subfolder, kind, dimensions, timestamp). |
| `GET /api/assets/file?uuid=&filename=&subfolder=&type=` | streams one output file from the box (host restricted to `*.seetacloud.com`, no path traversal). |
| `GET /api/gpus?dc=westDC3&dc=beijingDC2` | GPU slots with live idle counts per data center (`idle_by_dc`, `idle` = sum); unknown names are appended. |
| `GET /api/instances` | normalized pro instances (newest first) and in-flight deploy/release jobs. |
| `POST /api/instances` | `create/payg`; tries each selected data center richest-first, resolves the payg spec id, disk = (30 + extra GB). With `wait: true` a failed create becomes a polling job. |
| `DELETE /api/jobs/{id}` | cancel a queued deploy job. |
| `POST /api/instances/{uuid}/power_on` | power on. |
| `POST /api/instances/{uuid}/power_off` | power off. |
| `POST /api/instances/{uuid}/release` | needs `{"confirm": true}`; runs `power_off` → wait `shutdown` → `release` as a background job. |
| `GET /api/health` | per-box host stats and ComfyUI state cached by the poller. |

- Deploy form: GPU slot (idle count live per data center), one or both of
  `westDC3` / `beijingDC2`, optional name, extra system disk (0-500 GB).
- Application and version are chosen from `market.db` (250 apps): a filter box
  narrows the app list, the version list only shows deployable versions, and
  the info line shows the image uuid and start command that will be sent.
  Without `market.db` the form cannot resolve an image and `/api/apps` answers
  503; rebuild the DB with `python3 tools/market.py sync`.
- **Browse** tab: card grid of the whole catalog (logo, English name and
  description, tags, stars, version count), server-side search, sort by
  stars/name/updated, load-more paging, and a detail dialog with every
  deployable version. "Select for deploy" fills the Deploy form.
- **Assets** tab: generated files of a running ComfyUI box, newest first —
  image/video/audio tiles served through the proxy, kind filter, click to open
  the original. Refreshes only on demand, never on a timer.
- Every slot is selectable. When the chosen card has no idle GPU anywhere, the
  form still queues it: a background job re-polls `gpu_type` every 15 s for up
  to 6 h and creates the instance in the first data center that frees a slot.
  Queued jobs appear under Instances with a Cancel button.
- Page refreshes instances and health every 5 s; GPU inventory every 60 s.
- Backend polls every 10 s in a background task, four boxes at a time: host
  stats over password SSH (paramiko, `collector.py` piped to `python3 -`) and
  ComfyUI `/queue` + `/system_stats` on the `:6006` domain.
- Health tiles: GPU util, VRAM, temp, power, RAM, disk free, load. Warns at
  temp ≥ 85 °C, disk ≥ 90%, swap > 50%, VRAM > 95%, load > 2× CPUs, and host
  stats older than 60 s.
- Release is two-step because `release` needs `shutdown`, and `power_off`
  alone still bills disk.
- The console drives the older `/api/v1/adl_server/*` endpoints, not the
  `/api/v1/adl_dev/*` app API from <https://www.autodl.art/docs/app_api/>.

## What each file does

| file | role |
|---|---|
| `tools/create_autodl.py` | `create/payg` for app 313 (MURAD-ComfyUI v2.0), polls until `running`, prints ComfyUI host + ssh, then records the instance in `servers.json`. |
| `tools/servers.py` | Rewrites `servers.json` from `GET /instance/pro`: uuid, status, gpu, region, price, comfy/panel URLs, ssh host and port. Passwords are deliberately excluded. |
| `tools/market.py` | Crawls the app marketplace (`GET /application` + `GET /application/square/detail`) into `market.db` (SQLite: `applications`, `versions`) and queries it: `find`, `show`, `sql`. `fetch --pages` + `load` split the crawl across parallel shards. |
| `tools/market_test.py` | Offline check of `market.py` against a fixture in a temp DB (`ADL_MARKET_DB`): load idempotency, tags, latest-version join, every `find` filter. Run `python3 tools/market_test.py`. |
| `tools/release_autodl.py` | `power_off` → wait `shutdown` → `release` → refresh `servers.json`. Destructive; needs `--confirm`. |
| `tools/patch_murad_box.sh` | Runs on the box: patches `server.py` so the panel's RAM sentinel stops unloading models between prompts, then makes sure ComfyUI is serving (panel restart, falling back to the image's own `start-comfyui.sh`). Prints `SERVING:` on success. |
| `tools/patch_murad_box.exp` | Drives the above over SSH with `SSH_HOST` / `SSH_PORT` / `PW` / `PANEL` env vars. |
| `webui/app.py` | FastAPI console: instance list, deploy form backed by `market.db`, power/release/deploy jobs, and the 10 s health poller (SSH + ComfyUI). Serves `index.html` and `/api/*`; the token stays server-side. |
| `webui/index.html` | Vanilla-JS single page: deploy form, instance table, health tiles. Polls instances/health every 5 s, GPU inventory every 60 s. |
| `webui/assets.py` | APIRouter for the ComfyUI output gallery: flattens `/history`, proxies `/view` bytes. |
| `webui/collector.py` | Read-only stdlib script piped over SSH stdin: GPU (`nvidia-smi`), disks, load, memory, swap and top processes as one JSON object. |

## Marketplace image database

`market.db` is a disposable index of every published image (250 apps at the
time of writing). Rebuild it with `sync`; query it with `find`, `show` or raw
SQL.

```bash
python3 tools/market.py sync                     # ~250 detail calls
python3 tools/market.py find comfy --sort star
python3 tools/market.py find --framework PyTorch --cuda 13.0 --limit 10
python3 tools/market.py show 273
python3 tools/market.py sql "select name, star_num from applications order by star_num desc limit 5"
```

`translations` caches English `name`, `describe` and `tags` per app so the UI
never calls a translator at page time. The source of truth is
`tools/translations_en.json`, committed and hand-reviewed; `sync` copies it
into the DB and needs no network. Only apps missing from that file fall back to
the Google endpoint, and only with `--online`.

```bash
python3 tools/translate_market.py sync              # import the curated file
python3 tools/translate_market.py sync --online     # + translate apps not in it
python3 tools/translate_market.py sync --refresh    # rewrite rows from the file
python3 tools/translate_market.py show 313
```

New catalog entries show Chinese until their English is added to
`translations_en.json`; `sync` lists the app ids that are still missing.

`applications` holds name, describe, author_note, readme, tags, stars,
downloads and the latest version label; `versions` holds one row per image
version (framework, framework_v, cuda_v, chip_corp, cpu_arch, image_size,
private_image_uuid, image_status). `find --filter` joins the latest version,
so framework/cuda filters describe the current image, not older ones.

## Facts worth keeping

- GPU slot ids: `3` = 4080(S)-32G `vGPU-32GB` ¥1.77/h, `2` = 3090-48G `vGPU-48GB-350W` ¥1.87/h, `5` = 4090-48G ¥3.03/h, `1` = 5090 ¥3.03/h, `4` = PRO 6000 96G ¥7.35/h, `6` = H800 ¥10.50/h.
- Only `westDC3` and `beijingDC2` serve pro machines. Idle counts churn by the minute; poll `machine_pro/gpu_type`, then create immediately.
- A create can still fail with `当前算力规格暂无库存` when the card vanishes between poll and order.
- App 313 image: `app-image-8a23e8ee9e`, `image_id 313`, version `2.0`, `current_version_id 962`; disk default `32212254720` bytes (30 GB base, no expansion).
- `power_off` does not stop billing. `release` requires `shutdown` first (`请在实例关机状态下执行释放操作` otherwise).
- A fresh box needs ~60-90 s before `:8443` answers; `/api/comfy/status` reports `starting` until then.
- Patching `server.py` alone changes nothing until ComfyUI restarts — that is why `patch_murad_box.sh` starts it and waits for `/system_stats`.

Deeper notes (sentinel diagnosis, model layout, download pitfalls) live in
`~/Projects/wzry/autodl.md` and `~/Projects/wzry/comfy.md`.
