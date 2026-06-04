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
- Backend: Python 3 + FastAPI, served by Uvicorn on an Ubuntu server
- Metrics: polls `http://device-ip:9100/metrics` (Node Exporter, Prometheus format) on a 10-second interval
- SSH: Paramiko with trust-on-first-use host key pinning; passwords memory-only
- Real-time push: WebSocket delivers metric updates and SSH console output to the browser instantly
- Config: `devices.json` holds device names, hosts, usernames, Node Exporter ports, and command libraries — no credentials

## Deploy to server

**Prerequisites — on each device you want to monitor (Ubuntu / Raspberry Pi OS):**
```bash
sudo apt install prometheus-node-exporter
```

**One-time setup on your Ubuntu server:**
```bash
git clone https://github.com/JDE-Projects/Simple-Network-Dashboard.git ~/simple-network-dashboard
cd ~/simple-network-dashboard
pip install -r requirements.txt
python main.py
```

Then open `http://<server-ip>:3000` in your browser.

To run the dashboard automatically on boot, set it up as a systemd service:
```bash
sudo nano /etc/systemd/system/simple-network-dashboard.service
```
```ini
[Unit]
Description=Simple Network Dashboard
After=network.target

[Service]
User=youruser
WorkingDirectory=/home/youruser/simple-network-dashboard
ExecStart=/usr/bin/python3 main.py
Restart=always

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable simple-network-dashboard
sudo systemctl start simple-network-dashboard
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
- The dashboard has no authentication and is intended for use on a private, trusted LAN only. Do not expose port 3000 to the internet.

## A note on how this was built
This project was built with AI assistance. The design decisions, feature direction, and real-world testing were directed by me. The code was written and revised with an AI assistant against that direction.

## License
Released under the MIT license. See LICENSE.
