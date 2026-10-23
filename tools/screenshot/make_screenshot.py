#!/usr/bin/env python3
"""Regenerate screenshots/network-dashboard-light-dark.png.

Runs the dashboard from a throwaway copy in a temp folder, seeds its sign-in
state, injects invented Node Exporter metrics, photographs it in light and
dark, and stitches the two halves together.

Nothing here touches the working copy or a real install: the app runs from a
copy, on a spare port, with its own devices.json. The real devices.json is
never read or written.

    python tools/screenshot/make_screenshot.py

Options:
    --keep            leave the temp folder in place for inspection
    --build-tools P   path to the build-tools repo (default: sibling folder)
"""

import importlib.util
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))

# The scene files sit beside this script; the app's own metrics parser sits
# in the repo root.
sys.path.insert(0, HERE)
sys.path.insert(1, REPO_ROOT)
import fake_exporter  # noqa: E402
import scene  # noqa: E402

import metrics_poller  # noqa: E402

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


def root_modules() -> list[str]:
    """Return the root modules required by the flat dashboard application."""
    for name in ("main.py", "auth.py"):
        if not os.path.isfile(os.path.join(REPO_ROOT, name)):
            fail(f"expected to find {name} in {REPO_ROOT}")
    return sorted(
        name for name in os.listdir(REPO_ROOT)
        if name.endswith(".py") and name != "conftest.py"
    )


def seed_auth_state(temp_dir: str, data_dir: str) -> str:
    """Create throwaway authentication state through the staged auth module."""
    auth_path = os.path.join(temp_dir, "auth.py")
    spec = importlib.util.spec_from_file_location("screenshot_staged_auth", auth_path)
    if spec is None or spec.loader is None:
        fail(f"could not load staged authentication module from {auth_path}")
    staged_auth = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(staged_auth)
    password = secrets.token_urlsafe(24)
    staged_auth.publish_auth_state(
        os.path.join(data_dir, "auth.json"),
        staged_auth.create_auth_state(password),
    )
    return password


def stage_app(temp_dir: str) -> str:
    """Copy the dashboard, runtime folders, devices, and sign-in state."""
    for name in root_modules():
        src = os.path.join(REPO_ROOT, name)
        shutil.copy2(src, os.path.join(temp_dir, name))
    for name in APP_DIRS:
        src = os.path.join(REPO_ROOT, name)
        if not os.path.isdir(src):
            fail(f"expected to find the {name} folder in {REPO_ROOT}")
        shutil.copytree(src, os.path.join(temp_dir, name))

    data_dir = os.path.join(temp_dir, "data")
    os.mkdir(data_dir)
    os.mkdir(os.path.join(temp_dir, "logs"))
    with open(os.path.join(data_dir, "devices.json"), "w",
              encoding="utf-8") as f:
        json.dump({"_app": "Simple Network Dashboard", "devices": scene.DEVICES},
                  f, indent=2)
    return seed_auth_state(temp_dir, data_dir)


def build_stats() -> dict:
    """Extract public metrics from two invented readings about two seconds apart."""
    previous = metrics_poller.extract_metrics(
        metrics_poller._parse(fake_exporter._render()), None
    )
    time.sleep(2)
    current = metrics_poller.extract_metrics(
        metrics_poller._parse(fake_exporter._render()), previous
    )
    return {key: value for key, value in current.items() if not key.startswith("_")}


def build_setup_script(stats: dict) -> str:
    """JavaScript that dresses the page: an open SSH session with output, a
    fixed console clock, injected metrics, and (optionally) tidied addresses.

    It calls the page's own functions, the same ones the WebSocket calls when
    a real session runs, so nothing about the app is bypassed or stubbed.
    """
    device_id = json.dumps(scene.FEATURED["id"])
    parts = [
        f"window.__screenshotDeviceId = {device_id};",
        f"window.__screenshotMetrics = {json.dumps(stats)};",
        (
            "window.__restoreScreenshotMetrics = () => {"
            "  METRICS[window.__screenshotDeviceId] = window.__screenshotMetrics;"
            "};"
        ),
    ]

    if scene.PRETTY_ADDRESSES:
        # The stats panel and device cards redraw every time metrics arrive,
        # so the address replacement must run after each redraw.
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
        )

    parts += [
        # The poller rejects this tool's loopback addresses and broadcasts an
        # error every two seconds. Reinstall the generated metrics before each
        # page render, just as address tidying is reapplied after a redraw.
        (
            "for (const name of ['renderStats', 'renderDevices']) {"
            "  const original = window[name];"
            "  window[name] = function (...args) {"
            "    window.__restoreScreenshotMetrics();"
            "    const result = original.apply(this, args);"
            "    if (window.__tidyAddresses) window.__tidyAddresses();"
            "    return result;"
            "  };"
            "}"
        ),
        f"selectDevice({device_id});",
        f"handleSSHStatus({device_id}, 'connected');",
        # Brings the console tab forward and hides the "connect to begin"
        # placeholder, the same call clicking the tab makes.
        f"setActiveTab({device_id});",
        (
            f"for (const [text, level] of {json.dumps(scene.CONSOLE)}) "
            f"  addLine({device_id}, text, level);"
        ),
        # Use the page's formatter with fixed Date values, so locale-specific
        # 12-hour and 24-hour rendering stays faithful and deterministic.
        (
            "(() => {"
            f"  const start = {json.dumps(scene.CONSOLE_START)}"
            "    .split(':').map(Number);"
            "  const stamp = new Date(2000, 0, 1, start[0], start[1], start[2]);"
            "  for (const tag of document.querySelectorAll('.ln .tag')) {"
            "    tag.textContent = consoleTime(stamp);"
            "    stamp.setSeconds(stamp.getSeconds() + 1);"
            "  }"
            "})();"
        ),
    ]

    if scene.PRETTY_ADDRESSES:
        parts.append("window.__tidyAddresses();")
    return " ".join(parts)


def write_capture_config(temp_dir: str, app_port: int, password: str, stats: dict) -> str:
    config = {
        "url": f"http://127.0.0.1:{app_port}/",
        "width": LAYOUT_WIDTH,
        "height": LAYOUT_HEIGHT,
        "scale": CAPTURE_SCALE,
        "outDir": "shots",
        "signIn": {
            "url": f"http://127.0.0.1:{app_port}/login",
            "passwordSelector": "#password",
            "password": password,
            "submitSelector": "#loginForm button",
            "doneWhen": "location.pathname === '/'",
        },
        "waitFor": "document.querySelector('#deviceList').children.length > 0",
        # Wait for the injected CPU result rather than a transient poller
        # error, and reject the page's first-poll placeholder explicitly.
        "waitForData":
            # Built in the page, so the number reads the way the page prints
            # it (27, not Python's 27.0).
            "document.querySelector('#statsPanel').textContent"
            "  .includes(window.__screenshotMetrics.cpu_percent + '%') && "
            "!document.querySelector('#statsPanel').textContent"
            "  .includes('Calculating')",
        "setup": build_setup_script(stats),
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
    result = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
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

    app_port = free_port()
    temp_dir = tempfile.mkdtemp(prefix="snd-screenshot-")
    processes = []

    try:
        password = stage_app(temp_dir)
        stats = build_stats()

        env = dict(
            os.environ,
            PYTHONUNBUFFERED="1",
            SND_DATA_DIR=os.path.join(temp_dir, "data"),
            SND_LOG_DIR=os.path.join(temp_dir, "logs"),
            SND_PUBLIC_ORIGIN=f"http://127.0.0.1:{app_port}",
        )
        dashboard = subprocess.Popen(
            [sys.executable, "main.py", "--port", str(app_port)],
            cwd=temp_dir, env=env)
        processes.append(dashboard)
        wait_for_http(f"http://127.0.0.1:{app_port}/login", 30, "dashboard",
                      dashboard)

        config_path = write_capture_config(temp_dir, app_port, password, stats)
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
