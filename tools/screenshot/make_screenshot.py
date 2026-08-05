#!/usr/bin/env python3
"""Regenerate screenshots/network-dashboard-light-dark.png.

Runs the dashboard from a throwaway copy in a temp folder, pointed at invented
devices and a fake Node Exporter, photographs it in light and dark, and
stitches the two halves together.

Nothing here touches the working copy or a real install: the app runs from a
copy, on a spare port, with its own devices.json. The real devices.json is
never read or written.

    python tools/screenshot/make_screenshot.py

Options:
    --keep            leave the temp folder in place for inspection
    --build-tools P   path to the build-tools repo (default: sibling folder)
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scene  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

APP_FILES = ["main.py", "metrics_poller.py", "ssh_manager.py"]
APP_DIRS = ["static"]

OUT_IMAGE = os.path.join(REPO_ROOT, "screenshots",
                         "network-dashboard-light-dark.png")

# Each theme is laid out at this size and captured at half scale, giving two
# 900x648 halves and the 1800x648 composite the README has always used.
LAYOUT_WIDTH = 1800
LAYOUT_HEIGHT = 1296
CAPTURE_SCALE = 0.5


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def free_port(address: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((address, 0))
        return s.getsockname()[1]


def wait_for_http(url: str, timeout: float, label: str,
                  proc: subprocess.Popen) -> None:
    """Wait for a just-started process to serve url.

    Checks the process itself as well as the socket, so a crash on startup
    (a port already taken, a missing dependency) is reported straight away
    with its exit code, instead of looking like a slow start for the whole
    timeout and then reporting the wrong cause.
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        code = proc.poll()
        if code is not None:
            fail(f"{label} exited with code {code} before serving {url}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(0.2)
    fail(f"{label} did not answer at {url} within {timeout}s ({last})")


def stage_app(temp_dir: str, exporter_port: int) -> None:
    """Copy the app into temp_dir and give it the invented device list."""
    for name in APP_FILES:
        src = os.path.join(REPO_ROOT, name)
        if not os.path.exists(src):
            fail(f"expected to find {name} in {REPO_ROOT}")
        shutil.copy2(src, os.path.join(temp_dir, name))
    for name in APP_DIRS:
        src = os.path.join(REPO_ROOT, name)
        if not os.path.isdir(src):
            fail(f"expected to find the {name} folder in {REPO_ROOT}")
        shutil.copytree(src, os.path.join(temp_dir, name))

    devices = [dict(d, metrics_port=exporter_port) for d in scene.DEVICES]
    with open(os.path.join(temp_dir, "devices.json"), "w",
              encoding="utf-8") as f:
        json.dump({"_app": "Simple Network Dashboard", "devices": devices},
                  f, indent=2)


def build_setup_script() -> str:
    """JavaScript that dresses the page: an open SSH session with output, a
    fixed console clock, and (optionally) tidied addresses.

    It calls the page's own functions, the same ones the WebSocket calls when
    a real session runs, so nothing about the app is bypassed or stubbed.
    """
    device_id = json.dumps(scene.FEATURED["id"])
    parts = []

    if scene.PRETTY_ADDRESSES:
        # The stats panel and device cards redraw every time metrics arrive,
        # which would undo a one-off text swap. Wrap the render functions so
        # the swap is reapplied after every redraw.
        parts.append(
            "window.__tidyAddresses = () => {"
            f"  const from = {json.dumps(scene.REAL_PREFIX)};"
            f"  const to = {json.dumps(scene.PRETTY_PREFIX)};"
            "  const walker = document.createTreeWalker("
            "    document.body, NodeFilter.SHOW_TEXT);"
            "  let node;"
            "  while ((node = walker.nextNode())) {"
            "    if (node.nodeValue.includes(from)) {"
            "      node.nodeValue = node.nodeValue.split(from).join(to);"
            "    }"
            "  }"
            "};"
            "for (const name of ['renderStats', 'renderDevices']) {"
            "  const original = window[name];"
            "  window[name] = function (...args) {"
            "    const result = original.apply(this, args);"
            "    window.__tidyAddresses();"
            "    return result;"
            "  };"
            "}"
        )

    parts += [
        f"selectDevice({device_id});",
        f"handleSSHStatus({device_id}, 'connected');",
        # Brings the console tab forward and hides the "connect to begin"
        # placeholder, the same call clicking the tab makes.
        f"setActiveTab({device_id});",
        f"for (const [text, level] of {json.dumps(scene.CONSOLE)}) "
        f"  addLine({device_id}, text, level);",
        # Replace the wall clock with a fixed one, so two runs of this tool
        # produce the same image.
        "(() => {"
        f"  const start = {json.dumps(scene.CONSOLE_START)}"
        "    .split(':').map(Number);"
        "  let seconds = start[0] * 3600 + start[1] * 60 + start[2];"
        "  for (const tag of document.querySelectorAll('.ln .tag')) {"
        "    const h = String(Math.floor(seconds / 3600) % 24)"
        "      .padStart(2, '0');"
        "    const m = String(Math.floor(seconds / 60) % 60).padStart(2, '0');"
        "    const s = String(seconds % 60).padStart(2, '0');"
        "    tag.textContent = h + ':' + m + ':' + s;"
        "    seconds += 1;"
        "  }"
        "})();",
    ]

    if scene.PRETTY_ADDRESSES:
        parts.append("window.__tidyAddresses();")
    return " ".join(parts)


def write_capture_config(temp_dir: str, app_port: int) -> str:
    config = {
        "url": f"http://127.0.0.1:{app_port}/",
        "width": LAYOUT_WIDTH,
        "height": LAYOUT_HEIGHT,
        "scale": CAPTURE_SCALE,
        "outDir": "shots",
        "waitFor": "document.querySelector('#deviceList').children.length > 0",
        # CPU usage needs two polls before it can be worked out, so wait for
        # it specifically. Waiting on any percentage would catch RAM on the
        # first poll and photograph CPU still reading "Calculating".
        "waitForData":
            "document.querySelector('#statsPanel').textContent"
            "  .includes('%') && "
            "!document.querySelector('#statsPanel').textContent"
            "  .includes('Calculating')",
        "setup": build_setup_script(),
        "settleMs": 600,
        "shots": [
            {"name": "light", "script": "applyTheme('light')"},
            {"name": "dark", "script": "applyTheme('dark')"},
        ],
    }
    path = os.path.join(temp_dir, "shots.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return path


def run(cmd: list, label: str) -> None:
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        fail(f"{label} failed with exit code {result.returncode}")


def main(argv: list) -> None:
    keep = "--keep" in argv
    build_tools = os.path.join(os.path.dirname(REPO_ROOT), "build-tools")
    if "--build-tools" in argv:
        index = argv.index("--build-tools") + 1
        if index >= len(argv):
            fail("--build-tools needs a path after it")
        build_tools = argv[index]

    capture_script = os.path.join(build_tools, "screenshot", "capture.mjs")
    compose_script = os.path.join(build_tools, "screenshot", "compose.py")
    for path in (capture_script, compose_script):
        if not os.path.exists(path):
            fail(f"missing {path}. Pass --build-tools with the repo path.")

    exporter_address = scene.FEATURED["host"]
    exporter_port = free_port(exporter_address)
    app_port = free_port()
    temp_dir = tempfile.mkdtemp(prefix="snd-screenshot-")
    processes = []

    try:
        stage_app(temp_dir, exporter_port)

        exporter = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "fake_exporter.py"),
             exporter_address, str(exporter_port)])
        processes.append(exporter)
        wait_for_http(
            f"http://{exporter_address}:{exporter_port}/metrics", 15,
            "fake exporter", exporter)

        env = dict(os.environ, PYTHONUNBUFFERED="1")
        dashboard = subprocess.Popen(
            [sys.executable, "main.py", "--port", str(app_port)],
            cwd=temp_dir, env=env)
        processes.append(dashboard)
        wait_for_http(f"http://127.0.0.1:{app_port}/", 30, "dashboard",
                      dashboard)

        config_path = write_capture_config(temp_dir, app_port)
        run(["node", capture_script, config_path], "capture")

        shots_dir = os.path.join(temp_dir, "shots")
        run([sys.executable, compose_script, OUT_IMAGE,
             os.path.join(shots_dir, "light.png"),
             os.path.join(shots_dir, "dark.png")], "compose")
    finally:
        for proc in processes:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if keep:
            print(f"temp folder kept at {temp_dir}")
        else:
            shutil.rmtree(temp_dir, ignore_errors=True)
            if os.path.exists(temp_dir):
                print(f"WARNING: could not remove {temp_dir}", file=sys.stderr)

    print(f"updated {OUT_IMAGE}")


if __name__ == "__main__":
    main(sys.argv[1:])
