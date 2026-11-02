#!/bin/bash
# Patch the MURAD panel RAM sentinel out of ComfyUI, then make sure ComfyUI is
# actually serving. Run on the instance: bash patch_murad_box.sh <panel_base>
#
# Patching only edits server.py, so a box that is patched but not (re)started
# keeps rendering at the slow ~18 s/prompt rate. This script closes that gap:
#   already patched + serving  -> leave the process alone
#   fresh patch                -> restart through the panel, then poll
#   panel dead / still booting -> start it with the image's own start script
set -u
PANEL="${1:-}"
mkdir -p /root/ComfyUI/output/VNCCS/Characters
cd /root/ComfyUI || exit 1

serving() { curl -sk -m 10 http://127.0.0.1:6006/system_stats 2>/dev/null | grep -q comfyui_version; }

patched=0
if grep -A3 '@routes.post("/free")' server.py | grep -q 'unload_models = False'; then
  echo "ALREADY_PATCHED"
else
  cp -n server.py server.py.bak
  python3 - <<'EOF' || exit 1
p = "/root/ComfyUI/server.py"
s = open(p).read()
old = '''            unload_models = json_data.get("unload_models", False)
            free_memory = json_data.get("free_memory", False)'''
new = '''            unload_models = False
            free_memory = False'''
assert s.count(old) == 1, s.count(old)
open(p, "w").write(s.replace(old, new))
print("PATCHED")
EOF
  grep -A3 '@routes.post("/free")' server.py | grep -E 'unload_models|free_memory'
  patched=1
fi

wait_serving() {  # $1 = seconds
  for _ in $(seq 1 "$1"); do
    if serving; then
      echo "SERVING: $(curl -sk -m 10 http://127.0.0.1:6006/system_stats 2>/dev/null | head -c 120)"
      return 0
    fi
    sleep 5
  done
  return 1
}

if [ "$patched" -eq 0 ] && serving; then
  echo "SERVING_ALREADY (patched process already up, left untouched)"
  exit 0
fi

if [ -n "$PANEL" ]; then
  echo "RESTARTING via $PANEL/api/comfy/restart"
  curl -sk -m 30 -X POST "$PANEL/api/comfy/restart" >/dev/null 2>&1 || true
fi

wait_serving 36 && exit 0

# Panel was dead or still booting when the restart was posted: start it ourselves.
if [ -f /root/MURAD_panel/scripts/start-comfyui.sh ]; then
  echo "STARTING /root/MURAD_panel/scripts/start-comfyui.sh"
  setsid bash /root/MURAD_panel/scripts/start-comfyui.sh >>/root/murad_comfy_start.log 2>&1 </dev/null &
elif [ -f /root/MURAD_panel/scripts/start-MURAD-panel.sh ]; then
  echo "STARTING /root/MURAD_panel/scripts/start-MURAD-panel.sh"
  setsid bash /root/MURAD_panel/scripts/start-MURAD-panel.sh >>/root/murad_panel_start.log 2>&1 </dev/null &
fi

wait_serving 96 && exit 0
echo "NOT_SERVING: last log lines:" >&2
tail -5 /root/murad_comfy_start.log /root/murad_panel_start.log 2>/dev/null >&2
exit 1
