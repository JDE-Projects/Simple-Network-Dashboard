#!/usr/bin/env bash
set -e

APP_DIR="/opt/simple-network-dashboard"
DATA_DIR="/var/lib/simple-network-dashboard"
LOG_DIR="/var/log/simple-network-dashboard"
SERVICE_NAME="simple-network-dashboard"
UNIT_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
INSTALL_STATE_DIR="/etc/simple-network-dashboard"
INSTALL_STATE="${INSTALL_STATE_DIR}/install-state"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=11
PORT_MIN=3000
PORT_MAX=3010
FORCED_PORT=""
HTTPS_PORT=""
HTTPS_PORT_WAS_SET=false
HTTPS_PORT_DEFAULT=443
HTTPS_PORT_MIN=8443
HTTPS_PORT_MAX=8453
HTTPS_HOST=""
HTTPS_BIND=""
HTTPS_HOST_WAS_SET=false
PUBLIC_ORIGIN=""
CADDY_APP_CONFIG="/etc/caddy/simple-network-dashboard.caddy"
CADDY_IMPORT_LINE="import ${CADDY_APP_CONFIG}"
UFW_COMMENT="Simple Network Dashboard HTTPS"
CADDY_APP_MARKER="# Simple Network Dashboard managed proxy v1"
CADDY_IMPORT_COMMENT="# Simple Network Dashboard managed Caddy import"
CADDY_GLOBAL_MARKER="# Simple Network Dashboard managed global options v1"
HTTPS_DISABLE_REDIRECTS=false
CADDY_HOME="/var/lib/caddy"
CADDY_DATA_HOME="${CADDY_HOME}/.local/share"
CADDY_ROOT_CERT="${CADDY_DATA_HOME}/caddy/pki/authorities/local/root.crt"
DASHBOARD_ROOT_CERT="${DATA_DIR}/caddy-root-ca.crt"
AUTH_FILE="${DATA_DIR}/auth.json"
SESSIONS_FILE="${DATA_DIR}/sessions.json"
RESET_COMMAND="/usr/local/sbin/snd-reset-password"
RESET_COMMAND_MARKER="# Simple Network Dashboard managed password reset command v1"
CADDY_ROLLBACK_CADDYFILE=""
CADDY_ROLLBACK_APP_CONFIG=""
CADDY_ROLLBACK_HAD_APP_CONFIG=false
DASHBOARD_WAS_ACTIVE=false
DASHBOARD_INSTALLED_CADDY=false
# An update is rolled back only when an application and its venv are already
# installed. These are set by detect_existing_installation and
# create_rollback_point.
UPDATE_ELIGIBLE=false
PACKAGE_WAS_INSTALLED=false
ROLLBACK_ACTIVE=false
ROLLBACK_PACKAGE_MOVED=false
ROLLBACK_VENV_MOVED=false
# Free space required beyond the size of the current venv, in KiB (200 MiB).
# The old venv is moved by rename, so the extra space covers the new venv (about
# the old venv's size) plus the new package, pip's temporary files and logs.
DISK_MARGIN_KB=204800
# Readiness check timings, in seconds. Tests shrink them.
READY_TIMEOUT_SECONDS=30
READY_POLL_SECONDS=1
READY_CONFIRMATIONS=3
READY_REQUEST_TIMEOUT_SECONDS=2
READY_PROBE_SCRIPT=$(cat <<'PY'
import sys
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
try:
    with opener.open(sys.argv[1], timeout=float(sys.argv[2])) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
PY
)

usage() {
    echo "Usage: $0 [--port N] [--https-host HOST] [--https-port N]"
}

is_valid_port() {
    [[ "$1" =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

is_valid_ipv4() {
    local address="$1"
    local part
    local -a parts

    IFS='.' read -r -a parts <<< "$address"
    [ "${#parts[@]}" -eq 4 ] || return 1
    for part in "${parts[@]}"; do
        [[ "$part" =~ ^[0-9]{1,3}$ ]] || return 1
        [ "$((10#$part))" -le 255 ] || return 1
    done
}

is_lan_ipv4() {
    local address="$1"
    local first second

    is_valid_ipv4 "$address" || return 1
    IFS='.' read -r first second _ <<< "$address"
    if [ "$first" -eq 10 ]; then
        return 0
    fi
    if [ "$first" -eq 192 ] && [ "$second" -eq 168 ]; then
        return 0
    fi
    if [ "$first" -eq 172 ] && [ "$second" -ge 16 ] && [ "$second" -le 31 ]; then
        return 0
    fi
    return 1
}

detect_lan_ipv4() {
    local address

    while IFS= read -r address; do
        if is_lan_ipv4 "$address"; then
            printf '%s\n' "$address"
            return 0
        fi
    done < <(hostname -I 2>/dev/null | tr ' ' '\n')
    return 1
}

is_local_lan_ipv4() {
    local address="$1"
    local candidate

    while IFS= read -r candidate; do
        if [ "$candidate" = "$address" ] && is_lan_ipv4 "$candidate"; then
            return 0
        fi
    done < <(hostname -I 2>/dev/null | tr ' ' '\n')
    return 1
}

is_valid_https_host() {
    local host="$1"
    local label
    local -a labels

    if is_valid_ipv4 "$host"; then
        is_lan_ipv4 "$host"
        return
    fi
    [[ "$host" =~ ^[0-9]+(\.[0-9]+){3}$ ]] && return 1
    [ "${#host}" -le 253 ] || return 1
    [[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] || return 1
    [[ "$host" != .* ]] && [[ "$host" != *. ]] || return 1
    IFS='.' read -r -a labels <<< "$host"
    for label in "${labels[@]}"; do
        [ -n "$label" ] && [ "${#label}" -le 63 ] || return 1
        [[ "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || return 1
    done
}

parse_install_arguments() {
    FORCED_PORT=""
    HTTPS_PORT=""
    HTTPS_PORT_WAS_SET=false
    HTTPS_HOST=""
    HTTPS_BIND=""
    HTTPS_HOST_WAS_SET=false

    while [ $# -gt 0 ]; do
        case "$1" in
            --port|--https-port|--https-host)
                if [ $# -lt 2 ] || [ -z "$2" ]; then
                    echo "Error: $1 requires a value."
                    usage
                    return 1
                fi
                case "$1" in
                    --port) FORCED_PORT="$2" ;;
                    --https-port) HTTPS_PORT="$2"; HTTPS_PORT_WAS_SET=true ;;
                    --https-host) HTTPS_HOST="$2"; HTTPS_HOST_WAS_SET=true ;;
                esac
                shift 2
                ;;
            --port=*) FORCED_PORT="${1#--port=}"; shift ;;
            --https-port=*) HTTPS_PORT="${1#--https-port=}"; HTTPS_PORT_WAS_SET=true; shift ;;
            --https-host=*) HTTPS_HOST="${1#--https-host=}"; HTTPS_HOST_WAS_SET=true; shift ;;
            *)
                echo "Error: unknown argument '$1'."
                usage
                return 1
                ;;
        esac
    done

    if [ -n "$FORCED_PORT" ] && ! is_valid_port "$FORCED_PORT"; then
        echo "Error: --port must be a numeric port between 1 and 65535, got '$FORCED_PORT'."
        return 1
    fi
    if [ "$HTTPS_PORT_WAS_SET" = true ] && ! is_valid_port "$HTTPS_PORT"; then
        echo "Error: --https-port must be a numeric port between 1 and 65535, got '$HTTPS_PORT'."
        return 1
    fi
    if ! HTTPS_BIND=$(detect_lan_ipv4); then
        echo "Error: could not detect a private local IPv4 address for the HTTPS listener."
        return 1
    fi
    if [ "$HTTPS_HOST_WAS_SET" = false ]; then
        HTTPS_HOST="$HTTPS_BIND"
    fi
    if ! is_valid_https_host "$HTTPS_HOST"; then
        echo "Error: --https-host must be a valid IPv4 address or DNS hostname; IPv4 addresses must be private and locally assigned, got '$HTTPS_HOST'."
        usage
        return 1
    fi
    if is_valid_ipv4 "$HTTPS_HOST"; then
        if ! is_local_lan_ipv4 "$HTTPS_HOST"; then
            echo "Error: --https-host must be a valid IPv4 address or DNS hostname; IPv4 addresses must be private and locally assigned, got '$HTTPS_HOST'."
            usage
            return 1
        fi
        HTTPS_BIND="$HTTPS_HOST"
    fi
}

listeners_for_port() {
    local port="$1"

    awk -v port="$port" '
        $1 == "LISTEN" {
            endpoint = $4
            sub(/.*:/, "", endpoint)
            gsub(/]/, "", endpoint)
            if (endpoint == port) print
        }
    ' <<< "$SS_LISTENERS"
}

inspect_caddy_state() {
    local load_state dpkg_status

    CADDY_STATE="fresh"
    CADDY_BINARY=$(command -v caddy 2>/dev/null || true)
    CADDY_CONFIG_PATH=""
    if ! command -v systemctl &>/dev/null; then
        echo "Error: systemctl is required to inspect existing Caddy management."
        return 1
    fi
    if ! load_state=$(systemctl show --property=LoadState --value caddy 2>/dev/null); then
        echo "Error: could not inspect the caddy systemd service."
        return 1
    fi

    if [ "$load_state" = "not-found" ]; then
        if [ -n "$CADDY_BINARY" ]; then
            echo "Error: Caddy binary found at $CADDY_BINARY but no caddy systemd service was found."
            echo "Refusing to modify an unsupported Caddy setup."
            return 1
        fi
        return 0
    fi
    if [ -z "$CADDY_BINARY" ]; then
        if dpkg_status=$(dpkg-query -W -f='${db:Status-Abbrev}' caddy 2>/dev/null) \
            && [ "$dpkg_status" = "rc " ]; then
            echo "Error: Caddy was removed but its configuration was kept."
            echo "Optional: back up /etc/caddy and /var/lib/caddy (the second holds Caddy's certificates)."
            echo "Run sudo apt-get install caddy to restore Caddy while keeping its existing configuration."
            echo "Run this installer again."
            echo "Destructive alternative for a clean start: sudo apt-get purge caddy deletes /etc/caddy and /var/lib/caddy, including Caddy's certificates. Every device that trusts the dashboard's certificate would have to trust a new one."
            return 1
        fi
        echo "Error: a caddy systemd service exists but its Caddy binary could not be found."
        echo "Refusing to modify an unsupported Caddy setup."
        return 1
    fi

    CADDY_EXEC_START=$(systemctl show --property=ExecStart --value caddy 2>/dev/null) || {
        echo "Error: could not inspect the caddy systemd service launch command."
        return 1
    }
    if [[ "$CADDY_EXEC_START" == *"--resume"* ]] \
        || [[ "$CADDY_EXEC_START" == *"--adapter json"* ]] \
        || [[ "$CADDY_EXEC_START" == *".json"* ]]; then
        echo "Error: Caddy uses API/resume or JSON configuration and is not supported by this installer."
        return 1
    fi
    if [[ ! "$CADDY_EXEC_START" =~ caddy[[:space:]]+run[[:space:]] ]]; then
        echo "Error: caddy systemd service is not launched with 'caddy run'."
        echo "Refusing to modify an unsupported Caddy setup."
        return 1
    fi
    if [[ "$CADDY_EXEC_START" =~ --config[[:space:]]+([^[:space:];]+) ]]; then
        CADDY_CONFIG_PATH="${BASH_REMATCH[1]}"
    fi
    if [[ "$CADDY_CONFIG_PATH" != *Caddyfile ]]; then
        echo "Error: caddy systemd service does not use a Caddyfile configuration."
        echo "Refusing to modify an unsupported Caddy setup."
        return 1
    fi
    CADDY_STATE="compatible"
}

https_port_available() {
    local port="$1"
    local listeners

    listeners=$(listeners_for_port "$port")
    if [ -z "$listeners" ]; then
        return 0
    fi
    # A compatible Caddy already bound to the port is our own listener.
    if [ "$CADDY_STATE" = "compatible" ] && ! grep -qv 'users:(("caddy",' <<< "$listeners"; then
        return 0
    fi
    return 1
}

reload_or_start_caddy() {
    # Reload a running Caddy so existing sites keep serving without a drop.
    # Start it when it is not running, which is the case on a fresh install
    # where Caddy failed to bind its default port 80 because another service
    # already held it. A bare reload cannot bring a stopped Caddy up.
    if systemctl is-active --quiet caddy; then
        systemctl reload caddy
    else
        systemctl start caddy
    fi
}

select_https_port() {
    local candidate recorded

    # An explicit --https-port is honored as chosen, matching --port for the
    # backend: the administrator's selection is used without a free-port search.
    if [ "$HTTPS_PORT_WAS_SET" = true ]; then
        return 0
    fi

    # Updates keep the previously chosen HTTPS port, matching select_backend_port's
    # readback of the backend port from the deployed unit file. The prior port is
    # read from the deployed app config's site-opener line and reused only if it
    # is still available, so a dashboard that moved to a fallback port stays there
    # instead of reverting to 443 whenever 443 later frees up.
    if [ -f "$CADDY_APP_CONFIG" ] && [ ! -L "$CADDY_APP_CONFIG" ]; then
        recorded=$(grep -oE '^[A-Za-z0-9._-]+:[0-9]+ \{$' "$CADDY_APP_CONFIG" | head -n1 | sed -E 's/.*:([0-9]+) \{$/\1/')
        if [ -n "$recorded" ] && is_valid_port "$recorded" && https_port_available "$recorded"; then
            HTTPS_PORT="$recorded"
            return 0
        fi
    fi

    if https_port_available "$HTTPS_PORT_DEFAULT"; then
        HTTPS_PORT="$HTTPS_PORT_DEFAULT"
        return 0
    fi

    candidate=$HTTPS_PORT_MIN
    while [ "$candidate" -le "$HTTPS_PORT_MAX" ]; do
        if https_port_available "$candidate"; then
            HTTPS_PORT="$candidate"
            return 0
        fi
        candidate=$((candidate + 1))
    done

    echo "Error: no free HTTPS port found (tried ${HTTPS_PORT_DEFAULT} and ${HTTPS_PORT_MIN}-${HTTPS_PORT_MAX})."
    return 1
}

preflight_https() {
    if ! command -v ss &>/dev/null; then
        echo "Error: ss is required to inspect HTTPS listeners. Install iproute2 and re-run."
        return 1
    fi
    if ! SS_LISTENERS=$(ss -H -ltnp 2>/dev/null); then
        echo "Error: could not inspect TCP listeners with ss."
        return 1
    fi
    if ! inspect_caddy_state; then
        return 1
    fi
    if ! select_https_port; then
        return 1
    fi

    # Caddy's automatic HTTPS binds port 80 for HTTP-to-HTTPS redirects. If
    # port 80 is held by anything other than our own Caddy, disable those
    # redirects so an occupied port 80 does not break the install.
    if https_port_available 80; then
        HTTPS_DISABLE_REDIRECTS=false
    else
        HTTPS_DISABLE_REDIRECTS=true
    fi
}

install_caddy_if_fresh() {
    local caddy_key caddy_list base_caddyfile

    if [ "$CADDY_STATE" != "fresh" ]; then
        return 0
    fi
    if ! command -v apt-get &>/dev/null; then
        echo "Error: installing Caddy requires apt-get."
        return 1
    fi

    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg || return 1
    if ! command -v curl &>/dev/null || ! command -v gpg &>/dev/null; then
        echo "Error: Caddy prerequisites did not install curl and gpg."
        return 1
    fi
    caddy_key=$(mktemp) || return 1
    caddy_list=$(mktemp) || { rm -f -- "$caddy_key"; return 1; }
    trap 'rm -f -- "$caddy_key" "$caddy_list"' RETURN
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' -o "$caddy_key" || return 1
    gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg "$caddy_key" || return 1
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' -o "$caddy_list" || return 1
    install -m 0644 -- "$caddy_list" /etc/apt/sources.list.d/caddy-stable.list || return 1
    chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg || return 1
    chmod o+r /etc/apt/sources.list.d/caddy-stable.list || return 1
    apt-get update || return 1
    apt-get install -y caddy || return 1

    if ! inspect_caddy_state || [ "$CADDY_STATE" != "compatible" ]; then
        echo "Error: installed Caddy is not a compatible Caddyfile-managed systemd service."
        return 1
    fi

    # The apt package ships a stock Caddyfile with a default welcome site bound
    # to port 80. Left in place, that site competes for port 80 on every fresh
    # install, so replace it with a minimal, site-free base before anything is
    # layered on top of it.
    base_caddyfile=$(mktemp "$(dirname "$CADDY_CONFIG_PATH")/.simple-network-dashboard-Caddyfile-base.XXXXXX") || {
        echo "Error: could not stage a replacement Caddyfile."
        return 1
    }
    trap 'rm -f -- "$caddy_key" "$caddy_list" "$base_caddyfile"' RETURN
    if ! printf '# Simple Network Dashboard managed base Caddyfile\n' > "$base_caddyfile"; then
        echo "Error: could not write the replacement Caddyfile."
        return 1
    fi
    if ! install -m 0644 -- "$base_caddyfile" "$CADDY_CONFIG_PATH"; then
        echo "Error: could not publish the replacement Caddyfile."
        return 1
    fi
    DASHBOARD_INSTALLED_CADDY=true
}

verify_caddy_persistent_storage() {
    local service_user service_group service_environment account_home item
    local xdg_data_home_count=0
    local xdg_data_home_value=""
    local -a service_environment_items

    service_user=$(systemctl show --property=User --value caddy 2>/dev/null) || return 1
    service_group=$(systemctl show --property=Group --value caddy 2>/dev/null) || return 1
    service_environment=$(systemctl show --property=Environment --value caddy 2>/dev/null) || return 1
    account_home=$(getent passwd caddy | awk -F: '$1 == "caddy" { print $6; exit }') || return 1
    if [ "$service_user" != "caddy" ] || [ "$service_group" != "caddy" ] \
        || [ "$account_home" != "$CADDY_HOME" ]; then
        echo "Error: Caddy must use the standard caddy:caddy systemd identity and persistent data home ${CADDY_DATA_HOME}."
        echo "Refusing to modify a Caddy installation with non-standard certificate storage."
        return 1
    fi
    read -r -a service_environment_items <<< "$service_environment"
    for item in "${service_environment_items[@]}"; do
        item="${item#\"}"
        item="${item%\"}"
        if [[ "$item" == XDG_DATA_HOME=* ]]; then
            xdg_data_home_count=$((xdg_data_home_count + 1))
            xdg_data_home_value="${item#XDG_DATA_HOME=}"
        fi
    done
    if [ "$xdg_data_home_count" -gt 1 ] \
        || { [ "$xdg_data_home_count" -eq 1 ] && [ "$xdg_data_home_value" != "$CADDY_DATA_HOME" ]; }; then
        echo "Error: Caddy has a non-standard XDG_DATA_HOME override."
        echo "Refusing to modify a Caddy installation with non-standard certificate storage."
        return 1
    fi
    if [ -L "$CADDY_HOME" ] || [ ! -d "$CADDY_HOME" ]; then
        echo "Error: Caddy persistent home is unsafe or missing: $CADDY_HOME"
        return 1
    fi
}

count_caddy_imports() {
    awk -v comment="$CADDY_IMPORT_COMMENT" -v import_line="$CADDY_IMPORT_LINE" '
        $0 == import_line { total++ }
        previous == comment && $0 == import_line { managed++ }
        { previous=$0 }
        END { printf "%d %d\n", total + 0, managed + 0 }
    ' "$1"
}

caddy_global_block_state() {
    local file="$1" line trimmed last_comment=""

    while IFS= read -r line || [ -n "$line" ]; do
        trimmed=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
        [ -z "$trimmed" ] && continue
        if [[ "$trimmed" == \#* ]]; then
            last_comment="$trimmed"
            continue
        fi
        if [ "$trimmed" = "{" ]; then
            if [ "$last_comment" = "$CADDY_GLOBAL_MARKER" ]; then
                printf 'ours\n'
            else
                printf 'foreign\n'
            fi
            return 0
        fi
        printf 'none\n'
        return 0
    done < "$file"
    printf 'none\n'
}

# Succeed when the Caddyfile's first global options block already turns Caddy's
# automatic HTTP-to-HTTPS redirects off, so nothing needs adding to it.
caddy_global_block_disables_redirects() {
    local file="$1"

    awk '
        { line=$0; gsub(/^[[:space:]]+|[[:space:]]+$/, "", line) }
        !started {
            if (line == "" || line ~ /^#/) next
            if (line == "{") { started=1; depth=1; next }
            exit 1
        }
        {
            depth += gsub(/{/, "{", line)
            depth -= gsub(/}/, "}", line)
            if (line ~ /^auto_https[[:space:]]+disable_redirects([[:space:]]|$)/) found=1
            if (line ~ /^auto_https[[:space:]]+off([[:space:]]|$)/) found=1
            if (depth <= 0) exit (found ? 0 : 1)
        }
        END { exit (found ? 0 : 1) }
    ' "$file"
}

reconcile_caddy_global_block() {
    local file="$1" state dir tmp

    state=$(caddy_global_block_state "$file")
    dir=$(dirname "$file")

    if [ "$HTTPS_DISABLE_REDIRECTS" = true ]; then
        case "$state" in
            foreign)
                if ! caddy_global_block_disables_redirects "$file"; then
                    echo "Error: port 80 is in use, so Caddy's HTTP-to-HTTPS redirect must be turned off, but this Caddyfile already has its own global options block. Add the line 'auto_https disable_redirects' inside that block and re-run the installer." >&2
                    return 1
                fi
                ;;
            none)
                tmp=$(mktemp "$dir/.simple-network-dashboard-global.XXXXXX") || return 1
                if ! { printf '%s\n{\n    auto_https disable_redirects\n}\n\n' "$CADDY_GLOBAL_MARKER"; cat -- "$file"; } > "$tmp"; then
                    rm -f -- "$tmp"
                    return 1
                fi
                if ! cat -- "$tmp" > "$file"; then
                    rm -f -- "$tmp"
                    return 1
                fi
                rm -f -- "$tmp"
                ;;
            ours)
                :
                ;;
        esac
    else
        case "$state" in
            ours)
                tmp=$(mktemp "$dir/.simple-network-dashboard-global.XXXXXX") || return 1
                if ! awk -v marker="$CADDY_GLOBAL_MARKER" '
                    {
                        if (!started && $0 == marker) {
                            started = 1
                            removing = 1
                            next
                        }
                        if (removing) {
                            line = $0
                            gsub(/^[ \t]+|[ \t]+$/, "", line)
                            if (line == "}") {
                                removing = 0
                                skip_blank = 1
                            }
                            next
                        }
                        if (skip_blank) {
                            skip_blank = 0
                            if ($0 == "") next
                        }
                        print
                    }
                ' "$file" > "$tmp"; then
                    rm -f -- "$tmp"
                    return 1
                fi
                if ! cat -- "$tmp" > "$file"; then
                    rm -f -- "$tmp"
                    return 1
                fi
                rm -f -- "$tmp"
                ;;
            none|foreign)
                :
                ;;
        esac
    fi
}

discard_caddy_proxy_rollback() {
    if [ -n "$CADDY_ROLLBACK_CADDYFILE" ]; then
        rm -f -- "$CADDY_ROLLBACK_CADDYFILE" || return 1
    fi
    if [ -n "$CADDY_ROLLBACK_APP_CONFIG" ]; then
        rm -f -- "$CADDY_ROLLBACK_APP_CONFIG" || return 1
    fi
    CADDY_ROLLBACK_CADDYFILE=""
    CADDY_ROLLBACK_APP_CONFIG=""
    CADDY_ROLLBACK_HAD_APP_CONFIG=false
}

rollback_caddy_proxy_config() {
    local rollback_failed=false

    if [ -z "$CADDY_ROLLBACK_CADDYFILE" ] || [ -z "$CADDY_ROLLBACK_APP_CONFIG" ]; then
        echo "Error: Caddy rollback state is unavailable."
        return 1
    fi
    install -m 0644 -- "$CADDY_ROLLBACK_CADDYFILE" "$CADDY_CONFIG_PATH" || {
        echo "Error: previous Caddyfile could not be restored."
        rollback_failed=true
    }
    if [ "$CADDY_ROLLBACK_HAD_APP_CONFIG" = true ]; then
        install -m 0644 -- "$CADDY_ROLLBACK_APP_CONFIG" "$CADDY_APP_CONFIG" || {
            echo "Error: previous dashboard proxy config could not be restored."
            rollback_failed=true
        }
    else
        rm -f -- "$CADDY_APP_CONFIG" || {
            echo "Error: partial dashboard proxy config could not be removed."
            rollback_failed=true
        }
    fi
    reload_or_start_caddy || {
        echo "Error: rollback reload of Caddy failed."
        rollback_failed=true
    }
    if [ "$rollback_failed" = true ]; then
        echo "Caddy rollback files were retained at: $CADDY_ROLLBACK_CADDYFILE and $CADDY_ROLLBACK_APP_CONFIG"
        return 1
    fi
    discard_caddy_proxy_rollback
}

is_owned_caddy_app_config() {
    local metadata
    [ -L "$CADDY_APP_CONFIG" ] && return 1
    [ -f "$CADDY_APP_CONFIG" ] || return 1
    metadata=$(stat -c '%u:%g:%a' "$CADDY_APP_CONFIG") || return 1
    [ "$metadata" = "0:0:644" ] || return 1
    [ "$(head -n 1 "$CADDY_APP_CONFIG")" = "$CADDY_APP_MARKER" ]
}

write_caddy_proxy_config() {
    local config_dir staged_caddyfile validation_caddyfile staged_app_config backup_caddyfile backup_app_config had_app_config=false import_counts import_count managed_import_count validate_output

    config_dir=$(dirname "$CADDY_CONFIG_PATH")
    if [ ! -f "$CADDY_CONFIG_PATH" ] || [ -L "$CADDY_CONFIG_PATH" ]; then
        echo "Error: Caddyfile path is not a regular file: $CADDY_CONFIG_PATH"
        return 1
    fi
    if { [ -e "$CADDY_APP_CONFIG" ] || [ -L "$CADDY_APP_CONFIG" ]; } && ! is_owned_caddy_app_config; then
        echo "Error: existing dashboard Caddy proxy config is not a root-owned marked regular file."
        return 1
    fi
    staged_caddyfile=$(mktemp "$config_dir/.simple-network-dashboard-Caddyfile.XXXXXX") || return 1
    staged_app_config=$(mktemp "$config_dir/.simple-network-dashboard-proxy.XXXXXX") || {
        rm -f -- "$staged_caddyfile"
        return 1
    }
    validation_caddyfile=$(mktemp "$config_dir/.simple-network-dashboard-validation.XXXXXX") || { rm -f -- "$staged_caddyfile" "$staged_app_config"; return 1; }
    backup_caddyfile=$(mktemp "$config_dir/.simple-network-dashboard-Caddyfile-backup.XXXXXX") || { rm -f -- "$staged_caddyfile" "$staged_app_config" "$validation_caddyfile"; return 1; }
    backup_app_config=$(mktemp "$config_dir/.simple-network-dashboard-proxy-backup.XXXXXX") || { rm -f -- "$staged_caddyfile" "$staged_app_config" "$validation_caddyfile" "$backup_caddyfile"; return 1; }
    trap 'rm -f -- "$staged_caddyfile" "$validation_caddyfile" "$staged_app_config" "$backup_caddyfile" "$backup_app_config"' RETURN

    cp -- "$CADDY_CONFIG_PATH" "$staged_caddyfile" || return 1
    import_counts=$(count_caddy_imports "$staged_caddyfile") || return 1
    read -r import_count managed_import_count <<< "$import_counts"
    if [ "$import_count" -gt 0 ] && { [ "$import_count" -ne 1 ] || [ "$managed_import_count" -ne 1 ]; }; then
        echo "Error: Caddyfile contains an unowned or duplicate dashboard import; refusing to modify it."
        return 1
    fi
    if [ "$import_count" -eq 0 ]; then
        printf '\n%s\n%s\n' "$CADDY_IMPORT_COMMENT" "$CADDY_IMPORT_LINE" >> "$staged_caddyfile" || return 1
    fi
    if ! reconcile_caddy_global_block "$staged_caddyfile"; then
        return 1
    fi
    # Caddy calls the X-Forwarded-For header_up unnecessary, but it stays: a
    # shared Caddy whose global options set trusted_proxies would otherwise
    # forward a client-supplied value, and the dashboard trusts the header's
    # first entry from a loopback peer.
    if ! cat > "$staged_app_config" <<EOF
${CADDY_APP_MARKER}
${HTTPS_HOST}:${HTTPS_PORT} {
    tls internal
    reverse_proxy 127.0.0.1:${PORT} {
        header_up X-Forwarded-For {remote_host}
    }
}
EOF
    then
        return 1
    fi
    # Validate the exact combined shape before either managed file is published.
    # The validation copy alone points at the staged app snippet; the published
    # Caddyfile retains the stable app-owned import path.
    sed "s|^import ${CADDY_APP_CONFIG}$|import ${staged_app_config}|" "$staged_caddyfile" > "$validation_caddyfile" || return 1
    # Caddy's output is shown only when the check fails; successful validation
    # otherwise emits routine log chatter and a temporary-file formatting warning.
    if ! validate_output=$("$CADDY_BINARY" validate --config "$validation_caddyfile" --adapter caddyfile 2>&1); then
        printf '%s\n' "$validate_output"
        echo "Error: staged Caddy configuration is invalid; no Caddy configuration was changed."
        return 1
    fi

    cp -- "$CADDY_CONFIG_PATH" "$backup_caddyfile" || return 1
    if [ -f "$CADDY_APP_CONFIG" ]; then
        had_app_config=true
        cp -- "$CADDY_APP_CONFIG" "$backup_app_config" || return 1
    fi
    if ! install -m 0644 -- "$staged_app_config" "$CADDY_APP_CONFIG" \
        || ! install -m 0644 -- "$staged_caddyfile" "$CADDY_CONFIG_PATH"; then
        echo "Error: Caddy configuration publication failed; restoring the previous state."
        if ! install -m 0644 -- "$backup_caddyfile" "$CADDY_CONFIG_PATH"; then
            echo "Error: previous Caddyfile could not be restored."
        fi
        if [ "$had_app_config" = true ]; then
            install -m 0644 -- "$backup_app_config" "$CADDY_APP_CONFIG" || echo "Error: previous dashboard proxy config could not be restored."
        else
            rm -f -- "$CADDY_APP_CONFIG" || echo "Error: partial dashboard proxy config could not be removed."
        fi
        return 1
    fi
    if ! reload_or_start_caddy; then
        echo "Error: Caddy reload failed; restoring the previous dashboard proxy configuration."
        install -m 0644 -- "$backup_caddyfile" "$CADDY_CONFIG_PATH" || echo "Error: previous Caddyfile could not be restored."
        if [ "$had_app_config" = true ]; then
            install -m 0644 -- "$backup_app_config" "$CADDY_APP_CONFIG" || echo "Error: previous dashboard proxy config could not be restored."
        else
            rm -f -- "$CADDY_APP_CONFIG" || echo "Error: partial dashboard proxy config could not be removed."
        fi
        reload_or_start_caddy || echo "Error: rollback reload of Caddy also failed."
        return 1
    fi
    if ! systemctl is-active --quiet caddy; then
        echo "Error: Caddy did not come up after configuration; restoring the previous dashboard proxy configuration."
        install -m 0644 -- "$backup_caddyfile" "$CADDY_CONFIG_PATH" || echo "Error: previous Caddyfile could not be restored."
        if [ "$had_app_config" = true ]; then
            install -m 0644 -- "$backup_app_config" "$CADDY_APP_CONFIG" || echo "Error: previous dashboard proxy config could not be restored."
        else
            rm -f -- "$CADDY_APP_CONFIG" || echo "Error: partial dashboard proxy config could not be removed."
        fi
        reload_or_start_caddy || echo "Error: rollback reload of Caddy also failed."
        return 1
    fi
    CADDY_ROLLBACK_CADDYFILE="$backup_caddyfile"
    CADDY_ROLLBACK_APP_CONFIG="$backup_app_config"
    CADDY_ROLLBACK_HAD_APP_CONFIG="$had_app_config"
    trap 'rm -f -- "$staged_caddyfile" "$validation_caddyfile" "$staged_app_config"' RETURN
}

dashboard_ufw_rule_numbers() {
    ufw status numbered 2>/dev/null | awk -v comment="$UFW_COMMENT" '
        $0 ~ ("# " comment "[[:space:]]*$") { line=$0; sub(/^[[:space:]]*\[/, "", line); sub(/\].*/, "", line); gsub(/^[[:space:]]+|[[:space:]]+$/, "", line); print line }
    ' | sort -rn
}

delete_dashboard_ufw_rule_numbers() {
    local rule_numbers="$1"
    while IFS= read -r rule; do
        [ -n "$rule" ] || continue
        ufw --force delete "$rule" || return 1
    done <<< "$rule_numbers"
}

remove_dashboard_ufw_rules() {
    local rule_numbers
    rule_numbers=$(dashboard_ufw_rule_numbers) || return 1
    delete_dashboard_ufw_rule_numbers "$rule_numbers"
}

stale_dashboard_ufw_rule_numbers() {
    ufw status numbered 2>/dev/null | awk -v comment="$UFW_COMMENT" -v port="$HTTPS_PORT" '
        $0 ~ ("# " comment "[[:space:]]*$") {
            line=$0
            sub(/^[[:space:]]*\[/, "", line)
            number=line
            sub(/\].*/, "", number)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", number)
            sub(/^[^]]*\][[:space:]]*/, "", line)
            if (line !~ ("^" port "/")) print number
        }
    ' | sort -rn
}

configure_ufw_https() {
    local status stale_rule_numbers
    if ! command -v ufw &>/dev/null; then
        return 0
    fi
    status=$(ufw status 2>/dev/null) || return 1
    if ! grep -q '^Status: active' <<< "$status"; then
        return 0
    fi
    if ! ufw allow "${HTTPS_PORT}/tcp" comment "$UFW_COMMENT"; then
        echo "Error: could not add the dashboard HTTPS firewall rule; existing dashboard rules were preserved."
        return 1
    fi
    stale_rule_numbers=$(stale_dashboard_ufw_rule_numbers) || return 1
    if ! delete_dashboard_ufw_rule_numbers "$stale_rule_numbers"; then
        echo "Error: could not remove existing dashboard HTTPS firewall rules."
        return 1
    fi
}

# Ports (such as 8443/tcp) of the existing dashboard firewall rules, one per
# line. The IPv4 and IPv6 lines of one rule give the same port once.
dashboard_ufw_ports() {
    ufw status numbered 2>/dev/null | awk -v comment="$UFW_COMMENT" '
        $0 ~ ("# " comment "[[:space:]]*$") {
            line=$0
            sub(/^[[:space:]]*\[[^]]*\][[:space:]]*/, "", line)
            split(line, fields, /[[:space:]]+/)
            if (fields[1] != "") print fields[1]
        }
    ' | sort -u
}

# Numbers of dashboard firewall rules whose port is not in the list given as
# the argument (one port per line, none containing spaces), highest number first.
dashboard_ufw_rule_numbers_outside() {
    local keep
    keep=$(tr '\n' ' ' <<< "$1")
    ufw status numbered 2>/dev/null | awk -v comment="$UFW_COMMENT" -v keep="$keep" '
        BEGIN { n = split(keep, kept, " "); for (i = 1; i <= n; i++) if (kept[i] != "") allowed[kept[i]] = 1 }
        $0 ~ ("# " comment "[[:space:]]*$") {
            line=$0
            sub(/^[[:space:]]*\[/, "", line)
            number=line
            sub(/\].*/, "", number)
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", number)
            sub(/^[^]]*\][[:space:]]*/, "", line)
            split(line, fields, /[[:space:]]+/)
            if (!(fields[1] in allowed)) print number
        }
    ' | sort -rn
}

UFW_SNAPSHOT_TAKEN=false
UFW_SNAPSHOT_PORTS=""

# Records the dashboard firewall ports as they are before configure_ufw_https
# changes anything. Nothing is recorded when ufw is absent or inactive, since
# configure_ufw_https changes nothing then.
snapshot_dashboard_ufw_ports() {
    UFW_SNAPSHOT_TAKEN=false
    UFW_SNAPSHOT_PORTS=""
    if ! command -v ufw &>/dev/null; then
        return 0
    fi
    if ! ufw status 2>/dev/null | grep -q '^Status: active'; then
        return 0
    fi
    UFW_SNAPSHOT_PORTS=$(dashboard_ufw_ports) || return 0
    UFW_SNAPSHOT_TAKEN=true
}

# Puts the dashboard firewall rules back to the recorded ports: missing ones
# are added again, then rules for other ports are deleted. Returns 1 if any
# step fails, after trying the rest.
undo_dashboard_ufw_change() {
    local status=0 current port rule_numbers
    if [ "$UFW_SNAPSHOT_TAKEN" != true ]; then
        return 0
    fi
    current=$(dashboard_ufw_ports) || current=""
    while IFS= read -r port; do
        [ -n "$port" ] || continue
        if ! grep -qxF -- "$port" <<< "$current"; then
            if ! ufw allow "$port" comment "$UFW_COMMENT"; then
                echo "Error: could not restore the firewall rule for $port."
                status=1
            fi
        fi
    done <<< "$UFW_SNAPSHOT_PORTS"
    rule_numbers=$(dashboard_ufw_rule_numbers_outside "$UFW_SNAPSHOT_PORTS") || rule_numbers=""
    if ! delete_dashboard_ufw_rule_numbers "$rule_numbers"; then
        echo "Error: could not remove the dashboard firewall rules added by the update."
        status=1
    fi
    return "$status"
}

select_backend_port() {
    local candidate listening

    # Updates keep the service's existing backend port so its own listener does
    # not make every reinstall choose a different port.
    if [ -z "$FORCED_PORT" ] && [ -f "$UNIT_DEST" ]; then
        FORCED_PORT=$(grep -o -- '--port [0-9]*' "$UNIT_DEST" | awk '{print $2}')
        # Units installed before --port support always used port 3000.
        if [ -z "$FORCED_PORT" ]; then
            FORCED_PORT=3000
        fi
    fi

    if [ -n "$FORCED_PORT" ]; then
        PORT="$FORCED_PORT"
        return 0
    fi

    PORT=""
    listening=$(ss -tln 2>/dev/null || true)
    candidate=$PORT_MIN
    while [ "$candidate" -le "$PORT_MAX" ]; do
        if ! grep -qE ":${candidate}[[:space:]]" <<< "$listening"; then
            PORT="$candidate"
            return 0
        fi
        candidate=$((candidate + 1))
    done

    echo "Error: no free port found in range ${PORT_MIN}-${PORT_MAX}."
    echo "Free one up, or re-run with --port N to pick one manually."
    return 1
}

start_dashboard_backend() {
    if ! systemctl start "$SERVICE_NAME"; then
        echo "Error: dashboard service did not start on its loopback backend; HTTPS proxy was not published."
        return 1
    fi
    if ! wait_for_dashboard_ready "$PORT"; then
        echo "Error: dashboard service did not become ready on its loopback backend; HTTPS proxy was not published."
        return 1
    fi
}

export_caddy_root_certificate() {
    local staged_cert checksum root_metadata

    if ! command -v openssl &>/dev/null || ! command -v sha256sum &>/dev/null; then
        echo "Error: OpenSSL and sha256sum are required to validate and export Caddy's public root certificate."
        return 1
    fi
    if [ -L "$CADDY_ROOT_CERT" ] || [ ! -f "$CADDY_ROOT_CERT" ]; then
        echo "Error: Caddy's generated root certificate is missing or unsafe: $CADDY_ROOT_CERT"
        return 1
    fi
    root_metadata=$(stat -c '%U:%G:%a' "$CADDY_ROOT_CERT") || return 1
    if [ "$root_metadata" != "caddy:caddy:600" ]; then
        echo "Error: Caddy's generated root certificate has unsafe ownership or permissions."
        return 1
    fi
    if ! is_safe_runtime_file "$DASHBOARD_ROOT_CERT"; then
        echo "Error: dashboard root certificate destination is unsafe: $DASHBOARD_ROOT_CERT"
        return 1
    fi
    staged_cert=$(mktemp "${DATA_DIR}/.caddy-root-ca.XXXXXX") || return 1
    if ! openssl x509 -in "$CADDY_ROOT_CERT" -out "$staged_cert" \
        || ! openssl x509 -in "$staged_cert" -noout -ext basicConstraints | grep -q 'CA:TRUE' \
        || ! openssl verify -CAfile "$staged_cert" "$staged_cert"; then
        echo "Error: Caddy's root certificate could not be validated as a CA certificate."
        rm -f -- "$staged_cert"
        return 1
    fi
    checksum=$(sha256sum "$staged_cert") || {
        echo "Error: exported Caddy root certificate checksum could not be generated."
        rm -f -- "$staged_cert"
        return 1
    }
    checksum="${checksum%% *}"
    if ! chown snd:snd "$staged_cert" || ! chmod 600 "$staged_cert" \
        || ! mv -fT -- "$staged_cert" "$DASHBOARD_ROOT_CERT"; then
        echo "Error: Caddy root certificate could not be published safely."
        rm -f -- "$staged_cert"
        return 1
    fi
    echo "Exported Caddy root certificate file SHA-256 checksum: $checksum"
}

record_install_state() {
    local temp_state=""

    (
        umask 077
        mkdir -p "$INSTALL_STATE_DIR" || exit 1
        chown root:root "$INSTALL_STATE_DIR" || exit 1
        chmod 700 "$INSTALL_STATE_DIR" || exit 1
        temp_state=$(mktemp "${INSTALL_STATE_DIR}/.install-state.XXXXXX") || exit 1
        trap 'if [ -n "$temp_state" ]; then rm -f -- "$temp_state"; fi' EXIT
        printf 'RECORD_VERSION=2\nSND_UID=%s\nSND_GID=%s\nDASHBOARD_INSTALLED_CADDY=%s\n' \
            "$CURRENT_SND_UID" "$CURRENT_SND_GROUP_GID" "$DASHBOARD_INSTALLED_CADDY" > "$temp_state" || exit 1
        chown root:root "$temp_state" || exit 1
        chmod 600 "$temp_state" || exit 1
        mv -f "$temp_state" "$INSTALL_STATE" || exit 1
        temp_state=""
    )
}

# This reader validates the whole record but install.sh consumes only the
# recorded IDs. RECORDED_DASHBOARD_INSTALLED_CADDY is set for parity with
# uninstall.sh's reader, which does act on it.
# shellcheck disable=SC2034
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

is_safe_runtime_directory() {
    [ ! -L "$1" ] && { [ ! -e "$1" ] || [ -d "$1" ]; }
}

is_safe_runtime_file() {
    [ ! -L "$1" ] && { [ ! -e "$1" ] || [ -f "$1" ]; }
}

repair_runtime_storage_metadata() {
    python3 - "$DATA_DIR" "$LOG_DIR" <<'PY'
import os
import grp
import pwd
import stat
import sys

data_dir, log_dir = sys.argv[1:]
uid = pwd.getpwnam("snd").pw_uid
gid = grp.getgrnam("snd").gr_gid

def repair_directory(path):
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise RuntimeError(f"unsafe runtime directory: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise RuntimeError(f"unsafe runtime directory: {path}")
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)

def repair_file(path):
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise RuntimeError(f"unsafe runtime file: {path}")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise RuntimeError(f"unsafe runtime file: {path}")
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)

for directory in (data_dir, log_dir):
    repair_directory(directory)
for filename in ("devices.json", "known_hosts", "auth.json", "sessions.json"):
    repair_file(os.path.join(data_dir, filename))
PY
}

repair_runtime_storage() {
    local legacy_file
    local destination

    for destination in "$DATA_DIR" "$LOG_DIR"; do
        if ! is_safe_runtime_directory "$destination"; then
            echo "Error: private runtime directory $destination is unsafe. Refusing install."
            return 1
        fi
    done

    for legacy_file in devices.json known_hosts; do
        destination="$DATA_DIR/$legacy_file"
        if ! is_safe_runtime_file "$destination"; then
            echo "Error: private runtime file $destination is unsafe. Refusing install."
            return 1
        fi
        if [ "$ACCOUNT_STATE" != "fresh" ] && ! is_safe_runtime_file "$APP_DIR/$legacy_file"; then
            echo "Error: legacy runtime file $APP_DIR/$legacy_file is unsafe. Refusing install."
            return 1
        fi
    done

    if ! is_safe_runtime_file "$AUTH_FILE"; then
        echo "Error: private runtime file $AUTH_FILE is unsafe. Refusing install."
        return 1
    fi
    if ! is_safe_runtime_file "$SESSIONS_FILE"; then
        echo "Error: private runtime file $SESSIONS_FILE is unsafe. Refusing install."
        return 1
    fi

    mkdir -p "$DATA_DIR" "$LOG_DIR"

    if [ "$ACCOUNT_STATE" != "fresh" ]; then
        for legacy_file in devices.json known_hosts; do
            destination="$DATA_DIR/$legacy_file"
            if [ -f "$APP_DIR/$legacy_file" ] && [ ! -e "$destination" ]; then
                mv -nT -- "$APP_DIR/$legacy_file" "$destination"
            fi
        done
    fi

    repair_runtime_storage_metadata
}

copy_application_files() {
    local staged_package old_package

    if [ -z "${APP_DIR:-}" ] || [ "$APP_DIR" = "/" ]; then
        echo "Error: APP_DIR is empty or unsafe. Refusing to replace application files."
        return 1
    fi

    staged_package="$APP_DIR/.simple_network_dashboard.new"
    old_package="$APP_DIR/simple_network_dashboard"

    if [ -e "$staged_package" ] || [ -L "$staged_package" ]; then
        if [ ! -d "$staged_package" ] || [ -L "$staged_package" ]; then
            echo "Error: staged application path $staged_package is not a directory."
            return 1
        fi
        if ! rm -rf -- "$staged_package"; then
            echo "Error: could not remove leftover staged application path $staged_package."
            return 1
        fi
    fi

    if ! cp snd-reset-password requirements.txt uninstall.sh "$APP_DIR/"; then
        rm -rf -- "$staged_package"
        return 1
    fi
    if ! cp -R --no-preserve=ownership simple_network_dashboard "$staged_package"; then
        rm -rf -- "$staged_package"
        return 1
    fi
    if ! chown snd:snd "$staged_package" || ! chmod 2775 "$staged_package"; then
        rm -rf -- "$staged_package"
        return 1
    fi

    # On an update the previous package is already saved in the rollback
    # folder, so only an empty or unusable leftover can be in the way here.
    if [ -e "$old_package" ] || [ -L "$old_package" ]; then
        if [ ! -d "$old_package" ] || [ -L "$old_package" ]; then
            echo "Error: installed application path $old_package is not a directory."
            rm -rf -- "$staged_package"
            return 1
        fi
        if ! rm -rf -- "$old_package"; then
            echo "Error: could not remove the old application path $old_package."
            rm -rf -- "$staged_package"
            return 1
        fi
    fi

    if ! mv -T -- "$staged_package" "$old_package"; then
        rm -rf -- "$staged_package"
        return 1
    fi
}

remove_legacy_application_files() {
    local legacy_file legacy_path

    for legacy_file in auth.py config.py debug_log.py delivery_queue.py host_keys.py main.py metrics_poller.py persistence.py runtime_state.py session_manager.py ssh_manager.py ssh_session.py terminal_control.py terminal_history.py terminal_run.py update_check.py ws_manager.py; do
        legacy_path="$APP_DIR/$legacy_file"
        if [ -e "$legacy_path" ] || [ -L "$legacy_path" ]; then
            if [ ! -f "$legacy_path" ] || [ -L "$legacy_path" ]; then
                echo "Error: old application module $legacy_path is not a regular file."
                return 1
            fi
            if ! rm -- "$legacy_path"; then
                echo "Error: could not remove old application module $legacy_path."
                return 1
            fi
        fi
    done

    for legacy_path in "$APP_DIR/static" "$APP_DIR/__pycache__"; do
        if [ -e "$legacy_path" ] || [ -L "$legacy_path" ]; then
            if [ ! -d "$legacy_path" ] || [ -L "$legacy_path" ]; then
                echo "Error: old application path $legacy_path is not a directory."
                return 1
            fi
            if ! rm -rf -- "$legacy_path"; then
                echo "Error: could not remove old application path $legacy_path."
                return 1
            fi
        fi
    done
}

is_owned_reset_command() {
    local metadata

    [ -f "$RESET_COMMAND" ] && [ ! -L "$RESET_COMMAND" ] || return 1
    metadata=$(stat -c '%u:%g:%a' "$RESET_COMMAND") || return 1
    [ "$metadata" = "0:0:755" ] || return 1
    [ "$(sed -n '2p' "$RESET_COMMAND")" = "$RESET_COMMAND_MARKER" ]
}

is_verified_deployed_reset_command() {
    is_owned_reset_command \
        && [ -f "$APP_DIR/snd-reset-password" ] \
        && [ ! -L "$APP_DIR/snd-reset-password" ] \
        && cmp -s -- "$RESET_COMMAND" "$APP_DIR/snd-reset-password"
}

verify_reset_command_destination() {
    if { [ -e "$RESET_COMMAND" ] || [ -L "$RESET_COMMAND" ]; } \
        && ! is_verified_deployed_reset_command; then
        echo "Error: existing $RESET_COMMAND is not the dashboard-owned root command."
        echo "Refusing to replace it without a matching deployed dashboard wrapper."
        return 1
    fi
}

install_reset_command() {
    if { [ -e "$RESET_COMMAND" ] || [ -L "$RESET_COMMAND" ]; } \
        && ! is_owned_reset_command; then
        return 1
    fi
    install -o root -g root -m 0755 "$APP_DIR/snd-reset-password" "$RESET_COMMAND"
}

rollback_dir_path() {
    printf '%s\n' "${APP_DIR}.rollback"
}

# Delete one exact directory: it must sit directly under APP_DIR (or be the
# rollback folder) and must be a real directory, never a symlink.
remove_checked_directory() {
    local path="$1" rollback_dir

    if [ -z "${APP_DIR:-}" ] || [ "$APP_DIR" = "/" ]; then
        echo "Error: APP_DIR is empty or unsafe. Refusing to delete $path."
        return 1
    fi
    rollback_dir=$(rollback_dir_path)
    case "$path" in
        "$APP_DIR"/?*|"$rollback_dir") ;;
        *)
            echo "Error: refusing to delete unexpected path $path."
            return 1
            ;;
    esac
    if [ -L "$path" ]; then
        echo "Error: $path is a symbolic link, not a directory. It was not removed."
        return 1
    fi
    if [ ! -e "$path" ]; then
        return 0
    fi
    if [ ! -d "$path" ]; then
        echo "Error: $path is not a directory. It was not removed."
        return 1
    fi
    rm -rf -- "$path"
}

# Print the commands that put a saved copy back by hand.
print_rollback_restore_help() {
    local rollback_dir="$1"

    echo "To restore the previous version by hand:"
    echo "  sudo systemctl stop $SERVICE_NAME"
    echo "  sudo rm -r -- \"$APP_DIR/venv\" \"$APP_DIR/simple_network_dashboard\""
    echo "  sudo mv -T -- \"$rollback_dir/venv\" \"$APP_DIR/venv\""
    echo "  sudo mv -T -- \"$rollback_dir/simple_network_dashboard\" \"$APP_DIR/simple_network_dashboard\""
    echo "  (skip the last mv if the folder has no simple_network_dashboard; the file manifest lists what existed)"
    echo "  Then copy each saved file in $rollback_dir/files back with sudo cp -p, using these destinations"
    echo "  (remove the destination instead when the manifest marks it absent):"
    echo "    requirements -> $APP_DIR/requirements.txt"
    echo "    app-reset -> $APP_DIR/snd-reset-password"
    echo "    uninstall -> $APP_DIR/uninstall.sh"
    echo "    reset-command -> $RESET_COMMAND"
    echo "    unit -> $UNIT_DEST"
    echo "  sudo systemctl daemon-reload"
    echo "  sudo systemctl reset-failed $SERVICE_NAME"
    echo "  sudo systemctl start $SERVICE_NAME"
    echo "Once the restored dashboard is confirmed working, remove the saved copy:"
    echo "  sudo rm -r -- \"$rollback_dir\""
}

# Runs before anything is changed. A folder left by an unfinished update holds
# the only copy of the previous version, so it is never overwritten or deleted.
refuse_leftover_rollback_folder() {
    local rollback_dir

    rollback_dir=$(rollback_dir_path)
    if [ ! -e "$rollback_dir" ] && [ ! -L "$rollback_dir" ]; then
        return 0
    fi
    echo "Error: a previous update did not finish. Its saved copy of the old version is still at:"
    echo "  $rollback_dir"
    echo "Nothing was changed, and the installer will not overwrite or delete that folder."
    if [ -d "$rollback_dir" ] && [ ! -L "$rollback_dir" ]; then
        echo "It contains:"
        find "$rollback_dir" -mindepth 1 -maxdepth 1 -printf '  %M %u:%g %s %f\n' || true
    else
        echo "It is not a normal folder. Inspect it with: ls -la -- \"$rollback_dir\""
    fi
    print_rollback_restore_help "$rollback_dir"
    return 1
}

check_disk_space_for_update() {
    local venv_kb free_kb needed_kb

    if ! venv_kb=$(du -sk -- "$APP_DIR/venv" | awk 'NR == 1 { print $1 }') \
        || [[ ! "$venv_kb" =~ ^[0-9]+$ ]]; then
        echo "Error: could not measure the size of $APP_DIR/venv. Nothing was changed."
        return 1
    fi
    if ! free_kb=$(df -Pk -- "$APP_DIR" | awk 'NR == 2 { print $4 }') \
        || [[ ! "$free_kb" =~ ^[0-9]+$ ]]; then
        echo "Error: could not read the free disk space for $APP_DIR. Nothing was changed."
        return 1
    fi
    needed_kb=$((venv_kb + DISK_MARGIN_KB))
    if [ "$free_kb" -lt "$needed_kb" ]; then
        echo "Error: not enough free disk space to update safely."
        echo "The filesystem holding $APP_DIR has $((free_kb / 1024)) MB free; $((needed_kb / 1024)) MB is needed."
        echo "Free some space and run the installer again. Nothing was changed."
        return 1
    fi
}

detect_existing_installation() {
    local legacy_file has_application=false

    UPDATE_ELIGIBLE=false
    PACKAGE_WAS_INSTALLED=false
    if [ -d "$APP_DIR/simple_network_dashboard" ] && [ ! -L "$APP_DIR/simple_network_dashboard" ]; then
        PACKAGE_WAS_INSTALLED=true
        has_application=true
    else
        for legacy_file in main.py ws_manager.py; do
            if [ -f "$APP_DIR/$legacy_file" ] && [ ! -L "$APP_DIR/$legacy_file" ]; then
                has_application=true
            fi
        done
    fi
    if [ "$has_application" = true ] && [ -d "$APP_DIR/venv" ] && [ ! -L "$APP_DIR/venv" ]; then
        UPDATE_ELIGIBLE=true
    fi
}

rollback_small_file_path() {
    case "$1" in
        requirements) printf '%s\n' "$APP_DIR/requirements.txt" ;;
        app-reset) printf '%s\n' "$APP_DIR/snd-reset-password" ;;
        uninstall) printf '%s\n' "$APP_DIR/uninstall.sh" ;;
        reset-command) printf '%s\n' "$RESET_COMMAND" ;;
        unit) printf '%s\n' "$UNIT_DEST" ;;
        *) return 1 ;;
    esac
}

# The backend port the currently installed unit uses, empty when no unit exists.
read_unit_backend_port() {
    local port

    if [ ! -f "$UNIT_DEST" ]; then
        return 0
    fi
    port=$(grep -o -- '--port [0-9]*' "$UNIT_DEST" | awk '{print $2}' | head -n 1)
    # Units installed before --port support always used port 3000.
    printf '%s\n' "${port:-3000}"
}

# Put back whatever a half-built rollback point already took, then delete the
# folder only if everything went back. Always returns 1.
abandon_rollback_point_build() {
    local rollback_dir="$1" put_back_failed=false

    if [ "$ROLLBACK_VENV_MOVED" = true ]; then
        if mv -T -- "$rollback_dir/venv" "$APP_DIR/venv"; then
            ROLLBACK_VENV_MOVED=false
        else
            put_back_failed=true
        fi
    fi
    if [ "$ROLLBACK_PACKAGE_MOVED" = true ]; then
        if mv -T -- "$rollback_dir/simple_network_dashboard" "$APP_DIR/simple_network_dashboard"; then
            ROLLBACK_PACKAGE_MOVED=false
        else
            put_back_failed=true
        fi
    fi
    if [ "$put_back_failed" = true ]; then
        echo "Error: the saved copy could not be put back completely. The old files are in:"
        echo "  $rollback_dir"
        print_rollback_restore_help "$rollback_dir"
        return 1
    fi
    if ! remove_checked_directory "$rollback_dir"; then
        echo "Error: could not remove the unfinished rollback folder $rollback_dir."
        echo "The installed files are in place. Remove the folder once you have checked them."
    fi
    return 1
}

create_rollback_point() {
    local rollback_dir key source_path port

    ROLLBACK_ACTIVE=false
    ROLLBACK_PACKAGE_MOVED=false
    ROLLBACK_VENV_MOVED=false
    if [ -z "${APP_DIR:-}" ] || [ "$APP_DIR" = "/" ]; then
        echo "Error: APP_DIR is empty or unsafe. Refusing to build a rollback point."
        return 1
    fi
    rollback_dir=$(rollback_dir_path)
    if [ -e "$rollback_dir" ] || [ -L "$rollback_dir" ]; then
        echo "Error: $rollback_dir already exists. Nothing was changed."
        return 1
    fi
    if ! mkdir -m 0700 -- "$rollback_dir"; then
        echo "Error: could not create the rollback folder $rollback_dir. Nothing was changed."
        return 1
    fi
    if ! chown root:root -- "$rollback_dir" || ! chmod 0700 -- "$rollback_dir" \
        || ! mkdir -m 0700 -- "$rollback_dir/files"; then
        echo "Error: could not secure the rollback folder $rollback_dir."
        abandon_rollback_point_build "$rollback_dir"
        return 1
    fi

    : > "$rollback_dir/manifest" || { abandon_rollback_point_build "$rollback_dir"; return 1; }
    for key in requirements app-reset uninstall reset-command unit; do
        source_path=$(rollback_small_file_path "$key") || { abandon_rollback_point_build "$rollback_dir"; return 1; }
        if [ -L "$source_path" ] || { [ -e "$source_path" ] && [ ! -f "$source_path" ]; }; then
            echo "Error: $source_path is not a regular file. Nothing was changed."
            abandon_rollback_point_build "$rollback_dir"
            return 1
        fi
        if [ -e "$source_path" ]; then
            if ! cp -p -- "$source_path" "$rollback_dir/files/$key" \
                || ! cmp -s -- "$source_path" "$rollback_dir/files/$key" \
                || ! printf '%s present\n' "$key" >> "$rollback_dir/manifest"; then
                echo "Error: could not save a verified copy of $source_path. Nothing was changed."
                abandon_rollback_point_build "$rollback_dir"
                return 1
            fi
        elif ! printf '%s absent\n' "$key" >> "$rollback_dir/manifest"; then
            echo "Error: could not write the rollback manifest. Nothing was changed."
            abandon_rollback_point_build "$rollback_dir"
            return 1
        fi
    done

    if ! port=$(read_unit_backend_port) || ! printf '%s\n' "$port" > "$rollback_dir/port"; then
        echo "Error: could not record the current backend port. Nothing was changed."
        abandon_rollback_point_build "$rollback_dir"
        return 1
    fi
    if ! printf 'package %s\n' "$([ "$PACKAGE_WAS_INSTALLED" = true ] && echo present || echo absent)" \
        >> "$rollback_dir/manifest"; then
        echo "Error: could not write the rollback manifest. Nothing was changed."
        abandon_rollback_point_build "$rollback_dir"
        return 1
    fi

    if [ "$PACKAGE_WAS_INSTALLED" = true ]; then
        if ! mv -T -- "$APP_DIR/simple_network_dashboard" "$rollback_dir/simple_network_dashboard"; then
            echo "Error: could not move the installed application into the rollback folder."
            abandon_rollback_point_build "$rollback_dir"
            return 1
        fi
        ROLLBACK_PACKAGE_MOVED=true
    fi
    if ! mv -T -- "$APP_DIR/venv" "$rollback_dir/venv"; then
        echo "Error: could not move the Python environment into the rollback folder."
        abandon_rollback_point_build "$rollback_dir"
        return 1
    fi
    ROLLBACK_VENV_MOVED=true

    if ! : > "$rollback_dir/complete"; then
        echo "Error: could not finish the rollback point."
        abandon_rollback_point_build "$rollback_dir"
        return 1
    fi
    ROLLBACK_ACTIVE=true
}

discard_rollback_point() {
    local rollback_dir

    if [ "$ROLLBACK_ACTIVE" != true ]; then
        return 0
    fi
    rollback_dir=$(rollback_dir_path)
    if ! remove_checked_directory "$rollback_dir"; then
        echo "Error: could not remove the rollback folder $rollback_dir."
        echo "Remove it yourself once you have confirmed the dashboard works, or the next update will refuse to run."
        return 1
    fi
    ROLLBACK_ACTIVE=false
}

restore_rollback_small_file() {
    local rollback_dir="$1" key="$2" destination state temp_file

    destination=$(rollback_small_file_path "$key") || return 1
    state=$(awk -v key="$key" '$1 == key { print $2; exit }' "$rollback_dir/manifest") || return 1
    case "$state" in
        present)
            if [ -L "$rollback_dir/files/$key" ] || [ ! -f "$rollback_dir/files/$key" ]; then
                return 1
            fi
            temp_file="${destination}.restore.$$"
            if ! cp -p -- "$rollback_dir/files/$key" "$temp_file" \
                || ! cmp -s -- "$rollback_dir/files/$key" "$temp_file" \
                || ! mv -fT -- "$temp_file" "$destination"; then
                rm -f -- "$temp_file"
                return 1
            fi
            ;;
        absent)
            if [ -d "$destination" ] && [ ! -L "$destination" ]; then
                return 1
            fi
            rm -f -- "$destination"
            ;;
        *)
            return 1
            ;;
    esac
}

service_restart_count() {
    local count

    count=$(systemctl show -p NRestarts --value "$SERVICE_NAME") || return 1
    [[ "$count" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$count"
}

# One GET of /login on the loopback backend: no proxy, no redirects, short
# timeout. Succeeds only on HTTP 200.
dashboard_probe() {
    "$APP_DIR/venv/bin/python" -I -c "$READY_PROBE_SCRIPT" \
        "http://127.0.0.1:${1}/login" "$READY_REQUEST_TIMEOUT_SECONDS"
}

# Ready means: after the first HTTP 200, READY_CONFIRMATIONS more in a row,
# the service still active, and systemd's restart count unchanged since this
# check began (a crash loop restarts between probes). Gives up after
# READY_TIMEOUT_SECONDS.
wait_for_dashboard_ready() {
    local port="$1" restarts_before restarts_after streak=0 needed deadline

    if ! is_valid_port "$port"; then
        echo "Error: no valid backend port is known, so the dashboard cannot be checked."
        return 1
    fi
    if ! restarts_before=$(service_restart_count); then
        echo "Error: could not read the dashboard service restart count."
        return 1
    fi
    needed=$((READY_CONFIRMATIONS + 1))
    deadline=$((SECONDS + READY_TIMEOUT_SECONDS))
    while true; do
        if dashboard_probe "$port"; then
            streak=$((streak + 1))
        else
            streak=0
        fi
        if [ "$streak" -ge "$needed" ]; then
            break
        fi
        if [ "$SECONDS" -ge "$deadline" ]; then
            echo "Error: the dashboard did not answer steadily on http://127.0.0.1:${port}/login within ${READY_TIMEOUT_SECONDS} seconds."
            return 1
        fi
        sleep "$READY_POLL_SECONDS"
    done
    if ! restarts_after=$(service_restart_count) || [ "$restarts_before" != "$restarts_after" ]; then
        echo "Error: the dashboard service restarted while it was being checked."
        return 1
    fi
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        echo "Error: the dashboard service is not running."
        return 1
    fi
}

# Update failures only: put the saved package, environment and small files
# back, then confirm the old version answers on its old port.
restore_previous_installation() {
    local rollback_dir old_port key

    rollback_dir=$(rollback_dir_path)
    echo "The update failed. Restoring the previous version."
    if [ ! -f "$rollback_dir/complete" ]; then
        echo "Error: the rollback point at $rollback_dir is not complete, so it cannot be used automatically."
        print_rollback_restore_help "$rollback_dir"
        return 1
    fi
    ROLLBACK_ACTIVE=true

    if ! systemctl stop "$SERVICE_NAME"; then
        echo "Error: could not stop the dashboard service, so the previous version was not put back."
        echo "The previous version's saved files are in: $rollback_dir"
        print_rollback_restore_help "$rollback_dir"
        return 1
    fi
    if ! remove_checked_directory "$APP_DIR/simple_network_dashboard" \
        || ! remove_checked_directory "$APP_DIR/venv"; then
        echo "Error: could not remove the half-installed new version."
        echo "The previous version's saved files are in: $rollback_dir"
        print_rollback_restore_help "$rollback_dir"
        return 1
    fi
    if [ "$(awk '$1 == "package" { print $2; exit }' "$rollback_dir/manifest")" = present ]; then
        if ! mv -T -- "$rollback_dir/simple_network_dashboard" "$APP_DIR/simple_network_dashboard"; then
            echo "Error: could not move the previous application back."
            echo "The previous version's saved files are in: $rollback_dir"
            print_rollback_restore_help "$rollback_dir"
            return 1
        fi
    fi
    if ! mv -T -- "$rollback_dir/venv" "$APP_DIR/venv"; then
        echo "Error: could not move the previous Python environment back."
        echo "The previous version's saved files are in: $rollback_dir"
        print_rollback_restore_help "$rollback_dir"
        return 1
    fi
    # The reset command copies are restored back to back so the two always
    # match, as the uninstaller and the next update require.
    for key in requirements uninstall app-reset reset-command unit; do
        if ! restore_rollback_small_file "$rollback_dir" "$key"; then
            echo "Error: could not restore $(rollback_small_file_path "$key")."
            echo "The previous version's saved files are in: $rollback_dir"
            print_rollback_restore_help "$rollback_dir"
            return 1
        fi
    done
    if ! systemctl daemon-reload; then
        echo "Error: systemd could not reload the restored service definition."
        echo "The previous version's saved files are in: $rollback_dir"
        print_rollback_restore_help "$rollback_dir"
        return 1
    fi
    # A crash loop leaves the unit in a failed state that blocks a new start.
    systemctl reset-failed "$SERVICE_NAME" || true

    if [ "$DASHBOARD_WAS_ACTIVE" = true ]; then
        old_port=$(tr -d '[:space:]' < "$rollback_dir/port") || old_port=""
        if ! systemctl start "$SERVICE_NAME" || ! wait_for_dashboard_ready "$old_port"; then
            echo "Error: the previous files were put back, but the dashboard is not responding."
            echo "The saved copy was kept at: $rollback_dir"
            echo "Check: sudo systemctl status $SERVICE_NAME and sudo journalctl -u $SERVICE_NAME -n 50"
            return 1
        fi
        echo "Previous version restored and responding."
    else
        echo "The previous files were put back. The service was left stopped, as it was before the update."
    fi
    # The previous version is back; a folder that cannot be deleted is reported
    # by discard_rollback_point and does not change that.
    discard_rollback_point || true
}

# Called when an update step fails. Fresh installs have no rollback point and
# keep the plain report-and-exit behavior.
restore_after_failed_update() {
    if [ "$ROLLBACK_ACTIVE" != true ]; then
        return 0
    fi
    restore_previous_installation
}

initialize_authentication() {
    if [ -e "$AUTH_FILE" ]; then
        if ! (cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -m simple_network_dashboard.auth validate --auth-file "$AUTH_FILE"); then
            echo "Error: existing authentication state is malformed or unsafe. It was not changed."
            echo "Inspect $AUTH_FILE and restore a valid state before re-running the installer."
            return 1
        fi
        echo "Existing dashboard password state was preserved."
        return 0
    fi

    if ! (cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -m simple_network_dashboard.auth initialize --auth-file "$AUTH_FILE"); then
        echo "Error: dashboard password initialization did not complete. See the error above before continuing."
        return 1
    fi
}

check_python_version() {
    local version major minor
    version=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null) || version=""
    if ! [[ "$version" =~ ^[0-9]+\.[0-9]+$ ]]; then
        echo "Error: could not read the version of python3."
        return 1
    fi
    major="${version%%.*}"
    minor="${version#*.}"
    if (( major < MIN_PYTHON_MAJOR || (major == MIN_PYTHON_MAJOR && minor < MIN_PYTHON_MINOR) )); then
        echo "Error: Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR} or newer is required; this system has Python ${version}."
        echo "Use an operating system release that ships it, such as Debian 12, Raspberry Pi OS (Debian 12 based), or Ubuntu 24.04."
        return 1
    fi
}

write_service_unit() {
    local temp_unit

    temp_unit=$(mktemp "${UNIT_DEST}.XXXXXX") || return 1
    if ! cat > "$temp_unit" << EOF
[Unit]
Description=Simple Network Dashboard
After=network.target

[Service]
User=snd
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m simple_network_dashboard --port ${PORT}
Environment=SND_PUBLIC_ORIGIN=${PUBLIC_ORIGIN}
Restart=always
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
    then
        rm -f -- "$temp_unit"
        return 1
    fi
    if ! chmod 0644 "$temp_unit" || ! mv -fT -- "$temp_unit" "$UNIT_DEST"; then
        rm -f -- "$temp_unit"
        return 1
    fi
}

# Every step is checked and reports its own error; any failure returns 1 so
# the caller can restore the previous version.
apply_new_version() {
    if ! copy_application_files; then
        echo "Error: application files could not be copied."
        return 1
    fi

    # Virtual environments embed absolute paths in their scripts, so the new one
    # is built at its final location. The old one only ever moves back to its
    # original path.
    if ! sudo -u snd python3 -m venv "$APP_DIR/venv"; then
        echo "Error: the Python virtual environment could not be created."
        return 1
    fi
    if ! sudo -u snd "$APP_DIR/venv/bin/pip" install --require-hashes -r "$APP_DIR/requirements.txt" --quiet --no-cache-dir; then
        echo "Error: the dashboard's Python packages could not be installed."
        return 1
    fi

    if ! initialize_authentication; then
        return 1
    fi
    if ! install_reset_command; then
        echo "Error: dashboard password-reset command installation failed."
        return 1
    fi

    # Create systemd service file
    if ! write_service_unit; then
        echo "Error: the systemd service file could not be written."
        return 1
    fi
    if ! systemctl daemon-reload; then
        echo "Error: systemd could not reload the service definition."
        return 1
    fi
    if ! systemctl enable "$SERVICE_NAME" --quiet; then
        echo "Error: the dashboard service could not be enabled."
        return 1
    fi
    if ! start_dashboard_backend; then
        return 1
    fi
}

# Updates save the old package, environment and small files before anything is
# replaced, so a failed update can put the working version back. The saved copy
# is kept until main() has published the dashboard and the update has fully
# landed. Returns 1 on any failure, after restoring the previous version when
# there is one.
update_application() {
    if [ "$UPDATE_ELIGIBLE" = true ]; then
        if ! create_rollback_point; then
            echo "Error: the rollback point could not be built, so the update was not started."
            if [ "$DASHBOARD_WAS_ACTIVE" = true ]; then
                if systemctl start "$SERVICE_NAME"; then
                    echo "The dashboard service was started again."
                else
                    echo "Error: the dashboard service could not be started again. Run: sudo systemctl status $SERVICE_NAME"
                fi
            fi
            return 1
        fi
    fi

    if ! apply_new_version; then
        restore_after_failed_update || true
        return 1
    fi
}

# Publishes the dashboard through Caddy and the firewall. Returns 1 on any
# failure, after restoring the Caddy configuration and the firewall rules this
# step changed. The caller restores the previous version.
publish_dashboard_endpoint() {
    if ! write_caddy_proxy_config; then
        return 1
    fi
    if ! export_caddy_root_certificate; then
        echo "Error: Caddy root certificate export failed; restoring the dashboard proxy configuration before firewall changes."
        if ! rollback_caddy_proxy_config; then
            echo "Error: Caddy rollback after certificate export failure was incomplete."
        fi
        return 1
    fi
    snapshot_dashboard_ufw_ports
    if ! configure_ufw_https; then
        undo_dashboard_ufw_change || true
        if ! rollback_caddy_proxy_config; then
            echo "Error: Caddy rollback after firewall failure was incomplete."
        fi
        return 1
    fi
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
if ! parse_install_arguments "$@"; then
    exit 1
fi
if ! verify_reset_command_destination; then
    exit 1
fi
# A folder left by an unfinished update is the only copy of the old version.
if ! refuse_leftover_rollback_folder; then
    exit 1
fi

# This preflight deliberately precedes account classification and every
# installation mutation so a port or Caddy conflict is safe to resolve.
if ! preflight_https; then
    exit 1
fi
PUBLIC_ORIGIN="https://${HTTPS_HOST,,}"
if [ "$HTTPS_PORT" -ne 443 ]; then
    PUBLIC_ORIGIN="${PUBLIC_ORIGIN}:${HTTPS_PORT}"
fi
if ! install_caddy_if_fresh; then
    exit 1
fi
if ! verify_caddy_persistent_storage; then
    exit 1
fi

# ---------------------------------------------------------------------------
# Pick a port
# ---------------------------------------------------------------------------
if ! select_backend_port; then
    exit 1
fi

echo "Installing Simple Network Dashboard..."

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install it with: sudo apt install python3 python3-venv"
    exit 1
fi

if ! check_python_version; then
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

# Decide from what is actually installed whether this is an update that can be
# rolled back, before anything below creates files in the application folder.
detect_existing_installation
if [ "$UPDATE_ELIGIBLE" = true ] && ! check_disk_space_for_update; then
    exit 1
fi

if [ "$ACCOUNT_STATE" = "managed-update" ] && [ "$DASHBOARD_INSTALLED_CADDY" = true ]; then
    if ! record_install_state; then
        echo "Error: could not record that this dashboard installation installed Caddy."
        exit 1
    fi
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
mkdir -p "$APP_DIR/simple_network_dashboard"
chown snd:snd "$APP_DIR" "$APP_DIR/simple_network_dashboard"
chmod 2775 "$APP_DIR" "$APP_DIR/simple_network_dashboard"

# Stop the service (handles re-runs for updates). An update stops it even when
# it is not active, so a crash-looping service cannot restart over the files
# being moved. The stop must succeed before any file is touched.
if [ "$UPDATE_ELIGIBLE" = true ] && [ -e "$UNIT_DEST" ]; then
    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        DASHBOARD_WAS_ACTIVE=true
    fi
    if ! systemctl stop "$SERVICE_NAME"; then
        echo "Error: could not stop the dashboard service. No files were changed."
        exit 1
    fi
elif systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    DASHBOARD_WAS_ACTIVE=true
    if ! systemctl stop "$SERVICE_NAME"; then
        echo "Error: could not stop the dashboard service. No files were changed."
        exit 1
    fi
fi

# Keep private runtime state outside the replaceable application directory.
repair_runtime_storage

if ! update_application; then
    exit 1
fi
if ! publish_dashboard_endpoint; then
    restore_after_failed_update || true
    exit 1
fi
# The new version is fully working. The saved copy and the old flat layout go
# last, in this order, because a restored old version runs from them. Leftover
# Caddy temporary files do not block the rest, since a leftover saved copy
# would make the next update refuse to run.
cleanup_failed=false
if ! discard_caddy_proxy_rollback; then
    echo "Error: could not remove temporary Caddy rollback files."
    cleanup_failed=true
fi
if ! discard_rollback_point; then
    exit 1
fi
if ! remove_legacy_application_files; then
    echo "Error: legacy application cleanup failed."
    exit 1
fi
if [ "$cleanup_failed" = true ]; then
    exit 1
fi

echo ""
echo "Simple Network Dashboard is running."
echo "Open https://${HTTPS_HOST}:${HTTPS_PORT} in your browser."
echo "Certificate setup: https://${HTTPS_HOST}:${HTTPS_PORT}/certificate-setup"
echo "That page has step-by-step trust instructions for Windows, Linux, and macOS."
echo "Verify the certificate against the SHA-256 checksum printed above before importing it."
echo "To uninstall later: sudo bash $APP_DIR/uninstall.sh"
if [ "$ADDED_TO_GROUP" = true ]; then
    echo ""
    echo "Note: $INSTALL_USER was added to the snd group. Log out and back in for this to take effect."
fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
