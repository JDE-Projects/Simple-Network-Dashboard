#!/usr/bin/env bash
set -e

APP_DIR="/opt/simple-network-dashboard"
SERVICE_NAME="simple-network-dashboard"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
INSTALL_STATE_DIR="/etc/simple-network-dashboard"
INSTALL_STATE="${INSTALL_STATE_DIR}/install-state"
PORT_MIN=3000
PORT_MAX=3010
FORCED_PORT=""

record_install_state() {
    local temp_state=""

    (
        umask 077
        mkdir -p "$INSTALL_STATE_DIR" || exit 1
        chown root:root "$INSTALL_STATE_DIR" || exit 1
        chmod 700 "$INSTALL_STATE_DIR" || exit 1
        temp_state=$(mktemp "${INSTALL_STATE_DIR}/.install-state.XXXXXX") || exit 1
        trap 'if [ -n "$temp_state" ]; then rm -f -- "$temp_state"; fi' EXIT
        printf 'SND_UID=%s\nSND_GID=%s\n' "$CURRENT_SND_UID" "$CURRENT_SND_GROUP_GID" > "$temp_state" || exit 1
        chown root:root "$temp_state" || exit 1
        chmod 600 "$temp_state" || exit 1
        mv -f "$temp_state" "$INSTALL_STATE" || exit 1
        temp_state=""
    )
}

read_install_state() {
    local state_metadata
    local -a state_lines

    INSTALL_STATE_ERROR=""
    RECORDED_SND_UID=""
    RECORDED_SND_GID=""

    if [ -L "$INSTALL_STATE" ] || [ ! -f "$INSTALL_STATE" ]; then
        INSTALL_STATE_ERROR="unsafe (it must be a regular file)"
        return 1
    fi

    state_metadata=$(stat -c '%u:%g:%a' "$INSTALL_STATE") || {
        INSTALL_STATE_ERROR="unreadable"
        return 1
    }
    if [ "$state_metadata" != "0:0:600" ]; then
        INSTALL_STATE_ERROR="unsafe (it must be owned by root:root with mode 0600)"
        return 1
    fi

    mapfile -t state_lines < "$INSTALL_STATE" || {
        INSTALL_STATE_ERROR="unreadable"
        return 1
    }
    if [ "${#state_lines[@]}" -ne 2 ] \
        || [[ ! "${state_lines[0]}" =~ ^SND_UID=[0-9]+$ ]] \
        || [[ ! "${state_lines[1]}" =~ ^SND_GID=[0-9]+$ ]]; then
        INSTALL_STATE_ERROR="malformed"
        return 1
    fi

    RECORDED_SND_UID="${state_lines[0]#SND_UID=}"
    RECORDED_SND_GID="${state_lines[1]#SND_GID=}"
}

read_current_snd_ids() {
    local group_entry

    CURRENT_SND_UID=""
    CURRENT_SND_PRIMARY_GID=""
    CURRENT_SND_GROUP_GID=""
    if ! id snd &>/dev/null; then
        return 1
    fi
    if ! group_entry=$(getent group snd); then
        return 1
    fi

    CURRENT_SND_UID=$(id -u snd)
    CURRENT_SND_PRIMARY_GID=$(id -g snd)
    CURRENT_SND_GROUP_GID=$(printf '%s\n' "$group_entry" | awk -F: 'NF >= 3 { print $3; exit }')
    if [[ ! "$CURRENT_SND_UID" =~ ^[0-9]+$ ]] \
        || [[ ! "$CURRENT_SND_PRIMARY_GID" =~ ^[0-9]+$ ]] \
        || [[ ! "$CURRENT_SND_GROUP_GID" =~ ^[0-9]+$ ]]; then
        return 1
    fi
}

classify_account_state() {
    local is_fresh_install=false
    local user_exists=false
    local group_exists=false

    [ ! -e "$APP_DIR" ] && [ ! -e "$UNIT_DEST" ] \
        && [ ! -e "$INSTALL_STATE" ] && [ ! -L "$INSTALL_STATE" ] \
        && is_fresh_install=true
    id snd &>/dev/null && user_exists=true
    getent group snd &>/dev/null && group_exists=true

    if [ "$is_fresh_install" = true ]; then
        if [ "$user_exists" = true ] || [ "$group_exists" = true ]; then
            echo "Error: fresh install refused because an unmanaged snd user or group already exists."
            return 1
        fi
        ACCOUNT_STATE="fresh"
        return 0
    fi

    if [ -e "$INSTALL_STATE" ] || [ -L "$INSTALL_STATE" ]; then
        if ! read_install_state; then
            echo "Error: existing installation record is ${INSTALL_STATE_ERROR}. Refusing update."
            return 1
        fi
        if ! read_current_snd_ids; then
            echo "Error: managed installation is missing a valid snd user or group. Refusing update."
            return 1
        fi
        if [ "$CURRENT_SND_UID" != "$RECORDED_SND_UID" ] \
            || [ "$CURRENT_SND_PRIMARY_GID" != "$RECORDED_SND_GID" ] \
            || [ "$CURRENT_SND_GROUP_GID" != "$RECORDED_SND_GID" ]; then
            echo "Error: snd identity IDs do not match the installation record. Refusing update."
            return 1
        fi
        ACCOUNT_STATE="managed-update"
        echo "Verified managed snd account ownership for update."
        return 0
    fi

    if [ "$user_exists" != true ] || [ "$group_exists" != true ]; then
        echo "Error: legacy installation has only one or neither snd identity. Refusing update."
        return 1
    fi
    if ! read_current_snd_ids; then
        echo "Error: legacy installation has invalid snd identity IDs. Refusing update."
        return 1
    fi
    ACCOUNT_STATE="legacy-update"
    echo "Existing installation ownership is unverified; preserving snd identities without creating a record."
}

create_managed_snd_account() {
    local created_group=false
    local created_user=false
    local cleanup_failed=false

    if ! groupadd --system snd; then
        echo "Error: could not create the snd system group."
        return 1
    fi
    created_group=true
    if ! useradd --system --no-create-home --shell /usr/sbin/nologin --gid snd snd; then
        echo "Error: could not create the snd system user."
    else
        created_user=true
        if read_current_snd_ids && record_install_state; then
            echo "Created service account: snd"
            return 0
        fi
        echo "Error: could not verify or record the new snd identities."
    fi

    if [ "$created_user" = true ] && ! userdel snd; then
        echo "Error: cleanup could not remove the newly created snd user."
        cleanup_failed=true
    fi
    if [ "$created_group" = true ] && ! groupdel snd; then
        echo "Error: cleanup could not remove the newly created snd group."
        cleanup_failed=true
    fi
    if [ "$cleanup_failed" = true ]; then
        echo "Error: snd account creation failed and cleanup was incomplete."
    else
        echo "snd account creation failed; newly created identities were removed."
    fi
    return 1
}

main() {
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

# Classify account ownership before any installation mutation.
if ! classify_account_state; then
    exit 1
fi

if [ "$ACCOUNT_STATE" = "fresh" ]; then
    create_managed_snd_account
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
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
