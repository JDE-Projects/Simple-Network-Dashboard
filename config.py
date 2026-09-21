"""Application configuration constants."""

import os


def _ws_revalidate_seconds() -> float:
    """Return the live WebSocket session revalidation interval."""
    try:
        seconds = float(os.environ.get("SND_WS_REVALIDATE_SECONDS", ""))
    except ValueError:
        return 10
    return seconds if seconds > 0 else 10


METRICS_INTERVAL = 2  # seconds between polls for the selected device
WS_RELEASE_GRACE_SECONDS = 15  # grace period before a disconnected browser's SSH sessions are released, lets a page refresh reconnect without losing sessions
WS_REVALIDATE_SECONDS = _ws_revalidate_seconds()  # how often a live WebSocket re-checks that the session token it connected with is still valid
MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MB cap on incoming HTTP request bodies
MAX_DEVICES = 250  # cap on total saved devices
MAX_COMMANDS_PER_DEVICE = 250  # cap on saved commands per device, per request
MAX_NAME_LEN = 80  # device name / saved command name character cap
MAX_HOST_LEN = 253  # hostname character cap (RFC 1035 full name length)
MAX_USERNAME_LEN = 64  # SSH username character cap
MAX_COMMAND_LEN = 4096  # saved command text / run command character cap
MAX_CONFIRM_LEN = 500  # saved command confirmation prompt character cap
MIN_METRICS_PORT = 1
MAX_METRICS_PORT = 65535

APP_NAME    = "Simple Network Dashboard"
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
# Runtime storage roots. Default to the installed Linux locations; override with
# SND_DATA_DIR / SND_LOG_DIR to run the app off Linux (local development, tests).
DATA_DIR = os.environ.get("SND_DATA_DIR", "/var/lib/simple-network-dashboard")
LOG_DIR = os.environ.get("SND_LOG_DIR", "/var/log/simple-network-dashboard")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
DASHBOARD_ROOT_CERT = os.path.join(DATA_DIR, "caddy-root-ca.crt")
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
SESSION_COOKIE_NAME = "__Host-snd-session"
CSRF_COOKIE_NAME = "__Host-snd-csrf"
CSRF_HEADER_NAME = "x-csrf-token"
PUBLIC_ORIGIN_ENV = "SND_PUBLIC_ORIGIN"
WS_POLICY_VIOLATION_CODE = 1008
_PUBLIC_PATHS = {
    "/login",
    "/api/auth/login",
    "/api/auth/session",
    "/certificate-setup",
    "/certificate-setup/caddy-root-ca.crt",
}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'{wss_source}; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)
