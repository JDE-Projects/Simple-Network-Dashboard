"""Shell contracts for provenance-gated Caddy removal during uninstall."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _find_bash() -> str | None:
    if os.name == "nt":
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        if git_bash.exists():
            return str(git_bash)
    return shutil.which("bash")


BASH = _find_bash()


def _run_shell(body: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise the Caddy uninstall contracts")

    script = f"""
source ./uninstall.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
{body}
"""
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_blank_or_comment_only_caddyfile_is_safe_to_remove() -> None:
    result = _run_shell(
        """
cat > "$TEST_ROOT/Caddyfile" <<'EOF'
# Caddy configuration left after dashboard removal

    # Another comment
EOF
inspect_caddy_for_cleanup() { CADDY_CONFIG_PATH="$TEST_ROOT/Caddyfile"; }
CADDY_REMOVAL_ELIGIBLE=true
CADDY_INTENT=remove
caddy_config_is_safe_to_remove
printf 'safe=yes\\n'
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "safe=yes\n"


@pytest.mark.parametrize(
    "caddyfile",
    ["example.test { respond \\\"still in use\\\" }\n", "import /etc/caddy/other-sites.caddy\n"],
)
def test_used_or_foreign_import_caddyfile_is_kept(caddyfile: str) -> None:
    result = _run_shell(
        f"""
printf '%s' '{caddyfile}' > "$TEST_ROOT/Caddyfile"
inspect_caddy_for_cleanup() {{ CADDY_CONFIG_PATH="$TEST_ROOT/Caddyfile"; }}
CADDY_REMOVAL_ELIGIBLE=true
CADDY_INTENT=remove
if caddy_config_is_safe_to_remove; then exit 10; fi
"""
    )

    assert result.returncode == 0, result.stderr
    assert "Caddy was kept because other sites still use it." in result.stdout


@pytest.mark.parametrize("provenance", ["false", "unknown", ""])
def test_unverified_caddy_provenance_is_never_eligible(provenance: str) -> None:
    result = _run_shell(
        f"""
CADDY_INSTALL_STATE_READ_SUCCEEDED=true
RECORDED_DASHBOARD_INSTALLED_CADDY='{provenance}'
resolve_caddy_removal_eligibility
printf 'eligible=%s\\n' "$CADDY_REMOVAL_ELIGIBLE"
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "eligible=false\n"


def test_remove_caddy_flag_without_verified_provenance_keeps_caddy() -> None:
    result = _run_shell(
        """
CADDY_INSTALL_STATE_READ_SUCCEEDED=true
RECORDED_DASHBOARD_INSTALLED_CADDY=false
parse_uninstall_flags --remove-caddy
resolve_caddy_removal_eligibility
resolve_caddy_removal_intent
printf 'intent=%s\\n' "$CADDY_INTENT"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "no record that this dashboard installed it" in result.stdout
    assert result.stdout.endswith("intent=keep\n")


def test_yes_alone_and_empty_interactive_answer_keep_caddy() -> None:
    result = _run_shell(
        """
CADDY_INSTALL_STATE_READ_SUCCEEDED=true
RECORDED_DASHBOARD_INSTALLED_CADDY=true
parse_uninstall_flags --yes
resolve_caddy_removal_eligibility
resolve_caddy_removal_intent
printf 'yes_intent=%s\\n' "$CADDY_INTENT"
parse_uninstall_flags
resolve_caddy_removal_eligibility
resolve_caddy_removal_intent <<< ''
printf 'interactive_intent=%s\\n' "$CADDY_INTENT"
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "yes_intent=keep\ninteractive_intent=keep\n"


def test_caddy_removal_uses_remove_not_purge_and_leaves_identity() -> None:
    result = _run_shell(
        """
CALLS="$TEST_ROOT/calls"
systemctl() { printf 'systemctl:%s\\n' "$*" >> "$CALLS"; return 1; }
apt-get() { printf 'apt-get:%s\\n' "$*" >> "$CALLS"; }
rm() { printf 'rm:%s\\n' "$*" >> "$CALLS"; }
userdel() { printf 'userdel:%s\\n' "$*" >> "$CALLS"; }
groupdel() { printf 'groupdel:%s\\n' "$*" >> "$CALLS"; }
[() {
    if builtin test "$1" = -e && builtin test "$2" = /var/lib/caddy; then return 0; fi
    if builtin test "$1" = -d && builtin test "$2" = /var/lib/caddy; then return 0; fi
    if builtin test "$1" = -L && builtin test "$2" = /var/lib/caddy; then return 1; fi
    if builtin test "$1" = ! && builtin test "$2" = -e && builtin test "$3" = /var/lib/caddy; then return 1; fi
    if builtin test "$1" = ! && builtin test "$2" = -d && builtin test "$3" = /var/lib/caddy; then return 1; fi
    if builtin test "$1" = ! && builtin test "$2" = -L && builtin test "$3" = /var/lib/caddy; then return 0; fi
    builtin [ "$@"
}
remove_dashboard_caddy
cat "$CALLS"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "apt-get:remove -y caddy" in result.stdout
    assert "purge" not in result.stdout
    assert "autoremove" not in result.stdout
    assert "rm:-rf -- /var/lib/caddy" in result.stdout
    assert "rm:-f -- /etc/apt/sources.list.d/caddy-stable.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg" in result.stdout
    assert "userdel:" not in result.stdout
    assert "groupdel:" not in result.stdout
    assert "caddy service account and group were intentionally left in place" in result.stdout


@pytest.mark.parametrize("failure", ["remove", "home"])
def test_caddy_removal_failure_is_loud_and_never_claims_success(failure: str) -> None:
    result = _run_shell(
        f"""
apt-get() {{ [ '{failure}' = remove ] && return 1; return 0; }}
rm() {{
    if [ '{failure}' = home ] && [ "$1" = -rf ] && [ "$3" = /var/lib/caddy ]; then return 1; fi
    return 0
}}
[() {{
    if builtin test "$1" = -e && builtin test "$2" = /var/lib/caddy; then return 0; fi
    if builtin test "$1" = -d && builtin test "$2" = /var/lib/caddy; then return 0; fi
    if builtin test "$1" = -L && builtin test "$2" = /var/lib/caddy; then return 1; fi
    if builtin test "$1" = ! && builtin test "$2" = -e && builtin test "$3" = /var/lib/caddy; then return 1; fi
    if builtin test "$1" = ! && builtin test "$2" = -d && builtin test "$3" = /var/lib/caddy; then return 1; fi
    if builtin test "$1" = ! && builtin test "$2" = -L && builtin test "$3" = /var/lib/caddy; then return 0; fi
    builtin [ "$@"
}}
if remove_dashboard_caddy; then exit 10; fi
printf 'status=%s\\n' "$CADDY_REMOVAL_STATUS"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "Error: Caddy cleanup incomplete:" in result.stdout
    assert "Caddy and its certificate store were removed." not in result.stdout
    assert result.stdout.endswith("status=failed\n")


def test_caddy_removal_treats_already_removed_store_as_success() -> None:
    # apt-get remove may delete /var/lib/caddy and /etc/caddy via package hooks
    # before this code runs. An already-absent path must count as success, not a
    # false failure, and the apt source files must still be removed.
    result = _run_shell(
        """
CALLS="$TEST_ROOT/calls"
systemctl() { return 1; }
apt-get() { printf 'apt-get:%s\\n' "$*" >> "$CALLS"; return 0; }
rm() { printf 'rm:%s\\n' "$*" >> "$CALLS"; return 0; }
[() {
    if builtin test "$1" = -e && builtin test "$2" = /var/lib/caddy; then return 1; fi
    if builtin test "$1" = -L && builtin test "$2" = /var/lib/caddy; then return 1; fi
    if builtin test "$1" = ! && builtin test "$2" = -e && builtin test "$3" = /var/lib/caddy; then return 0; fi
    if builtin test "$1" = ! && builtin test "$2" = -L && builtin test "$3" = /var/lib/caddy; then return 0; fi
    builtin [ "$@"
}
remove_dashboard_caddy
printf 'status=%s\\n' "$CADDY_REMOVAL_STATUS"
cat "$CALLS"
"""
    )

    assert result.returncode == 0, result.stderr
    assert "Error:" not in result.stdout
    assert "rm:-rf -- /var/lib/caddy" not in result.stdout
    assert "rm:-f -- /etc/apt/sources.list.d/caddy-stable.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg" in result.stdout
    assert "status=removed\n" in result.stdout
