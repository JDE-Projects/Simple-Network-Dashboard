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
CADDY_APT_SOURCE="/etc/apt/sources.list.d/caddy-stable.list"
CADDY_APT_KEYRING="/usr/share/keyrings/caddy-stable-archive-keyring.gpg"

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

parse_uninstall_flags() {
    AUTO_YES=false
    REMOVE_CADDY_REQUESTED=false

    local flag
    for flag in "$@"; do
        case "$flag" in
            --yes) AUTO_YES=true ;;
            --remove-caddy) REMOVE_CADDY_REQUESTED=true ;;
            *) : ;;
        esac
    done
}

resolve_caddy_removal_eligibility() {
    CADDY_REMOVAL_ELIGIBLE=false
    if [ "${CADDY_INSTALL_STATE_READ_SUCCEEDED:-false}" = true ] \
        && [ "${RECORDED_DASHBOARD_INSTALLED_CADDY:-}" = true ]; then
        CADDY_REMOVAL_ELIGIBLE=true
    fi
}

resolve_caddy_removal_intent() {
    local remove_caddy_answer

    CADDY_INTENT=keep
    if [ "$CADDY_REMOVAL_ELIGIBLE" != true ]; then
        if [ "$REMOVE_CADDY_REQUESTED" = true ]; then
            echo "Caddy will be kept because there is no record that this dashboard installed it."
        fi
        return 0
    fi

    if [ "$REMOVE_CADDY_REQUESTED" = true ]; then
        CADDY_INTENT=remove
    elif [ "$AUTO_YES" = false ]; then
        # A closed or non-interactive input stream defaults to keeping Caddy.
        if ! read -r -p "This dashboard installed the Caddy web server. Remove Caddy and its certificate store too? It will be kept if anything else still uses it. [y/N] " remove_caddy_answer; then
            remove_caddy_answer="n"
        fi
        remove_caddy_answer="${remove_caddy_answer:-n}"
        if [[ "$remove_caddy_answer" =~ ^[Yy]$ ]]; then
            CADDY_INTENT=remove
        fi
    fi
}

caddy_config_is_safe_to_remove() {
    local import_counts import_count managed_import_count

    CADDY_KEEP_REASON=""
    if [ "$CADDY_REMOVAL_ELIGIBLE" != true ] || [ "$CADDY_INTENT" != remove ]; then
        return 1
    fi
    if ! inspect_caddy_for_cleanup; then
        CADDY_KEEP_REASON="Caddy was kept because it is not a supported Caddyfile-managed service."
        echo "$CADDY_KEEP_REASON"
        return 1
    fi
    if [ ! -r "$CADDY_CONFIG_PATH" ]; then
        CADDY_KEEP_REASON="Caddy was kept because its Caddyfile could not be read."
        echo "$CADDY_KEEP_REASON"
        return 1
    fi
    import_counts=$(count_caddy_imports "$CADDY_CONFIG_PATH") || {
        CADDY_KEEP_REASON="Caddy was kept because its Caddyfile could not be read."
        echo "$CADDY_KEEP_REASON"
        return 1
    }
    read -r import_count managed_import_count <<< "$import_counts"
    if [[ ! "$import_count" =~ ^[0-9]+$ ]] || [[ ! "$managed_import_count" =~ ^[0-9]+$ ]]; then
        CADDY_KEEP_REASON="Caddy was kept because its Caddyfile could not be read."
        echo "$CADDY_KEEP_REASON"
        return 1
    fi
    if [ "$import_count" -ne 0 ] \
        || ! awk '/^[[:space:]]*$/ { next } /^[[:space:]]*#/ { next } { exit 1 }' "$CADDY_CONFIG_PATH"; then
        CADDY_KEEP_REASON="Caddy was kept because other sites still use it."
        echo "$CADDY_KEEP_REASON"
        return 1
    fi
}

remove_dashboard_caddy() {
    CADDY_REMOVAL_STATUS=failed

    if systemctl is-active --quiet caddy 2>/dev/null; then
        systemctl stop caddy || echo "Error: could not stop the Caddy service before removal."
    fi
    if systemctl is-enabled --quiet caddy 2>/dev/null; then
        systemctl disable caddy --quiet || echo "Error: could not disable the Caddy service before removal."
    fi
    if ! apt-get purge -y caddy; then
        echo "Error: Caddy cleanup incomplete: the Caddy package was not removed; its certificate store at $CADDY_HOME and Caddy apt source files were left in place."
        return 1
    fi

    if [ "$CADDY_HOME" != "/var/lib/caddy" ] || [ ! -d "$CADDY_HOME" ] || [ -L "$CADDY_HOME" ]; then
        echo "Error: Caddy cleanup incomplete: the Caddy package was removed, but the certificate store at $CADDY_HOME and Caddy apt source files were left in place because the certificate-store path was unsafe or not a real directory."
        return 1
    fi
    if ! rm -rf -- "$CADDY_HOME"; then
        echo "Error: Caddy cleanup incomplete: the Caddy package was removed, but the certificate store at $CADDY_HOME and Caddy apt source files were left in place."
        return 1
    fi
    if ! rm -f -- "$CADDY_APT_SOURCE" "$CADDY_APT_KEYRING"; then
        echo "Error: Caddy cleanup incomplete: the Caddy package and certificate store were removed, but one or more Caddy apt source files were not removed."
        return 1
    fi

    CADDY_REMOVAL_STATUS=removed
    echo "Caddy and its certificate store were removed. The caddy service account and group were intentionally left in place."
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
    RECORDED_DASHBOARD_INSTALLED_CADDY=""

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
    if [ "${state_lines[0]}" = "RECORD_VERSION=2" ]; then
        if [ "${#state_lines[@]}" -ne 4 ] \
            || [[ ! "${state_lines[1]}" =~ ^SND_UID=[0-9]+$ ]] \
            || [[ ! "${state_lines[2]}" =~ ^SND_GID=[0-9]+$ ]] \
            || { [ "${state_lines[3]}" != "DASHBOARD_INSTALLED_CADDY=true" ] \
                && [ "${state_lines[3]}" != "DASHBOARD_INSTALLED_CADDY=false" ]; }; then
            INSTALL_STATE_ERROR="malformed"
            return 1
        fi
        RECORDED_SND_UID="${state_lines[1]#SND_UID=}"
        RECORDED_SND_GID="${state_lines[2]#SND_GID=}"
        RECORDED_DASHBOARD_INSTALLED_CADDY="${state_lines[3]#DASHBOARD_INSTALLED_CADDY=}"
        return 0
    fi
    if [ "${#state_lines[@]}" -ne 2 ] \
        || [[ ! "${state_lines[0]}" =~ ^SND_UID=[0-9]+$ ]] \
        || [[ ! "${state_lines[1]}" =~ ^SND_GID=[0-9]+$ ]]; then
        INSTALL_STATE_ERROR="malformed"
        return 1
    fi
    RECORDED_SND_UID="${state_lines[0]#SND_UID=}"
    RECORDED_SND_GID="${state_lines[1]#SND_GID=}"
    RECORDED_DASHBOARD_INSTALLED_CADDY="unknown"
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
    CADDY_INSTALL_STATE_READ_SUCCEEDED=false

    if [ ! -e "$INSTALL_STATE" ] && [ ! -L "$INSTALL_STATE" ]; then
        RETAIN_SND_REASON="the ownership record is absent"
    else
        if ! read_install_state; then
            RETAIN_SND_REASON="the ownership record is ${INSTALL_STATE_ERROR}"
        else
            CADDY_INSTALL_STATE_READ_SUCCEEDED=true
            if ! read_current_snd_ids; then
                RETAIN_SND_REASON="the snd user or group is missing or has invalid IDs"
            elif [ "$CURRENT_SND_UID" != "$RECORDED_SND_UID" ] \
                || [ "$CURRENT_SND_PRIMARY_GID" != "$RECORDED_SND_GID" ] \
                || [ "$CURRENT_SND_GROUP_GID" != "$RECORDED_SND_GID" ]; then
                RETAIN_SND_REASON="the current snd IDs do not match the ownership record"
            else
                REMOVE_SND_IDENTITIES=true
            fi
        fi
    fi
}

remove_install_state() {
    if [ -f "$INSTALL_STATE" ] || [ -L "$INSTALL_STATE" ]; then
        rm -f "$INSTALL_STATE"
    fi
    # Remove the dashboard-owned state directory once its record is gone, but
    # only when it is empty so unexpected contents are left for inspection.
    local state_dir
    state_dir=$(dirname "$INSTALL_STATE")
    if [ -d "$state_dir" ]; then
        rmdir "$state_dir" 2>/dev/null || true
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

# Parse flags and resolve the optional Caddy removal before the summary.
parse_uninstall_flags "$@"
resolve_caddy_removal_eligibility
resolve_caddy_removal_intent

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
if [ "$CADDY_REMOVAL_ELIGIBLE" = true ] && [ "$CADDY_INTENT" = remove ]; then
    echo "  - Caddy web server and certificate store: if nothing else still uses it"
else
    echo "  - Caddy web server: kept"
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
        # A closed or non-interactive input stream defaults to backing up,
        # the safe choice, instead of letting set -e abort on the read.
        if ! read -r -p "Back up devices.json and known_hosts before removing? [Y/n] " DO_BACKUP; then
            DO_BACKUP="y"
        fi
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
    # A closed or non-interactive input stream must fail loudly here rather
    # than let set -e abort silently before this confirmation is answered.
    if ! read -r -p "Remove Simple Network Dashboard now? [y/N] " CONFIRM; then
        echo "No confirmation received on a non-interactive input stream. Nothing was removed."
        echo "For an unattended removal, re-run with: sudo bash uninstall.sh --yes"
        exit 1
    fi
    CONFIRM="${CONFIRM:-n}"
    if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
        echo "Aborted. Nothing was removed."
        exit 0
    fi
fi

# Remove only the dashboard-owned proxy integration and labelled firewall
# rules before application files are touched. A failed cleanup leaves the
# installation in place for an administrator to inspect and retry.
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

CADDY_REMOVAL_STATUS=kept
if [ "$CADDY_REMOVAL_ELIGIBLE" = true ] && [ "$CADDY_INTENT" = remove ]; then
    if caddy_config_is_safe_to_remove; then
        remove_dashboard_caddy || true
    fi
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
case "$CADDY_REMOVAL_STATUS" in
    removed)
        echo "Caddy and its certificate store at $CADDY_HOME were removed. The caddy service account and group were left in place."
        ;;
    kept)
        if [ "$CADDY_REMOVAL_ELIGIBLE" != true ]; then
            echo "The Caddy package and persistent CA storage at $CADDY_HOME were preserved."
        elif [ "$CADDY_INTENT" = remove ]; then
            echo "${CADDY_KEEP_REASON:-Caddy was kept because its configuration could not be safely inspected.}"
        else
            echo "The Caddy package and persistent CA storage at $CADDY_HOME were preserved."
        fi
        ;;
    failed)
        echo "Caddy cleanup did not finish. Some Caddy files may remain; see the error above."
        ;;
esac
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
