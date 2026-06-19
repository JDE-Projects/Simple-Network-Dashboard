#!/usr/bin/env bash
set -e

APP_DIR="/opt/simple-network-dashboard"
SERVICE_NAME="simple-network-dashboard"

if [ "$EUID" -ne 0 ]; then
    echo "Run with sudo: sudo bash uninstall.sh"
    exit 1
fi

INSTALL_USER="${SUDO_USER:-$(logname 2>/dev/null || echo '')}"

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
echo "  - service account and group: snd"
echo ""

# ---------------------------------------------------------------------------
# Config backup
# ---------------------------------------------------------------------------

BACKUP_DIR=""
HAS_CONFIG=false

if [ -f "$APP_DIR/devices.json" ] || [ -f "$APP_DIR/known_hosts" ]; then
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

        mkdir -p "$BACKUP_DIR"
        [ -f "$APP_DIR/devices.json" ] && cp "$APP_DIR/devices.json" "$BACKUP_DIR/"
        [ -f "$APP_DIR/known_hosts" ] && cp "$APP_DIR/known_hosts" "$BACKUP_DIR/"

        if [ -n "$INSTALL_USER" ] && [ -d "/home/$INSTALL_USER" ]; then
            chown -R "$INSTALL_USER:$INSTALL_USER" "$BACKUP_DIR"
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

# Remove snd user (no -r; account was created with --no-create-home)
if id snd &>/dev/null; then
    userdel snd
fi

# Remove snd group if it still exists
if getent group snd &>/dev/null; then
    groupdel snd 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "Simple Network Dashboard has been removed."
echo ""
echo "If your account was previously added to the snd group, that membership"
echo "is now gone. Log out and back in to refresh your group state."
if [ -n "$BACKUP_DIR" ]; then
    echo ""
    echo "Config backup: $BACKUP_DIR"
fi
