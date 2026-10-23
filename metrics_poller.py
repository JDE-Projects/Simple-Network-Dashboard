"""
Fetches and parses Node Exporter metrics from a device.
No SSH required — polls http://host:port/metrics directly.
"""

import asyncio
import ipaddress
import re
import socket
import time
import httpx
from typing import Optional

from config import METRICS_FETCH_DEADLINE, METRICS_MAX_RESPONSE_BYTES

# Filesystem types that are virtual/pseudo — not worth showing on disk panel
_EXCLUDE_FS = {
    "tmpfs", "devtmpfs", "squashfs", "overlay", "ramfs",
    "proc", "sysfs", "cgroup", "cgroup2", "devpts", "mqueue",
    "hugetlbfs", "pstore", "securityfs", "debugfs", "tracefs",
    "configfs", "fusectl", "bpf", "nsfs", "efivarfs",
}

# Network interfaces to skip (loopback, virtual, container, tunnel)
_EXCLUDE_IFACE = re.compile(
    r"^(lo|veth|docker|br-|virbr|tun|tap|sit|dummy|bond|team|flannel|cali|cilium)"
)

# Block devices whose mounts are never real storage (squashfs images, DSM
# service mounts under /tmp, snap loops)
_EXCLUDE_DEVICE = re.compile(r"^/dev/loop\d+")


# ---------------------------------------------------------------------------
# Metrics target validation
# ---------------------------------------------------------------------------

_MAX_HOST_LENGTH = 253


def is_valid_host_string(host: str) -> bool:
    """Reject an obviously malformed metrics target host string.

    Rejects empty or whitespace-only strings, strings containing whitespace
    or ASCII control characters, and strings longer than 253 characters.
    This is a sanity check, not full hostname DNS syntax validation.
    """
    if not host or not host.strip():
        return False
    if len(host) > _MAX_HOST_LENGTH:
        return False
    for ch in host:
        if ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    return True


def is_allowed_metrics_target(address: str) -> bool:
    """Return whether a parsed IP address is allowed as a metrics target.

    Reject-list, allow-the-rest: rejects loopback, link-local, multicast,
    unspecified, and reserved addresses (IPv4 and IPv6 alike). Everything
    else is allowed, including private ranges, ordinary public/routable
    addresses, IPv6, and carrier-grade NAT space (100.64.0.0/10).

    Deliberately does not use ip.is_private or ip.is_global, since those
    flags have reported CGNAT space inconsistently across Python versions.

    Raises ValueError if `address` does not parse as an IP address. Caller
    is responsible for hostname resolution, which is handled separately.
    """
    ip = ipaddress.ip_address(address)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    return True


# ---------------------------------------------------------------------------
# Prometheus text format parser
# ---------------------------------------------------------------------------

# One `name="value"` pair plus the comma or closing brace after it. The quoted
# value may hold any character except an unescaped quote, so spaces, commas,
# braces and `=` inside it stay part of the value. Regex matching runs in C,
# which matters because this parses every label on every poll.
_LABEL_PAIR_RE = re.compile(r'\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*([,}])')
_LABEL_BLOCK_END_RE = re.compile(r"\s*\}")
_METRIC_NAME_RE = re.compile(r"[^\s{]+")
_LABEL_ESCAPE_RE = re.compile(r"\\(.)")
_LABEL_ESCAPES = {"\\": "\\", '"': '"', "n": "\n"}


def _unescape_label(raw: str) -> str:
    """Undo the format's three escapes; any other backslash pair is kept as written."""
    return _LABEL_ESCAPE_RE.sub(lambda m: _LABEL_ESCAPES.get(m.group(1), m.group(0)), raw)


def _parse_label_block(line: str, start: int) -> Optional[tuple[dict, int]]:
    """Read the `{...}` block at `start`. Returns (labels, index after `}`),
    or None if the block is malformed or unterminated."""
    labels: dict = {}
    pos = start + 1
    while True:
        # Closes an empty block, or one with a trailing comma.
        end = _LABEL_BLOCK_END_RE.match(line, pos)
        if end:
            return labels, end.end()
        pair = _LABEL_PAIR_RE.match(line, pos)
        if not pair:
            return None
        name, raw, sep = pair.groups()
        labels[name] = _unescape_label(raw) if "\\" in raw else raw
        pos = pair.end()
        if sep == "}":
            return labels, pos


def _parse(text: str) -> dict:
    """Parse Prometheus text lines into metric entries, skipping malformed lines.
    Returns {metric_name: [{'labels': {}, 'value': float}, ...]}."""
    result: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        name_match = _METRIC_NAME_RE.match(line)
        if not name_match:
            continue
        name = name_match.group(0)

        labels = {}
        value_start = name_match.end()
        if value_start < len(line) and line[value_start] == "{":
            label_block = _parse_label_block(line, value_start)
            if label_block is None:
                continue
            labels, value_start = label_block

        if value_start >= len(line) or not line[value_start].isspace():
            continue
        fields = line[value_start:].split()
        if len(fields) not in (1, 2):
            continue
        try:
            value = float(fields[0])
            if len(fields) == 2:
                int(fields[1])
        except ValueError:
            continue

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
    out["_timestamp"] = time.time()

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
        if _EXCLUDE_DEVICE.match(device):
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

    if prev and "_net_raw" in prev and "_timestamp" in prev:
        elapsed = out["_timestamp"] - prev["_timestamp"]
        if elapsed > 0:
            rates = {}
            for iface, vals in net_raw.items():
                if iface in prev["_net_raw"]:
                    p = prev["_net_raw"][iface]
                    rx_kbs = max(0, vals["rx"] - p["rx"]) / elapsed / 1024
                    tx_kbs = max(0, vals["tx"] - p["tx"]) / elapsed / 1024
                    rates[iface] = {"rx_kbs": round(rx_kbs, 1), "tx_kbs": round(tx_kbs, 1)}
            out["network"] = rates

    return out


# ---------------------------------------------------------------------------
# Public entry point called by the background polling loop in main.py
# ---------------------------------------------------------------------------

async def _resolve_metrics_hostname(host: str, port: int) -> Optional[str]:
    """Resolve a hostname (not an IP literal) via the event loop's async
    resolver and return the first candidate address that passes
    is_allowed_metrics_target, or None if none do.

    Raises socket.gaierror if resolution fails or returns no results, so the
    caller can distinguish a DNS failure from a resolved-but-blocked target.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise socket.gaierror("no addresses returned")

    for info in infos:
        candidate = info[4][0]
        if is_allowed_metrics_target(candidate):
            return candidate
    return None


async def fetch_metrics(host: str, port: int = 9100, prev: Optional[dict] = None) -> dict:
    """Fetch Node Exporter metrics for one device. Returns an extracted dict.
    On any failure returns {'error': '<reason>'}."""
    if not is_valid_host_string(host):
        return {"error": "invalid host"}

    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False

    if is_literal:
        ip = host if is_allowed_metrics_target(host) else None
        if ip is None:
            return {"error": "blocked"}
    else:
        try:
            ip = await _resolve_metrics_hostname(host, port)
        except socket.gaierror:
            return {"error": "dns"}
        if ip is None:
            return {"error": "blocked"}

    host_part = f"[{ip}]" if ipaddress.ip_address(ip).version == 6 else ip
    url = f"http://{host_part}:{port}/metrics"
    async def _fetch() -> dict:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_length = resp.headers.get("Content-Length")
                if content_length is not None:
                    if not content_length.isascii() or not content_length.isdecimal():
                        return {"error": "bad response"}
                    if int(content_length) > METRICS_MAX_RESPONSE_BYTES:
                        return {"error": "response too large"}
                chunks = []
                received = 0
                async for chunk in resp.aiter_bytes():
                    received += len(chunk)
                    if received > METRICS_MAX_RESPONSE_BYTES:
                        return {"error": "response too large"}
                    chunks.append(chunk)
                text = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                return extract_metrics(_parse(text), prev)

    try:
        # The deadline covers the whole fetch, so a reply that trickles in
        # never outlasts it.
        return await asyncio.wait_for(_fetch(), METRICS_FETCH_DEADLINE)
    except httpx.ConnectError:
        return {"error": "unreachable"}
    except httpx.TimeoutException:
        return {"error": "timeout"}
    except asyncio.TimeoutError:
        return {"error": "timeout"}
    except Exception as e:
        if isinstance(e, httpx.HTTPStatusError):
            return {"error": f"http status {e.response.status_code}"}
        return {"error": f"failed ({type(e).__name__})"}
