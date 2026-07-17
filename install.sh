#!/usr/bin/env bash
set -e

APP_DIR="/opt/simple-network-dashboard"
SERVICE_NAME="simple-network-dashboard"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
PORT_MIN=3000
PORT_MAX=3010
FORCED_PORT=""

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo: sudo bash install.sh"
    exit 1
fi

INSTALL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo '')}"
if [ -z "$INSTALL_USER" ]; then
    echo "Error: could not determine the user who invoked sudo."
    exit 1
fi

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            FORCED_PORT="$2"
            shift 2
            ;;
        --port=*)
            FORCED_PORT="${1#--port=}"
            shift
            ;;
        *)
            echo "Error: unknown argument '$1'. Usage: $0 [--port N]"
            exit 1
            ;;
    esac
done

if [ -n "$FORCED_PORT" ]; then
    case "$FORCED_PORT" in
        ''|*[!0-9]*)
            echo "Error: --port requires a numeric port, got '$FORCED_PORT'."
            exit 1
            ;;
    esac
    if [ "$FORCED_PORT" -lt 1 ] || [ "$FORCED_PORT" -gt 65535 ]; then
        echo "Error: --port must be between 1 and 65535, got '$FORCED_PORT'."
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Pick a port
# ---------------------------------------------------------------------------
# On an update (unit already installed), keep the port the service already
# uses: probing would see the running service holding its own port and move
# the app somewhere new on every re-run.
if [ -z "$FORCED_PORT" ] && [ -f "$UNIT_DEST" ]; then
    FORCED_PORT=$(grep -o -- '--port [0-9]*' "$UNIT_DEST" | awk '{print $2}')
    # Legacy installs (from before --port support) have no --port in their
    # ExecStart, so the grep above finds nothing. Those always ran on 3000.
    if [ -z "$FORCED_PORT" ]; then
        FORCED_PORT=3000
    fi
fi

if [ -n "$FORCED_PORT" ]; then
    PORT="$FORCED_PORT"
else
    PORT=""
    LISTENING=$(ss -tln 2>/dev/null || true)
    candidate=$PORT_MIN
    while [ "$candidate" -le "$PORT_MAX" ]; do
        if ! echo "$LISTENING" | grep -qE ":${candidate}[[:space:]]"; then
            PORT="$candidate"
            break
        fi
        candidate=$((candidate + 1))
    done
    if [ -z "$PORT" ]; then
        echo "Error: no free port found in range ${PORT_MIN}-${PORT_MAX}."
        echo "Free one up, or re-run with --port N to pick one manually."
        exit 1
    fi
fi

echo "Installing Simple Network Dashboard..."

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install it with: sudo apt install python3 python3-venv"
    exit 1
fi

# Check venv module
if ! python3 -c "import venv" &>/dev/null; then
    echo "Error: python3-venv not found. Install it with: sudo apt install python3-venv"
    exit 1
fi

# Create snd service account if it doesn't exist
if ! id snd &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin snd
    echo "Created service account: snd"
fi

# Add install user to snd group so they can deploy updates via scp
if ! groups "$INSTALL_USER" | grep -qw snd; then
    usermod -aG snd "$INSTALL_USER"
    ADDED_TO_GROUP=true
fi

# Create app directory with setgid so copied files inherit the snd group
mkdir -p "$APP_DIR/static"
chown snd:snd "$APP_DIR" "$APP_DIR/static"
chmod 2775 "$APP_DIR" "$APP_DIR/static"

# Stop service if already running (handles re-runs for updates)
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
fi

# Copy app files
cp main.py metrics_poller.py ssh_manager.py requirements.txt uninstall.sh "$APP_DIR/"
cp static/index.html "$APP_DIR/static/"

# Create venv if it doesn't exist, then install/update requirements
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u snd python3 -m venv "$APP_DIR/venv"
fi
sudo -u snd "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet

# Create systemd service file
cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=Simple Network Dashboard
After=network.target

[Service]
User=snd
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python main.py --port ${PORT}
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
systemctl start "$SERVICE_NAME"

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo "Simple Network Dashboard is running."
echo "Open http://${SERVER_IP}:${PORT} in your browser."
echo "To uninstall later: sudo bash $APP_DIR/uninstall.sh"
if [ "$ADDED_TO_GROUP" = true ]; then
    echo ""
    echo "Note: $INSTALL_USER was added to the snd group. Log out and back in for this to take effect."
fi
