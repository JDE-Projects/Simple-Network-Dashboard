# Simple Network Dashboard
A self-hosted web dashboard for monitoring and managing home lab devices over a private local network.
Built by [JDE-Projects](https://github.com/JDE-Projects).

## Highlights
- Live device stats (CPU, RAM, disk, temperature, network) polled every 10 seconds — no credentials needed for metrics
- SSH management per device — run saved commands or one-off custom commands with live console output
- Per-device command library with pinned quick-buttons, optional sudo, and optional confirm prompts
- SSH passwords are never saved — held in server memory only while connected, wiped immediately on disconnect
- No login required — designed for private LAN use only

## How it works
- Backend: Python 3 + FastAPI, served by Uvicorn on a Linux server (Ubuntu, Raspberry Pi OS, etc.)
- Metrics: polls `http://device-ip:9100/metrics` (Node Exporter, Prometheus format) on a 10-second interval
- SSH: Paramiko with trust-on-first-use host key pinning; passwords memory-only
- Real-time push: WebSocket delivers metric updates and SSH console output to the browser instantly
- Config: `devices.json` holds device names, hosts, usernames, Node Exporter ports, and command libraries — no credentials

## Deploy

**Prerequisites — on each device you want to monitor (Ubuntu / Raspberry Pi OS):**
```bash
sudo apt install prometheus-node-exporter
```

**On the server that will host the dashboard (requires Python 3.10+):**
```bash
git clone https://github.com/JDE-Projects/Simple-Network-Dashboard.git
cd Simple-Network-Dashboard
sudo bash install.sh
```

Then open `http://<server-ip>:3000` in your browser.

The install script creates a dedicated `snd` service account with no login shell, installs the app to `/opt/simple-network-dashboard`, and sets up a systemd service that starts automatically on boot. Your personal account is added to the `snd` group so you can deploy updates — log out and back in after the first install for that to take effect.

**If you have a firewall enabled:**
```bash
sudo ufw allow 3000/tcp
```

**To update to a newer version:**
```bash
git pull
sudo bash install.sh
```

## Using it
1. Click **Add Device** and enter the display name, IP address, SSH username, and Node Exporter port (default 9100).
2. Stats appear immediately and update every 10 seconds — CPU, RAM, disk, temperature, and network rates.
3. To manage a device via SSH, enter the password for that device and click **Connect**.
4. Use the **Command Library** to save, pin, and reuse commands per device.
5. Pinned commands appear as quick-run buttons directly on the device card.

## Security and privacy
- SSH passwords are never written to disk. They are held in server memory only while a session is active and wiped immediately on disconnect.
- `devices.json` contains only device names, hosts, usernames, Node Exporter ports, and command libraries — no credentials of any kind.
- The dashboard runs as a dedicated `snd` service account — isolated from your personal account, no login shell.
- The dashboard has no authentication and is intended for use on a private, trusted LAN only. Do not expose port 3000 to the internet.

## A note on how this was built
This project was built with AI assistance. The design decisions, feature direction, and real-world testing were directed by me. The code was written and revised with an AI assistant against that direction.

## License
Released under the PolyForm Noncommercial License 1.0.0. See LICENSE.
