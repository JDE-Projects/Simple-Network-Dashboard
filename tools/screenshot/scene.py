#!/usr/bin/env python3
"""What the README screenshot shows: invented devices, an invented command
library, and an invented SSH session.

None of this is real. The addresses are loopback aliases in 127.0.0.0/8. The
generator extracts the displayed stats from the fake exporter's text before
the capture, because the dashboard correctly refuses to poll loopback targets.

PRETTY_ADDRESSES rewrites the displayed 127.24.8.x labels to 10.24.8.x for the
photograph only, so the image reads as an ordinary home LAN. Set it to False to
show the loopback addresses the app is really talking to.
"""

PRETTY_ADDRESSES = True
REAL_PREFIX = "127.24.8."
PRETTY_PREFIX = "10.24.8."

# The clock shown against console lines. Fixed so that two runs of the tool
# produce an identical image.
CONSOLE_START = "09:41:07"

COMMANDS = [
    {"name": "Interface Status", "command": "ip -br addr",
     "sudo": False, "confirm": ""},
    {"name": "Routing Table", "command": "ip route show",
     "sudo": False, "confirm": ""},
    {"name": "Disk Usage", "command": "df -h",
     "sudo": False, "confirm": ""},
    {"name": "Service Health", "command": "systemctl --failed",
     "sudo": False, "confirm": ""},
    {"name": "Update Packages", "command": "apt update && apt full-upgrade -y",
     "sudo": True, "confirm": "Update all packages now?"},
]

DEVICES = [
    {"id": "demo-1", "name": "Edge Gateway",  "host": REAL_PREFIX + "1",
     "username": "netops",  "commands": COMMANDS},
    {"id": "demo-2", "name": "Storage Node",  "host": REAL_PREFIX + "20",
     "username": "storage", "commands": []},
    {"id": "demo-3", "name": "DNS Resolver",  "host": REAL_PREFIX + "53",
     "username": "dnsops",  "commands": []},
    {"id": "demo-4", "name": "Lab Hypervisor", "host": REAL_PREFIX + "30",
     "username": "virtops", "commands": []},
]

# The device the screenshot is focused on. Its address is used only in the
# staged device configuration before display-only address tidying.
FEATURED = DEVICES[0]

# (text, level). Levels are the console's own classes: cmd, out, ok, err,
# warn, muted.
CONSOLE = [
    (f"Connected to {PRETTY_PREFIX}1 as netops.", "ok"),
    ("$ Interface Status", "cmd"),
    ("lo               UNKNOWN        127.0.0.1/8 ::1/128", "out"),
    (
        (
            f"eth0             UP             {PRETTY_PREFIX}1/24 "
            "fe80::2c4:71ff:fe3a:9201/64"
        ),
        "out",
    ),
    ("wg0              UNKNOWN        10.44.0.1/24", "out"),
    ("✓ Interface Status finished.", "ok"),
    ("$ Routing Table", "cmd"),
    (
        (
            f"default via {PRETTY_PREFIX}254 dev eth0 proto dhcp src "
            f"{PRETTY_PREFIX}1 metric 100"
        ),
        "out",
    ),
    (
        (
            f"{PRETTY_PREFIX}0/24 dev eth0 proto kernel scope link src "
            f"{PRETTY_PREFIX}1"
        ),
        "out",
    ),
    ("10.44.0.0/24 dev wg0 proto kernel scope link src 10.44.0.1", "out"),
    ("✓ Routing Table finished.", "ok"),
    ("$ Service Health", "cmd"),
    ("  UNIT LOAD ACTIVE SUB DESCRIPTION", "out"),
    ("0 loaded units listed.", "out"),
    ("✓ Service Health finished.", "ok"),
    ("$ Disk Usage", "cmd"),
    ("Filesystem      Size  Used Avail Use% Mounted on", "out"),
    ("/dev/mmcblk0p2   62G   24G   38G  39% /", "out"),
    ("/dev/sda1        20G   12G  7.8G  61% /var/log", "out"),
    ("✓ Disk Usage finished.", "ok"),
]
