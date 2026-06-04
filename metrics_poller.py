"""
Fetches and parses Node Exporter metrics from a device.
No SSH required — polls http://host:port/metrics directly.
"""

import re
import time
import httpx
from typing import Optional

POLL_INTERVAL = 10  # seconds between polls

# Filesystem types that are virtual/pseudo — not worth showing on disk panel
_EXCLUDE_FS = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "ramfs",
    "proc", "sysfs", "cgroup", "cgroup2", "devpts", "mqueue",
    "hugetlbfs", "pstore", "securityfs", "debugfs", "tracefs",
    "configfs", "fusectl", "bpf", "nsfs", "efivarfs",
}

# Network interfaces to skip (loopback, virtual, container, tunnel)
_EXCLUDE_IFACE = re.compile(
    r"^(lo|veth|docker|br-|virbr|tun|tap|dummy|bond|team|flannel|cali|cilium)"
)


# ---------------------------------------------------------------------------
# Prometheus text format parser
# ---------------------------------------------------------------------------

def _parse(text: str) -> dict:
    """Minimal Prometheus exposition format parser.
    Returns {metric_name: [{'labels': {}, 'value': float}, ...]}
    """
    result: dict = {}
    label_re = re.compile(r'(\w+)="([^"]*)"')
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        metric_part = parts[0]
        try:
            value = float(parts[1])
        except (ValueError, IndexError):
            continue
        if "{" in metric_part:
            name, rest = metric_part.split("{", 1)
            labels = dict(label_re.findall(rest))
        else:
            name = metric_part
            labels = {}
        result.setdefault(name, []).append({"labels": labels, "value": value})
    return result


def _scalar(parsed: dict, name: str) -> Optional[float]:
    entries = parsed.get(name, [])
    return entries[0]["value"] if entries else None


# ---------------------------------------------------------------------------
# Metric extraction (with delta-based CPU and network rates)
# ---------------------------------------------------------------------------

def extract_metrics(parsed: dict, prev: Optional[dict]) -> dict:
    out: dict = {}

    # --- System info ---
    uname_entries = parsed.get("node_uname_info", [])
    if uname_entries:
        lbl = uname_entries[0].get("labels", {})
        out["hostname"] = lbl.get("nodename", "")
        out["kernel"]   = lbl.get("release", "")
        out["arch"]     = lbl.get("machine", "")

    boot = _scalar(parsed, "node_boot_time_seconds")
    if boot:
        s = int(time.time() - boot)
        d, r = divmod(s, 86400)
        h, r = divmod(r, 3600)
        m = r // 60
        if d:
            out["uptime"] = f"{d}d {h}h {m}m"
        elif h:
            out["uptime"] = f"{h}h {m}m"
        else:
            out["uptime"] = f"{m}m"

    # --- Load averages ---
    for key, metric in [("load1", "node_load1"), ("load5", "node_load5"), ("load15", "node_load15")]:
        v = _scalar(parsed, metric)
        if v is not None:
            out[key] = round(v, 2)

    # --- CPU ---
    cpu_entries = parsed.get("node_cpu_seconds_total", [])
    if cpu_entries:
        total_now = sum(e["value"] for e in cpu_entries)
        idle_now  = sum(e["value"] for e in cpu_entries if e["labels"].get("mode") == "idle")
        out["cpu_count"] = len({e["labels"].get("cpu") for e in cpu_entries})
        out["_cpu_raw"]  = {"total": total_now, "idle": idle_now}
        if prev and "_cpu_raw" in prev:
            dt = total_now - prev["_cpu_raw"]["total"]
            di = idle_now  - prev["_cpu_raw"]["idle"]
            if dt > 0:
                out["cpu_percent"] = round((1 - di / dt) * 100, 1)

    # --- Memory ---
    mem_total = _scalar(parsed, "node_memory_MemTotal_bytes")
    mem_avail = _scalar(parsed, "node_memory_MemAvailable_bytes")
    if mem_total and mem_avail is not None:
        used = mem_total - mem_avail
        out["ram_total_gb"] = round(mem_total / 1024**3, 2)
        out["ram_used_gb"]  = round(used       / 1024**3, 2)
        out["ram_percent"]  = round(used / mem_total * 100, 1)

    swap_total = _scalar(parsed, "node_memory_SwapTotal_bytes")
    swap_free  = _scalar(parsed, "node_memory_SwapFree_bytes")
    if swap_total and swap_total > 0 and swap_free is not None:
        swap_used = swap_total - swap_free
        out["swap_total_gb"] = round(swap_total / 1024**3, 2)
        out["swap_used_gb"]  = round(swap_used  / 1024**3, 2)
        out["swap_percent"]  = round(swap_used / swap_total * 100, 1)

    # --- Disk ---
    avail_map = {
        (e["labels"].get("device"), e["labels"].get("mountpoint")): e["value"]
        for e in parsed.get("node_filesystem_avail_bytes", [])
    }
    seen = set()
    disks = []
    for e in parsed.get("node_filesystem_size_bytes", []):
        lbl = e["labels"]
        fstype     = lbl.get("fstype", "")
        mountpoint = lbl.get("mountpoint", "")
        device     = lbl.get("device", "")
        if fstype in _EXCLUDE_FS or mountpoint in seen:
            continue
        seen.add(mountpoint)
        size  = e["value"]
        avail = avail_map.get((device, mountpoint), 0)
        if size <= 0:
            continue
        used = size - avail
        disks.append({
            "mount":    mountpoint,
            "device":   device,
            "total_gb": round(size / 1024**3, 1),
            "used_gb":  round(used  / 1024**3, 1),
            "percent":  round(used / size * 100, 1),
        })
    disks.sort(key=lambda d: (0 if d["mount"] == "/" else 1, d["mount"]))
    out["disks"] = disks

    # --- Temperature ---
    temps = []
    for e in parsed.get("node_thermal_zone_temp", []):
        lbl = e["labels"]
        label = lbl.get("type") or f"zone{lbl.get('zone', '?')}"
        c = round(e["value"], 1)
        if c > 0:
            temps.append({"label": label, "celsius": c})

    # hwmon (lm-sensors): use _input sensors only to avoid duplicates
    for e in parsed.get("node_hwmon_temp_celsius", []):
        lbl    = e["labels"]
        sensor = lbl.get("sensor", "")
        if sensor and not sensor.endswith("_input") and not sensor.endswith("temp"):
            # Skip max/crit/alarm sensors — only want the live reading
            if any(sensor.endswith(s) for s in ("_max", "_crit", "_alarm", "_min", "_emergency")):
                continue
        label = lbl.get("name") or sensor or lbl.get("chip", "")
        c = round(e["value"], 1)
        if c > 0:
            temps.append({"label": label, "celsius": c})

    out["temps"] = temps

    # --- Network (delta-based KB/s rates) ---
    rx_map = {e["labels"].get("device"): e["value"] for e in parsed.get("node_network_receive_bytes_total",  [])}
    tx_map = {e["labels"].get("device"): e["value"] for e in parsed.get("node_network_transmit_bytes_total", [])}
    ifaces = (set(rx_map) | set(tx_map)) - {None}
    net_raw = {}
    for iface in ifaces:
        if _EXCLUDE_IFACE.match(iface):
            continue
        net_raw[iface] = {"rx": rx_map.get(iface, 0), "tx": tx_map.get(iface, 0)}
    out["_net_raw"] = net_raw

    if prev and "_net_raw" in prev:
        rates = {}
        for iface, vals in net_raw.items():
            if iface in prev["_net_raw"]:
                p = prev["_net_raw"][iface]
                rx_kbs = max(0, vals["rx"] - p["rx"]) / POLL_INTERVAL / 1024
                tx_kbs = max(0, vals["tx"] - p["tx"]) / POLL_INTERVAL / 1024
                rates[iface] = {"rx_kbs": round(rx_kbs, 1), "tx_kbs": round(tx_kbs, 1)}
        out["network"] = rates

    return out


# ---------------------------------------------------------------------------
# Public entry point called by the background polling loop in main.py
# ---------------------------------------------------------------------------

async def fetch_metrics(host: str, port: int = 9100, prev: Optional[dict] = None) -> dict:
    """Fetch Node Exporter metrics for one device. Returns an extracted dict.
    On any failure returns {'error': '<reason>'}."""
    url = f"http://{host}:{port}/metrics"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return extract_metrics(_parse(resp.text), prev)
    except httpx.ConnectError:
        return {"error": "unreachable"}
    except httpx.TimeoutException:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}
