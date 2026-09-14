#!/usr/bin/env bash
set -e

APP_DIR="/opt/simple-network-dashboard"
DATA_DIR="/var/lib/simple-network-dashboard"
LOG_DIR="/var/log/simple-network-dashboard"
SERVICE_NAME="simple-network-dashboard"
RESET_COMMAND="/usr/local/sbin/snd-reset-password"
RESET_COMMAND_MARKER="# Simple Network Dashboard managed password reset command v1"
INSTALL_STATE="/etc/simple-network-dashboard/install-state"
CADDY_APP_CONFIG="/etc/caddy/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import ${CADDY_APP_CONFIG}"
UFW_COMMENT="Simple Network Dashboard HTTPS"
CADDY_APP_MARKER="# Simple Network Dashboard managed proxy v1"
CADDY_IMPORT_COMMENT="# Simple Network Dashboard managed Caddy import"
CADDY_HOME="/var/lib/caddy"

is_owned_caddy_app_config() {
    local metadata
    [ -L "$CADDY_APP_CONFIG" ] && return 1
    [ -f "$CADDY_APP_CONFIG" ] || return 1
    metadata=$(stat -c '%u:%g:%a' "$CADDY_APP_CONFIG") || return 1
    [ "$metadata" = "0:0:644" ] || return 1
    [ "$(head -n 1 "$CADDY_APP_CONFIG")" = "$CADDY_APP_MARKER" ]
}

count_caddy_imports() {
    awk -v comment="$CADDY_IMPORT_COMMENT" -v import_line="$CADDY_IMPORT_LINE" '
        $0 == import_line { total++ }
        previous == comment && $0 == import_line { managed++ }
        { previous=$0 }
        END { printf "%d %d\n", total + 0, managed + 0 }
    ' "$1"
}

inspect_caddy_for_cleanup() {
    local load_state exec_start

    command -v caddy &>/dev/null || {
        echo "Error: Caddy is unavailable; dashboard proxy cleanup was not attempted."
        return 1
    }
    load_state=$(systemctl show --property=LoadState --value caddy 2>/dev/null) || return 1
    if [ "$load_state" != "loaded" ]; then
        echo "Error: Caddy service is not available; dashboard proxy cleanup was not attempted."
        return 1
    fi
    exec_start=$(systemctl show --property=ExecStart --value caddy 2>/dev/null) || return 1
    if [[ "$exec_start" == *"--resume"* ]] || [[ "$exec_start" == *"--adapter json"* ]] \
        || [[ "$exec_start" == *".json"* ]] \
        || [[ ! "$exec_start" =~ caddy[[:space:]]+run[[:space:]] ]] \
        || [[ ! "$exec_start" =~ --config[[:space:]]+([^[:space:];]+) ]]; then
        echo "Error: Caddy is not a compatible Caddyfile-managed service; dashboard proxy cleanup was not attempted."
        return 1
    fi
    CADDY_CONFIG_PATH="${BASH_REMATCH[1]}"
    if [[ "$CADDY_CONFIG_PATH" != *Caddyfile ]] || [ ! -f "$CADDY_CONFIG_PATH" ] \
        || [ -L "$CADDY_CONFIG_PATH" ]; then
        echo "Error: Caddyfile path is unsafe or unsupported; dashboard proxy cleanup was not attempted."
        return 1
    fi
    CADDY_BINARY=$(command -v caddy)
}

remove_dashboard_caddy_integration() {
    local config_dir staged_caddyfile backup_caddyfile backup_app_config had_app_config=false import_counts import_count managed_import_count

    if { [ -e "$CADDY_APP_CONFIG" ] || [ -L "$CADDY_APP_CONFIG" ]; } && ! is_owned_caddy_app_config; then
        echo "Error: dashboard Caddy proxy config is not a root-owned marked regular file; refusing to remove it."
        return 1
    fi
    if [ ! -e "$CADDY_APP_CONFIG" ] && ! command -v caddy &>/dev/null; then
        return 0
    fi
    if ! inspect_caddy_for_cleanup; then
        return 1
    fi
    import_counts=$(count_caddy_imports "$CADDY_CONFIG_PATH") || return 1
    read -r import_count managed_import_count <<< "$import_counts"
    if [ "$import_count" -ne "$managed_import_count" ]; then
        echo "Error: Caddyfile contains an unowned dashboard import; refusing cleanup."
        return 1
    fi
    if [ "$managed_import_count" -eq 0 ] && [ ! -e "$CADDY_APP_CONFIG" ]; then
        return 0
    fi

    config_dir=$(dirname "$CADDY_CONFIG_PATH")
    staged_caddyfile=$(mktemp "$config_dir/.simple-network-dashboard-uninstall.XXXXXX") || return 1
    backup_caddyfile=$(mktemp "$config_dir/.simple-network-dashboard-uninstall-backup.XXXXXX") || {
        rm -f -- "$staged_caddyfile"
        return 1
    }
    backup_app_config=$(mktemp "$config_dir/.simple-network-dashboard-proxy-backup.XXXXXX") || {
        rm -f -- "$staged_caddyfile" "$backup_caddyfile"
        return 1
    }
    trap 'rm -f -- "$staged_caddyfile" "$backup_caddyfile" "$backup_app_config"' RETURN
    awk -v comment="$CADDY_IMPORT_COMMENT" -v import_line="$CADDY_IMPORT_LINE" '
        $0 == comment { pending=1; next }
        pending && $0 == import_line { pending=0; next }
        pending { print comment; pending=0 }
        $0 != import_line { print }
        END { if (pending) print comment }
    ' "$CADDY_CONFIG_PATH" > "$staged_caddyfile" || return 1
    if ! "$CADDY_BINARY" validate --config "$staged_caddyfile" --adapter caddyfile; then
        echo "Error: remaining Caddy configuration is invalid; no dashboard proxy configuration was removed."
        return 1
    fi

    cp -- "$CADDY_CONFIG_PATH" "$backup_caddyfile" || return 1
    if [ -f "$CADDY_APP_CONFIG" ]; then
        had_app_config=true
    fi
    if { [ "$had_app_config" = true ] && ! mv -- "$CADDY_APP_CONFIG" "$backup_app_config"; } \
        || ! install -m 0644 -- "$staged_caddyfile" "$CADDY_CONFIG_PATH"; then
        echo "Error: Caddy cleanup publication failed; restoring the dashboard proxy configuration."
        install -m 0644 -- "$backup_caddyfile" "$CADDY_CONFIG_PATH" || echo "Error: previous Caddyfile could not be restored."
        if [ "$had_app_config" = true ] && [ -f "$backup_app_config" ]; then
            mv -- "$backup_app_config" "$CADDY_APP_CONFIG" || echo "Error: dashboard proxy config could not be restored."
        fi
        return 1
    fi
    if ! systemctl reload caddy; then
        echo "Error: Caddy reload failed; restoring the dashboard proxy configuration."
        install -m 0644 -- "$backup_caddyfile" "$CADDY_CONFIG_PATH" || echo "Error: previous Caddyfile could not be restored."
        if [ "$had_app_config" = true ] && [ -f "$backup_app_config" ]; then
            mv -- "$backup_app_config" "$CADDY_APP_CONFIG" || echo "Error: dashboard proxy config could not be restored."
        fi
        systemctl reload caddy || echo "Error: rollback reload of Caddy also failed."
        return 1
    fi
    rm -f -- "$backup_app_config"
}

remove_dashboard_ufw_rules() {
    local rule_numbers

    command -v ufw &>/dev/null || return 0
    rule_numbers=$(ufw status numbered 2>/dev/null | awk -v comment="$UFW_COMMENT" '
        $0 ~ ("# " comment "[[:space:]]*$") { line=$0; sub(/^[[:space:]]*\[/, "", line); sub(/\].*/, "", line); gsub(/^[[:space:]]+|[[:space:]]+$/, "", line); print line }
    ' | sort -rn) || return 1
    while IFS= read -r rule; do
        [ -n "$rule" ] || continue
        ufw --force delete "$rule" || return 1
    done <<< "$rule_numbers"
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

is_owned_reset_command() {
    local metadata

    [ -f "$RESET_COMMAND" ] && [ ! -L "$RESET_COMMAND" ] || return 1
    [ -f "$APP_DIR/snd-reset-password" ] && [ ! -L "$APP_DIR/snd-reset-password" ] || return 1
    metadata=$(stat -c '%u:%g:%a' "$RESET_COMMAND") || return 1
    [ "$metadata" = "0:0:755" ] || return 1
    [ "$(sed -n '2p' "$RESET_COMMAND")" = "$RESET_COMMAND_MARKER" ] \
        && cmp -s -- "$RESET_COMMAND" "$APP_DIR/snd-reset-password"
}

remove_owned_reset_command() {
    if [ ! -e "$RESET_COMMAND" ] && [ ! -L "$RESET_COMMAND" ]; then
        return 0
    fi
    if ! is_owned_reset_command; then
        echo "Retaining $RESET_COMMAND because it is not the verified dashboard-owned reset command."
        return 0
    fi
    rm -- "$RESET_COMMAND"
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

# Remove only the dashboard-owned proxy integration and labelled firewall
# rules before application files are touched.  A failed cleanup leaves the
# installation in place for an administrator to inspect and retry. The Caddy
# package and its shared persistent CA storage under $CADDY_HOME are never
# modified by this uninstaller.
if ! remove_dashboard_caddy_integration; then
    echo "Error: dashboard Caddy integration cleanup failed. Application files were not removed."
    exit 1
fi
if ! remove_dashboard_ufw_rules; then
    echo "Error: dashboard HTTPS firewall cleanup failed. Caddy cleanup may already have completed; application files were not removed."
    exit 1
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
if ! remove_owned_reset_command; then
    echo "Error: could not remove the verified dashboard password-reset command. Application files were not removed."
    exit 1
fi

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
echo "The Caddy package and persistent CA storage at $CADDY_HOME were preserved."
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
