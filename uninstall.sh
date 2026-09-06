#!/usr/bin/env bash
set -e

APP_DIR="/opt/simple-network-dashboard"
DATA_DIR="/var/lib/simple-network-dashboard"
LOG_DIR="/var/log/simple-network-dashboard"
SERVICE_NAME="simple-network-dashboard"
INSTALL_STATE="/etc/simple-network-dashboard/install-state"

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
    if ! id snd &>/dev/null || ! group_entry=$(getent group snd); then
        return 1
    fi
    CURRENT_SND_UID=$(id -u snd)
    CURRENT_SND_PRIMARY_GID=$(id -g snd)
    CURRENT_SND_GROUP_GID=$(printf '%s\n' "$group_entry" | awk -F: 'NF >= 3 { print $3; exit }')
    [[ "$CURRENT_SND_UID" =~ ^[0-9]+$ ]] \
        && [[ "$CURRENT_SND_PRIMARY_GID" =~ ^[0-9]+$ ]] \
        && [[ "$CURRENT_SND_GROUP_GID" =~ ^[0-9]+$ ]]
}

classify_uninstall_account_state() {
    REMOVE_SND_IDENTITIES=false
    RETAIN_SND_REASON=""

    if [ ! -e "$INSTALL_STATE" ] && [ ! -L "$INSTALL_STATE" ]; then
        RETAIN_SND_REASON="the ownership record is absent"
    elif ! read_install_state; then
        RETAIN_SND_REASON="the ownership record is ${INSTALL_STATE_ERROR}"
    elif ! read_current_snd_ids; then
        RETAIN_SND_REASON="the snd user or group is missing or has invalid IDs"
    elif [ "$CURRENT_SND_UID" != "$RECORDED_SND_UID" ] \
        || [ "$CURRENT_SND_PRIMARY_GID" != "$RECORDED_SND_GID" ] \
        || [ "$CURRENT_SND_GROUP_GID" != "$RECORDED_SND_GID" ]; then
        RETAIN_SND_REASON="the current snd IDs do not match the ownership record"
    else
        REMOVE_SND_IDENTITIES=true
    fi
}

remove_install_state() {
    if [ -f "$INSTALL_STATE" ] || [ -L "$INSTALL_STATE" ]; then
        rm -f "$INSTALL_STATE"
    fi
}

remove_snd_identities_if_verified() {
    SND_IDENTITIES_REMOVED=false
    classify_uninstall_account_state
    if [ "$REMOVE_SND_IDENTITIES" != true ]; then
        echo "Retaining snd user and group because ${RETAIN_SND_REASON}."
        return 0
    fi

    # No -r: the account was created with --no-create-home.
    if ! userdel snd; then
        echo "Error: could not remove the verified snd user; the installation record was retained."
        return 1
    fi
    if ! groupdel snd; then
        echo "Error: snd user was removed but the snd group remains; the installation record was retained."
        return 1
    fi
    if ! remove_install_state; then
        echo "Error: snd identities were removed but the installation record could not be removed."
        return 1
    fi
    SND_IDENTITIES_REMOVED=true
}

is_private_config_file() {
    [ -f "$1" ] && [ ! -L "$1" ]
}

has_private_config() {
    is_private_config_file "$DATA_DIR/devices.json" \
        || is_private_config_file "$DATA_DIR/known_hosts"
}

create_private_backup_dir() {
    if [ -z "${BACKUP_OWNER:-}" ] || [ -z "${BACKUP_GROUP:-}" ]; then
        echo "Error: could not determine the sudo user's backup ownership."
        return 1
    fi
    if ! mkdir -m 0700 -- "$BACKUP_DIR"; then
        echo "Error: could not create private backup directory: $BACKUP_DIR"
        return 1
    fi
}

backup_private_config_file() {
    local source_file="$1"
    local backup_file="$BACKUP_DIR/${source_file##*/}"

    # The subshell's restrictive umask keeps the root-created copy private
    # until ownership is transferred to the sudo user below.
    if ! (umask 077; cp --no-dereference -- "$source_file" "$backup_file"); then
        echo "Error: could not copy private configuration: $source_file"
        return 1
    fi
    if ! is_private_config_file "$backup_file"; then
        echo "Error: backup copy is not a regular file: $backup_file"
        rm -f -- "$backup_file"
        return 1
    fi
    if ! chmod 0600 -- "$backup_file"; then
        echo "Error: could not set private backup file permissions: $backup_file"
        return 1
    fi
    if ! chown --no-dereference -- "$BACKUP_OWNER:$BACKUP_GROUP" "$backup_file"; then
        echo "Error: could not set backup file ownership: $backup_file"
        return 1
    fi
}

backup_private_config() {
    if is_private_config_file "$DATA_DIR/devices.json"; then
        backup_private_config_file "$DATA_DIR/devices.json" || return 1
    fi
    if is_private_config_file "$DATA_DIR/known_hosts"; then
        backup_private_config_file "$DATA_DIR/known_hosts" || return 1
    fi
}

finalize_private_backup_dir() {
    if ! chown -- "$BACKUP_OWNER:$BACKUP_GROUP" "$BACKUP_DIR"; then
        echo "Error: could not set backup directory ownership: $BACKUP_DIR"
        return 1
    fi
}

require_root() {
    if [ "$EUID" -ne 0 ]; then
        echo "Run with sudo: sudo bash uninstall.sh"
        return 1
    fi
}

main() {
if ! require_root; then
    return 1
fi

INSTALL_USER="${SUDO_USER:-$(logname 2>/dev/null || id -un)}"
BACKUP_OWNER="$INSTALL_USER"
BACKUP_GROUP=$(id -gn "$BACKUP_OWNER") || {
    echo "Error: could not determine the sudo user's backup group."
    exit 1
}

# Decide account deletion before any removal mutation.
classify_uninstall_account_state

# Parse flags
AUTO_YES=false
if [ "${1:-}" = "--yes" ]; then
    AUTO_YES=true
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo "This will remove:"
echo "  - systemd service: $SERVICE_NAME"
echo "  - application directory: $APP_DIR (including venv)"
echo "  - private runtime data: $DATA_DIR"
echo "  - debug logs: $LOG_DIR"
if [ "$REMOVE_SND_IDENTITIES" = true ]; then
    echo "  - verified service account and group: snd"
else
    echo "  - service account and group: retained because ${RETAIN_SND_REASON}"
fi
echo ""

# ---------------------------------------------------------------------------
# Config backup
# ---------------------------------------------------------------------------

BACKUP_DIR=""
HAS_CONFIG=false

if has_private_config; then
    HAS_CONFIG=true
fi

if [ "$HAS_CONFIG" = true ]; then
    DO_BACKUP="y"
    if [ "$AUTO_YES" = false ]; then
        read -r -p "Back up devices.json and known_hosts before removing? [Y/n] " DO_BACKUP
        DO_BACKUP="${DO_BACKUP:-y}"
    fi

    if [[ "$DO_BACKUP" =~ ^[Yy]$ ]] || [ -z "$DO_BACKUP" ]; then
        TIMESTAMP=$(date +%Y%m%d-%H%M%S)

        # Determine backup destination
        if [ -n "$INSTALL_USER" ] && [ -d "/home/$INSTALL_USER" ]; then
            BACKUP_DIR="/home/$INSTALL_USER/snd-backup-$TIMESTAMP"
        else
            BACKUP_DIR="$(pwd)/snd-backup-$TIMESTAMP"
            echo "Note: could not determine user home directory; backing up to $BACKUP_DIR"
        fi

        if ! create_private_backup_dir \
            || ! backup_private_config \
            || ! finalize_private_backup_dir; then
            echo "Error: configuration backup could not be completed safely. Nothing was removed."
            exit 1
        fi

        echo "Config backed up to: $BACKUP_DIR"
        echo ""
    fi
fi

# ---------------------------------------------------------------------------
# Confirm removal
# ---------------------------------------------------------------------------

if [ "$AUTO_YES" = false ]; then
    read -r -p "Remove Simple Network Dashboard now? [y/N] " CONFIRM
    CONFIRM="${CONFIRM:-n}"
    if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
        echo "Aborted. Nothing was removed."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------

echo "Removing Simple Network Dashboard..."

# Stop service
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl stop "$SERVICE_NAME"
fi

# Disable service
if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    systemctl disable "$SERVICE_NAME" --quiet
fi

# Remove service file
if [ -f "/etc/systemd/system/$SERVICE_NAME.service" ]; then
    rm "/etc/systemd/system/$SERVICE_NAME.service"
    systemctl daemon-reload
fi
systemctl reset-failed "$SERVICE_NAME" 2>/dev/null || true

# Remove app directory
if [ -d "$APP_DIR" ]; then
    rm -rf "$APP_DIR"
fi

# Remove private state only after the optional backup above completes.
if [ -d "$DATA_DIR" ]; then
    rm -rf "$DATA_DIR"
fi
if [ -d "$LOG_DIR" ]; then
    rm -rf "$LOG_DIR"
fi

# Re-check ownership at the point of account deletion, after prompts and removal.
if ! remove_snd_identities_if_verified; then
    echo ""
    echo "Application files were removed, but snd account cleanup was incomplete."
    exit 1
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "Simple Network Dashboard has been removed."
echo ""
if [ "$SND_IDENTITIES_REMOVED" = true ]; then
    echo "If your account was previously added to the snd group, that membership"
    echo "is now gone. Log out and back in to refresh your group state."
else
    echo "The snd identities were retained because ${RETAIN_SND_REASON}."
    echo "Any existing snd group membership remains in place."
fi
if [ -n "$BACKUP_DIR" ]; then
    echo ""
    echo "Config backup: $BACKUP_DIR"
fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
