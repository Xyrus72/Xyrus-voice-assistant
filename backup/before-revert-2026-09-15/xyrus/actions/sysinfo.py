"""System information via psutil (§3.7). Returns data; never speaks (G10). Runs on T-exec."""
from __future__ import annotations

import os
import time

IGNORED_PROCS = {"system idle process", "system", "idle", "registry", "memory compression", "secure system"}


def battery() -> dict | None:
    import psutil
    b = psutil.sensors_battery()
    if b is None:
        return None
    return {"pct": int(round(b.percent)), "plugged": bool(b.power_plugged)}


def system_status() -> dict:
    """{"cpu": 12, "mem": 48, "battery": None | {"pct": 80, "plugged": True}, "uptime_s": 11400}"""
    import psutil
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory().percent
    return {"cpu": int(round(cpu)), "mem": int(round(mem)), "battery": battery(),
            "uptime_s": int(time.time() - psutil.boot_time())}


def disks(limit: int = 3) -> list[dict]:
    """[{"drive": "C", "free_gb": 120, "total_gb": 480, "pct": 75}] for up to `limit` fixed drives."""
    import psutil
    out = []
    for part in psutil.disk_partitions(all=False):
        opts = (part.opts or "").lower()
        if "cdrom" in opts or "removable" in opts or not part.fstype:
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        drive = (part.device or part.mountpoint).rstrip(":\\/")[:1].upper() or part.mountpoint
        out.append({"drive": drive, "free_gb": int(u.free // 1024 ** 3), "total_gb": int(u.total // 1024 ** 3),
                    "pct": int(round(u.percent))})
        if len(out) >= limit:
            break
    return out


def top_processes(n: int = 3, sample_s: float = 1.0) -> list[str]:
    """Names (no .exe, lowercase) of the processes using the most CPU over a 1 s sample, grouped by name."""
    import psutil
    own = os.getpid()
    procs = []
    for p in psutil.process_iter(["name"]):
        try:
            p.cpu_percent(None)
            procs.append(p)
        except Exception:
            continue
    time.sleep(sample_s)
    usage: dict[str, float] = {}
    for p in procs:
        try:
            name = (p.info.get("name") or "").lower()
            if not name or name in IGNORED_PROCS or p.pid in (0, own):
                continue
            stem = name[:-4] if name.endswith(".exe") else name
            usage[stem] = usage.get(stem, 0.0) + p.cpu_percent(None)
        except Exception:
            continue
    return [k for k, v in sorted(usage.items(), key=lambda kv: -kv[1]) if v > 0][:n]
