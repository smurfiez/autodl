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

## What each file does

| file | role |
|---|---|
| `tools/create_autodl.py` | `create/payg` for app 313 (MURAD-ComfyUI v2.0), polls until `running`, prints ComfyUI host + ssh, then records the instance in `servers.json`. |
| `tools/servers.py` | Rewrites `servers.json` from `GET /instance/pro`: uuid, status, gpu, region, price, comfy/panel URLs, ssh host and port. Passwords are deliberately excluded. |
| `tools/release_autodl.py` | `power_off` → wait `shutdown` → `release` → refresh `servers.json`. Destructive; needs `--confirm`. |
| `tools/patch_murad_box.sh` | Runs on the box: patches `server.py` so the panel's RAM sentinel stops unloading models between prompts, then makes sure ComfyUI is serving (panel restart, falling back to the image's own `start-comfyui.sh`). Prints `SERVING:` on success. |
| `tools/patch_murad_box.exp` | Drives the above over SSH with `SSH_HOST` / `SSH_PORT` / `PW` / `PANEL` env vars. |

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
