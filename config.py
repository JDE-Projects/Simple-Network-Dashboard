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
METRICS_FETCH_DEADLINE = 8  # seconds from connection through the last metrics response byte
METRICS_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # cap metrics responses to bound memory use
METRICS_MAX_CONCURRENT_FETCHES = 8  # cap simultaneous exporter requests per polling cycle
DELIVERY_QUEUE_MAX_BYTES = 4 * 1024 * 1024  # cap queued worker-to-loop delivery bytes
DELIVERY_QUEUE_MAX_ITEMS = 4096  # cap queued worker-to-loop delivery items
DELIVERY_RESERVED_FRACTION = 0.10  # reserve delivery capacity for non-terminal messages
SOCKET_QUEUE_MAX_BYTES = 4 * 1024 * 1024  # cap queued outbound bytes per socket
SOCKET_SEND_DEADLINE = 10  # seconds before a stalled socket send is closed
TERM_BATCH_INTERVAL = 0.03  # seconds before buffered terminal output is sent
TERM_BATCH_MAX_CHARS = 32768  # character cap for one terminal output batch
TERM_UNACKED_MAX_BYTES = 512 * 1024  # terminal output bytes awaiting a browser write acknowledgement before SSH reading pauses
TERM_ACK_STALL_SECONDS = 20  # seconds without terminal acknowledgement progress before one run temporarily resumes reading
TERM_HISTORY_MAX_LINES = 1500  # complete terminal history lines kept for page refresh
TERM_HISTORY_MAX_LINE_CHARS = 4096  # character cap for one terminal history line, dropping excess input
TERM_INIT_HISTORY_MAX_CHARS = 1_048_576  # encoded size cap for all history lines in one init frame, oldest dropped first, kept well under SOCKET_QUEUE_MAX_BYTES
TERM_DEFAULT_COLS = 120  # columns used when the browser provides no terminal size
TERM_DEFAULT_ROWS = 30  # rows used when the browser provides no terminal size
TERM_MIN_COLS = 20  # smallest accepted terminal column count
TERM_MAX_COLS = 500  # largest accepted terminal column count
TERM_MIN_ROWS = 5  # smallest accepted terminal row count
TERM_MAX_ROWS = 200  # largest accepted terminal row count
TERM_INPUT_MAX_CHARS = 16384  # character cap for one terminal input message
TERM_PENDING_INPUT_MAX_BYTES = 65536  # byte cap for terminal input awaiting a device write
TERM_WRITE_DEADLINE = 10  # seconds before a blocked terminal write stops the run
TERM_SEND_TIMEOUT = 1  # seconds per channel send so the writer can recheck its deadline
TERM_STOP_GRACE_SECONDS = 3  # seconds allowed for a command to react to Ctrl+C
TERM_SILENCE_SECONDS = 1800  # seconds of terminal silence before a busy session becomes idle
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
