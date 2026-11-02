"""Print this AutoDL box's health as one JSON object.

Sent over SSH stdin as `python3 -` by webui/app.py and never stored on the
box. Python 3.8+, stdlib only, read-only.
"""

COLLECTOR = r'''
import json, os, shutil, subprocess, time

def sh(*cmd, timeout=8):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""

def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def text(path):
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return ""

out = {"time": time.time(), "gpus": [], "disks": {}}
query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
for line in sh("nvidia-smi", "--query-gpu=" + query, "--format=csv,noheader,nounits").splitlines():
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 7:  # "No devices were found" and other non-table lines
        continue
    name, *values = parts
    util, used, total, temp, power, limit = map(num, values)
    out["gpus"].append({"name": name, "util": util, "mem_used": used, "mem_total": total,
                        "temp": temp, "power": power, "power_limit": limit})

for path in ("/", "/root/autodl-tmp", "/root/autodl-fs"):
    if path == "/" or os.path.ismount(path):
        try:
            usage = shutil.disk_usage(path)
        except OSError:  # hung or vanished mount must not kill the whole report
            continue
        out["disks"][path] = {"used": usage.used, "total": usage.total}

load = text("/proc/loadavg").split()
if load:
    out["load"] = [float(value) for value in load[:3]]
out["cpus"] = os.cpu_count()
quota = text("/sys/fs/cgroup/cpu.max").split()  # v2: "<quota> <period>" or "max <period>"
if len(quota) == 2 and quota[0].isdigit() and int(quota[1]):
    out["cpus"] = max(1, round(int(quota[0]) / int(quota[1])))

uptime = text("/proc/uptime").split()
if uptime:
    out["uptime_s"] = float(uptime[0])

meminfo = {}
for line in text("/proc/meminfo").splitlines():
    if ":" not in line:
        continue
    key, value = line.split(":", 1)
    parts = value.split()
    if parts and parts[0].isdigit():
        meminfo[key] = int(parts[0]) * 1024
if {"MemTotal", "MemAvailable"} <= meminfo.keys():
    out["mem"] = {"used": meminfo["MemTotal"] - meminfo["MemAvailable"], "total": meminfo["MemTotal"]}

# a container sees the host's /proc/meminfo; prefer the cgroup limit when finite
limit, current = text("/sys/fs/cgroup/memory.max").strip(), text("/sys/fs/cgroup/memory.current").strip()
if not (limit.isdigit() and current.isdigit()):
    limit = text("/sys/fs/cgroup/memory/memory.limit_in_bytes").strip()
    current = text("/sys/fs/cgroup/memory/memory.usage_in_bytes").strip()
if limit.isdigit() and current.isdigit() and int(limit) < 1 << 50:
    out["mem"] = {"used": int(current), "total": int(limit)}
if {"SwapTotal", "SwapFree"} <= meminfo.keys():
    out["swap"] = {"used": meminfo["SwapTotal"] - meminfo["SwapFree"], "total": meminfo["SwapTotal"]}

processes = sh("bash", "-c", "ps -eo comm,pcpu --sort=-pcpu --no-headers | head -5")
out["top"] = [line.strip() for line in processes.splitlines() if line.strip()]

print(json.dumps(out))
'''
