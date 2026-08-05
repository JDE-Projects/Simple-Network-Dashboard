#!/usr/bin/env python3
"""A stand-in for Node Exporter, used only when generating README screenshots.

Serves Prometheus text on /metrics for an invented machine. The cumulative
counters are derived from elapsed time rather than request count, so the
delta-based CPU and network rates in metrics_poller.py work out to exactly the
intended figures no matter how the poll timing drifts.

Everything here is made up. No real host, address or hardware appears in the
screenshots this produces.

    python fake_exporter.py [bind-address] [port]
"""

import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

GIB = 1024**3
KIB = 1024

# --- The machine being invented ---------------------------------------------
NODENAME = "edge-gw-01"
KERNEL = "6.6.31-v8+"
ARCH = "aarch64"
UPTIME_SECONDS = 18 * 86400 + 7 * 3600

CPU_COUNT = 4
CPU_IDLE_FRACTION = 0.73          # so the dashboard shows 27% used
LOAD_1, LOAD_5, LOAD_15 = 0.31, 0.28, 0.25

# Used amounts are given as fractions of the total so the dashboard's
# percentages come out as whole numbers rather than 46.3% and 38.7%.
RAM_TOTAL = 8 * GIB
RAM_USED = 0.46 * RAM_TOTAL
SWAP_TOTAL = 2 * GIB
SWAP_USED = 0.03 * SWAP_TOTAL

DISKS = [
    # device, mountpoint, fstype, total bytes, used fraction
    ("/dev/mmcblk0p2", "/",        "ext4", 62 * GIB, 0.39),
    ("/dev/sda1",      "/var/log", "ext4", 20 * GIB, 0.61),
]

TEMPS = [("CPU", 49.0)]

# Interface rates in KB/s, as the dashboard displays them.
INTERFACES = [
    ("eth0", 604, 142),
    ("wg0",   96,  74),
]

STARTED = time.monotonic()
BOOT_WALL = time.time() - UPTIME_SECONDS


def _render() -> str:
    elapsed = time.monotonic() - STARTED
    lines = []
    add = lines.append

    add(f'node_uname_info{{nodename="{NODENAME}",release="{KERNEL}",'
        f'machine="{ARCH}"}} 1')
    add(f"node_boot_time_seconds {BOOT_WALL:.0f}")

    add(f"node_load1 {LOAD_1}")
    add(f"node_load5 {LOAD_5}")
    add(f"node_load15 {LOAD_15}")

    # Each core accumulates one second of CPU time per second of wall clock.
    per_core = elapsed
    idle = per_core * CPU_IDLE_FRACTION
    busy = per_core - idle
    for cpu in range(CPU_COUNT):
        add(f'node_cpu_seconds_total{{cpu="{cpu}",mode="idle"}} {idle:.3f}')
        add(f'node_cpu_seconds_total{{cpu="{cpu}",mode="user"}} '
            f"{busy * 0.7:.3f}")
        add(f'node_cpu_seconds_total{{cpu="{cpu}",mode="system"}} '
            f"{busy * 0.3:.3f}")

    add(f"node_memory_MemTotal_bytes {RAM_TOTAL}")
    add(f"node_memory_MemAvailable_bytes {int(RAM_TOTAL - RAM_USED)}")
    add(f"node_memory_SwapTotal_bytes {SWAP_TOTAL}")
    add(f"node_memory_SwapFree_bytes {int(SWAP_TOTAL - SWAP_USED)}")

    for device, mount, fstype, total, used_fraction in DISKS:
        labels = f'device="{device}",mountpoint="{mount}",fstype="{fstype}"'
        add(f"node_filesystem_size_bytes{{{labels}}} {int(total)}")
        add(f"node_filesystem_avail_bytes{{{labels}}} "
            f"{int(total * (1 - used_fraction))}")

    for index, (label, celsius) in enumerate(TEMPS):
        add(f'node_hwmon_temp_celsius{{chip="soc",name="{label}",'
            f'sensor="temp{index + 1}_input"}} {celsius}')

    for name, rx_kbs, tx_kbs in INTERFACES:
        add(f'node_network_receive_bytes_total{{device="{name}"}} '
            f"{int(rx_kbs * KIB * elapsed)}")
        add(f'node_network_transmit_bytes_total{{device="{name}"}} '
            f"{int(tx_kbs * KIB * elapsed)}")

    # Filtered out by the poller. Present so that filtering is exercised.
    add('node_network_receive_bytes_total{device="lo"} 918273')
    add('node_network_transmit_bytes_total{device="lo"} 918273')
    add('node_filesystem_size_bytes{device="tmpfs",mountpoint="/run",'
        'fstype="tmpfs"} 419430400')
    add('node_filesystem_avail_bytes{device="tmpfs",mountpoint="/run",'
        'fstype="tmpfs"} 419430400')

    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = _render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep the capture run's output readable


def main(argv: list) -> None:
    address = argv[0] if argv else "127.0.0.1"
    port = int(argv[1]) if len(argv) > 1 else 9101
    server = HTTPServer((address, port), Handler)
    print(f"fake exporter listening on {address}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main(sys.argv[1:])
