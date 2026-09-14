# Simple Network Dashboard
A self-hosted web dashboard for monitoring and managing home lab devices over a private local network.
Built by [JDE-Projects](https://jde-projects.com), home of the Simple X Tools suite.

If you enjoyed this project and would like to buy me a coffee, check out my [Ko-fi](https://ko-fi.com/jdeprojects).

## Preview
<p align="center">
  <img src="screenshots/network-dashboard-light-dark.png" width="900" alt="Simple Network Dashboard in dark and light themes">
  <br><em>Dark and light themes</em>
</p>

## Highlights
- Live device stats (CPU, RAM, disk, temperature, network) polled every 10 seconds, no credentials needed for metrics
- SSH management per device: run saved commands or one-off custom commands with live console output
- Per-device command library with pinned quick-buttons, optional sudo, and optional confirm prompts
- SSH passwords are never saved: held in server memory only while connected, wiped immediately on disconnect
- No login required: designed for private LAN use only

## How it works
- Backend: Caddy terminates local HTTPS and proxies HTTP and WSS traffic to FastAPI/Uvicorn on `127.0.0.1`.
- Metrics: polls `http://device-ip:9100/metrics` (Node Exporter, Prometheus format) on a 10-second interval
- SSH: Paramiko with trust-on-first-use host key pinning; passwords memory-only
- Real-time push: WebSocket delivers metric updates and SSH console output to the browser instantly
- Config: `devices.json` holds device names, hosts, usernames, Node Exporter ports, and command libraries, no credentials

## Deploy

**Prerequisites, on each device you want to monitor (Ubuntu / Raspberry Pi OS):**
```bash
sudo apt install prometheus-node-exporter
```

**On the server that will host the dashboard (requires Python 3.10+):**

1. Download the latest release tarball (`simple-network-dashboard-vX.Y.Z.tar.gz`) from the [Releases](https://github.com/JDE-Projects/Simple-Network-Dashboard/releases) page.
2. Optionally [verify the download](#verify-this-download-optional).
3. Extract and install:
```bash
tar -xzf simple-network-dashboard-vX.Y.Z.tar.gz
cd simple-network-dashboard-vX.Y.Z
sudo bash install.sh
```

Then open `https://<server-ip>` in your browser.

The install script creates a dedicated `snd` service account with no login shell, installs the app to `/opt/simple-network-dashboard`, stores private dashboard data in `/var/lib/simple-network-dashboard`, writes debug logs to `/var/log/simple-network-dashboard`, and sets up a systemd service that starts automatically on boot. The private data and log directories are accessible only to `snd`. Your personal account is added to the `snd` group so you can deploy updates; log out and back in after the first install for that to take effect.

Caddy serves the dashboard on HTTPS port 443 by default, using the first detected private LAN IPv4 address as both its certificate host and listener. Uvicorn is reachable only from Caddy on `127.0.0.1`; the installer chooses an internal backend port in the 3000-3010 range and preserves it on updates. Use `--https-port N` for a custom external HTTPS port. Use `--port N` only to override the internal backend port.

Use `--https-host HOST` to select a DNS certificate host or a private IPv4 address already assigned to this server. Literal IPv4 values must be private and locally assigned. Caddy still binds the dashboard listener to a verified private local IPv4 address.

If UFW is active, the installer adds only an app-labelled rule for the selected HTTPS port. Do not add a backend-port rule manually.

Caddy is installed from its official repository when absent. The installer reuses a compatible standard Caddyfile-managed service without overwriting existing configuration, and refuses API/resume, JSON, or custom service management.

The dashboard uses Caddy's `tls internal` local CA. Open the installer-provided setup page, such as `https://<server-ip>:<https-port>/certificate-setup`, and download the root certificate. Compare the SHA-256 printed by the installer with Windows:
```powershell
Get-FileHash -Path "$env:USERPROFILE\Downloads\caddy-root-ca.crt" -Algorithm SHA256
```
Only after the values match, import it into the current user's Windows Root store:
```powershell
Import-Certificate -FilePath "$env:USERPROFILE\Downloads\caddy-root-ca.crt" -CertStoreLocation Cert:\CurrentUser\Root
```

### Verify this download (optional)

Check the SHA-256 checksum:
```bash
sha256sum -c simple-network-dashboard-vX.Y.Z.tar.gz.sha256
```

Check the build attestation (requires the [GitHub CLI](https://cli.github.com/)):
```bash
gh attestation verify simple-network-dashboard-vX.Y.Z.tar.gz \
  --repo JDE-Projects/Simple-Network-Dashboard \
  --signer-repo JDE-Projects/Build-Tools
```

This proves the tarball was packaged by the published GitHub Actions pipeline from this public source, not on a personal machine. The `--signer-repo` flag is required because the workflow lives in a separate repository.

### Updating

Download the new release tarball, extract it, and re-run the installer:
```bash
tar -xzf simple-network-dashboard-vX.Y.Z.tar.gz
cd simple-network-dashboard-vX.Y.Z
sudo bash install.sh
```
The install script stops the running service, refreshes the app files, and restarts it. Updates preserve private data, the selected backend port, shared Caddy configuration, and Caddy's CA and browser trust. Re-pass `--https-host` and `--https-port` when you use custom values; `--https-port` otherwise defaults to 443.

The dashboard's bottom bar also has a **Check for updates** button that tells you when a newer release is available.

## Using it
1. Click **Add Device** and enter the display name, IP address, SSH username, and Node Exporter port (default 9100).
2. Stats appear immediately and update every 10 seconds: CPU, RAM, disk, temperature, and network rates.
3. To manage a device via SSH, enter the password for that device and click **Connect**.
4. Use the **Command Library** to save, pin, and reuse commands per device.
5. Pinned commands appear as quick-run buttons directly on the device card.

## Uninstall

Run the uninstaller from the extracted release folder or from an installed system:
```bash
sudo bash uninstall.sh
# or, on an installed system:
sudo bash /opt/simple-network-dashboard/uninstall.sh
```

The script removes the systemd service, the `/opt/simple-network-dashboard` directory (including the venv), private data in `/var/lib/simple-network-dashboard`, debug logs in `/var/log/simple-network-dashboard`, the dashboard-owned Caddy import and snippet, and app-labelled UFW rules. It preserves the shared Caddy package, configuration, and persistent CA storage. It removes the `snd` service account and group only when its protected installation record verifies that their current IDs match the account created by the installer. Otherwise, it retains them and explains why. Before removing anything it offers to back up `devices.json` and `known_hosts` from the private data directory into a private directory owned by the person who ran `sudo`, and asks for confirmation. Pass `--yes` for non-interactive runs (backs up config and proceeds without prompting). Uninstall cannot remove a certificate imported into a Windows trust store.

## Security and privacy
- SSH passwords are never written to disk. They are held in server memory only while a session is active and wiped immediately on disconnect.
- `devices.json` contains only device names, hosts, usernames, Node Exporter ports, and command libraries, no credentials of any kind. It is stored privately in `/var/lib/simple-network-dashboard`. Treat it as sensitive: it maps your internal hosts and accounts, so don't share it publicly (in a bug report, forum post, or public repo).
- The dashboard runs as a dedicated `snd` service account, isolated from your personal account, with no login shell.
- The dashboard has no authentication and is intended for use on a private, trusted LAN only. Do not expose or port-forward the dashboard to the internet.
- **Network use.** Other than the job you ask of it, this app makes one other network call: a check to GitHub for a newer release when you press **Check for updates**, which sends only a version request. It collects and sends no personal data, usage data, or analytics.

## A note on how this was built
This project was built with AI assistance. The design decisions, feature direction, and real-world testing were directed by me. The code was written and revised with an AI assistant against that direction. Treat it like any community tool: review and test it before relying on it.

## License
Released under the [PolyForm Noncommercial License 1.0.0](LICENSE).
Personal and noncommercial use, modification, and redistribution are
permitted; commercial use is not. See [THIRD-PARTY-LICENSES.txt](THIRD-PARTY-LICENSES.txt)
for notices on bundled dependencies.

For commercial licensing, open a [GitHub issue](https://github.com/JDE-Projects/Simple-Network-Dashboard/issues) with the title "Commercial License Inquiry".
