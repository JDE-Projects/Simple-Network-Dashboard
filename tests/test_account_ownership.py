"""Executable account-ownership contracts for the Linux installer scripts."""

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


def _run_shell(script_name: str, body: str) -> subprocess.CompletedProcess[str]:
    if BASH is None:
        pytest.skip("Bash is required to exercise the installer shell contracts")

    script = f"""
source ./{script_name}
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
APP_DIR="$TEST_ROOT/app"
UNIT_DEST="$TEST_ROOT/unit"
INSTALL_STATE_DIR="$TEST_ROOT/etc"
INSTALL_STATE="$INSTALL_STATE_DIR/install-state"
MOCK_USER=false
MOCK_GROUP=false
MOCK_UID=999
MOCK_GID=998
MOCK_STAT='0:0:600'

id() {{
    if [ "$1" = snd ] && [ "$MOCK_USER" = true ]; then return 0; fi
    if [ "$1" = -u ] && [ "$2" = snd ] && [ "$MOCK_USER" = true ]; then
        printf '%s\\n' "$MOCK_UID"; return 0
    fi
    if [ "$1" = -g ] && [ "$2" = snd ] && [ "$MOCK_USER" = true ]; then
        printf '%s\\n' "$MOCK_GID"; return 0
    fi
    return 1
}}
getent() {{
    if [ "$1" = group ] && [ "$2" = snd ] && [ "$MOCK_GROUP" = true ]; then
        printf 'snd:x:%s:\\n' "$MOCK_GID"; return 0
    fi
    return 2
}}
stat() {{ printf '%s\\n' "$MOCK_STAT"; }}
chown() {{ :; }}
chmod() {{ :; }}
groupadd() {{ MOCK_GROUP=true; }}
useradd() {{ MOCK_USER=true; }}
userdel() {{ USERDEL_CALLED=true; MOCK_USER=false; return "${{MOCK_USERDEL_STATUS:-0}}"; }}
groupdel() {{ GROUPDEL_CALLED=true; MOCK_GROUP=false; return "${{MOCK_GROUPDEL_STATUS:-0}}"; }}
{body}
"""
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_fresh_install_creates_and_records_managed_identities() -> None:
    result = _run_shell(
        "install.sh",
        """
if ! classify_account_state; then exit 10; fi
create_managed_snd_account
printf 'state=%s user=%s group=%s\\n' "$ACCOUNT_STATE" "$MOCK_USER" "$MOCK_GROUP"
cat "$INSTALL_STATE"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "state=fresh user=true group=true" in result.stdout
    assert "SND_UID=999\nSND_GID=998" in result.stdout


def test_fresh_install_cleans_up_group_when_user_creation_fails() -> None:
    result = _run_shell(
        "install.sh",
        """
useradd() { return 1; }
if create_managed_snd_account; then exit 10; fi
printf 'user=%s group=%s groupdel=%s\\n' "$MOCK_USER" "$MOCK_GROUP" "${GROUPDEL_CALLED:-false}"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "newly created identities were removed" in result.stdout
    assert "user=false group=false groupdel=true" in result.stdout


def test_fresh_install_cleans_up_identities_when_record_write_fails() -> None:
    result = _run_shell(
        "install.sh",
        """
record_install_state() { return 1; }
if create_managed_snd_account; then exit 10; fi
printf 'user=%s group=%s userdel=%s groupdel=%s\\n' "$MOCK_USER" "$MOCK_GROUP" "${USERDEL_CALLED:-false}" "${GROUPDEL_CALLED:-false}"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "newly created identities were removed" in result.stdout
    assert "user=false group=false userdel=true groupdel=true" in result.stdout


def test_record_write_stops_on_metadata_failure_without_publishing_record() -> None:
    result = _run_shell(
        "install.sh",
        """
chown() { return 1; }
CURRENT_SND_UID=999
CURRENT_SND_GROUP_GID=998
if record_install_state; then exit 10; fi
printf 'record_exists=%s\n' "$(test -e "$INSTALL_STATE" && echo yes || echo no)"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "record_exists=no" in result.stdout


def test_record_write_sets_root_ownership_and_private_modes() -> None:
    result = _run_shell(
        "install.sh",
        """
chown() { printf 'chown:%s\n' "$*" >> "$TEST_ROOT/metadata-calls"; }
chmod() { printf 'chmod:%s\n' "$*" >> "$TEST_ROOT/metadata-calls"; }
CURRENT_SND_UID=999
CURRENT_SND_GROUP_GID=998
record_install_state
cat "$TEST_ROOT/metadata-calls"
""",
    )

    assert result.returncode == 0, result.stderr
    calls = result.stdout.splitlines()
    assert len(calls) == 4
    assert calls[0].startswith("chown:root:root ") and calls[0].endswith("/etc")
    assert calls[1].startswith("chmod:700 ") and calls[1].endswith("/etc")
    temp_chown = calls[2].removeprefix("chown:root:root ")
    temp_chmod = calls[3].removeprefix("chmod:600 ")
    assert "/.install-state." in temp_chown
    assert temp_chmod == temp_chown


@pytest.mark.parametrize("caddy_installed", ["true", "false"])
def test_record_install_state_writes_version_two_record(caddy_installed: str) -> None:
    result = _run_shell(
        "install.sh",
        f"""
CURRENT_SND_UID=999
CURRENT_SND_GROUP_GID=998
DASHBOARD_INSTALLED_CADDY={caddy_installed}
record_install_state
cat "$INSTALL_STATE"
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "RECORD_VERSION=2\n"
        "SND_UID=999\n"
        "SND_GID=998\n"
        f"DASHBOARD_INSTALLED_CADDY={caddy_installed}\n"
    )


@pytest.mark.parametrize("script_name", ["install.sh", "uninstall.sh"])
@pytest.mark.parametrize("caddy_installed", ["true", "false"])
def test_read_install_state_accepts_version_two_record(
    script_name: str, caddy_installed: str
) -> None:
    result = _run_shell(
        script_name,
        f"""
mkdir -p "$INSTALL_STATE_DIR"
printf 'RECORD_VERSION=2\\nSND_UID=999\\nSND_GID=998\\nDASHBOARD_INSTALLED_CADDY={caddy_installed}\\n' > "$INSTALL_STATE"
read_install_state
printf 'uid=%s gid=%s caddy=%s\\n' "$RECORDED_SND_UID" "$RECORDED_SND_GID" "$RECORDED_DASHBOARD_INSTALLED_CADDY"
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"uid=999 gid=998 caddy={caddy_installed}\n"


@pytest.mark.parametrize("script_name", ["install.sh", "uninstall.sh"])
def test_read_install_state_accepts_legacy_record_with_unknown_caddy_origin(script_name: str) -> None:
    result = _run_shell(
        script_name,
        """
mkdir -p "$INSTALL_STATE_DIR"
printf 'SND_UID=999\\nSND_GID=998\\n' > "$INSTALL_STATE"
read_install_state
printf 'uid=%s gid=%s caddy=%s\\n' "$RECORDED_SND_UID" "$RECORDED_SND_GID" "$RECORDED_DASHBOARD_INSTALLED_CADDY"
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "uid=999 gid=998 caddy=unknown\n"


@pytest.mark.parametrize("script_name", ["install.sh", "uninstall.sh"])
@pytest.mark.parametrize(
    "record",
    [
        "RECORD_VERSION=2\\nSND_UID=999\\nSND_GID=998\\n",
        "RECORD_VERSION=2\\nSND_UID=999\\nSND_GROUP=998\\nDASHBOARD_INSTALLED_CADDY=true\\n",
        "RECORD_VERSION=2\\nSND_UID=999\\nSND_GID=998\\nDASHBOARD_INSTALLED_CADDY=yes\\n",
        "RECORD_VERSION=3\\nSND_UID=999\\nSND_GID=998\\nDASHBOARD_INSTALLED_CADDY=true\\n",
    ],
)
def test_read_install_state_rejects_malformed_versioned_record(
    script_name: str, record: str
) -> None:
    result = _run_shell(
        script_name,
        f"""
mkdir -p "$INSTALL_STATE_DIR"
printf '{record}' > "$INSTALL_STATE"
if read_install_state; then exit 10; fi
printf 'error=%s\\n' "$INSTALL_STATE_ERROR"
""",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "error=malformed\n"


@pytest.mark.parametrize(("user_exists", "group_exists"), [("true", "false"), ("false", "true")])
def test_fresh_install_rejects_existing_unmanaged_identity(
    user_exists: str, group_exists: str
) -> None:
    result = _run_shell(
        "install.sh",
        f"""
MOCK_USER={user_exists}
MOCK_GROUP={group_exists}
if classify_account_state; then exit 10; fi
""",
    )

    assert result.returncode == 0, result.stderr
    assert "fresh install refused" in result.stdout


def test_managed_update_validates_and_preserves_record() -> None:
    result = _run_shell(
        "install.sh",
        """
mkdir -p "$APP_DIR"
mkdir -p "$INSTALL_STATE_DIR"
printf 'SND_UID=999\\nSND_GID=998\\n' > "$INSTALL_STATE"
MOCK_USER=true
MOCK_GROUP=true
if ! classify_account_state; then exit 10; fi
printf 'state=%s\\n' "$ACCOUNT_STATE"
cat "$INSTALL_STATE"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "state=managed-update" in result.stdout
    assert result.stdout.count("SND_UID=999") == 1


def test_managed_record_allows_recovery_without_other_installation_files() -> None:
    result = _run_shell(
        "install.sh",
        """
mkdir -p "$INSTALL_STATE_DIR"
printf 'SND_UID=999\nSND_GID=998\n' > "$INSTALL_STATE"
MOCK_USER=true
MOCK_GROUP=true
if ! classify_account_state; then exit 10; fi
printf 'state=%s\n' "$ACCOUNT_STATE"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "state=managed-update" in result.stdout


def test_legacy_update_does_not_backfill_ownership_record() -> None:
    result = _run_shell(
        "install.sh",
        """
mkdir -p "$APP_DIR"
MOCK_USER=true
MOCK_GROUP=true
if ! classify_account_state; then exit 10; fi
printf 'state=%s record_exists=%s\\n' "$ACCOUNT_STATE" "$(test -e "$INSTALL_STATE" && echo yes || echo no)"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "ownership is unverified" in result.stdout
    assert "state=legacy-update record_exists=no" in result.stdout


def test_legacy_update_rejects_partial_identity() -> None:
    result = _run_shell(
        "install.sh",
        """
mkdir -p "$APP_DIR"
MOCK_USER=true
if classify_account_state; then exit 10; fi
""",
    )

    assert result.returncode == 0, result.stderr
    assert "only one or neither" in result.stdout


@pytest.mark.parametrize(
    ("record", "metadata", "expected"),
    [
        ("not-a-record\\n", "0:0:600", "malformed"),
        ("SND_UID=999\\nSND_GID=998\\n", "1000:1000:644", "unsafe"),
    ],
)
def test_managed_update_rejects_malformed_or_unsafe_record(
    record: str, metadata: str, expected: str
) -> None:
    result = _run_shell(
        "install.sh",
        f"""
mkdir -p "$APP_DIR"
mkdir -p "$INSTALL_STATE_DIR"
printf '{record}' > "$INSTALL_STATE"
MOCK_USER=true
MOCK_GROUP=true
MOCK_STAT='{metadata}'
if classify_account_state; then exit 10; fi
""",
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


def test_managed_update_rejects_changed_ids() -> None:
    result = _run_shell(
        "install.sh",
        """
mkdir -p "$APP_DIR"
mkdir -p "$INSTALL_STATE_DIR"
printf 'SND_UID=999\\nSND_GID=998\\n' > "$INSTALL_STATE"
MOCK_USER=true
MOCK_GROUP=true
MOCK_UID=1001
if classify_account_state; then exit 10; fi
""",
    )

    assert result.returncode == 0, result.stderr
    assert "do not match" in result.stdout


def test_safe_uninstall_removes_matching_identities_and_record() -> None:
    result = _run_shell(
        "uninstall.sh",
        """
mkdir -p "$INSTALL_STATE_DIR"
printf 'SND_UID=999\\nSND_GID=998\\n' > "$INSTALL_STATE"
MOCK_USER=true
MOCK_GROUP=true
classify_uninstall_account_state
if ! remove_snd_identities_if_verified; then exit 10; fi
printf 'remove=%s userdel=%s groupdel=%s record_exists=%s\\n' "$REMOVE_SND_IDENTITIES" "${USERDEL_CALLED:-false}" "${GROUPDEL_CALLED:-false}" "$(test -e "$INSTALL_STATE" && echo yes || echo no)"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "remove=true userdel=true groupdel=true record_exists=no" in result.stdout


def test_uninstall_rechecks_ids_immediately_before_deletion() -> None:
    result = _run_shell(
        "uninstall.sh",
        """
mkdir -p "$INSTALL_STATE_DIR"
printf 'SND_UID=999\\nSND_GID=998\\n' > "$INSTALL_STATE"
MOCK_USER=true
MOCK_GROUP=true
classify_uninstall_account_state
MOCK_UID=1001
if ! remove_snd_identities_if_verified; then exit 10; fi
printf 'remove=%s userdel=%s record_exists=%s\\n' "$REMOVE_SND_IDENTITIES" "${USERDEL_CALLED:-false}" "$(test -e "$INSTALL_STATE" && echo yes || echo no)"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "current snd IDs do not match" in result.stdout
    assert "remove=false userdel=false record_exists=yes" in result.stdout


@pytest.mark.parametrize(
    ("failure_variable", "expected"),
    [
        ("MOCK_USERDEL_STATUS=1", "could not remove the verified snd user"),
        ("MOCK_GROUPDEL_STATUS=1", "snd user was removed but the snd group remains"),
    ],
)
def test_uninstall_retains_record_when_identity_deletion_fails(
    failure_variable: str, expected: str
) -> None:
    result = _run_shell(
        "uninstall.sh",
        f"""
mkdir -p "$INSTALL_STATE_DIR"
printf 'SND_UID=999\\nSND_GID=998\\n' > "$INSTALL_STATE"
MOCK_USER=true
MOCK_GROUP=true
{failure_variable}
if remove_snd_identities_if_verified; then exit 10; fi
printf 'user=%s group=%s record_exists=%s\\n' "$MOCK_USER" "$MOCK_GROUP" "$(test -e "$INSTALL_STATE" && echo yes || echo no)"
""",
    )

    assert result.returncode == 0, result.stderr
    assert expected in result.stdout
    assert "record_exists=yes" in result.stdout


def test_unverified_uninstall_retains_identities() -> None:
    result = _run_shell(
        "uninstall.sh",
        """
MOCK_USER=true
MOCK_GROUP=true
classify_uninstall_account_state
if ! remove_snd_identities_if_verified; then exit 10; fi
printf 'remove=%s userdel=%s groupdel=%s\\n' "$REMOVE_SND_IDENTITIES" "${USERDEL_CALLED:-false}" "${GROUPDEL_CALLED:-false}"
""",
    )

    assert result.returncode == 0, result.stderr
    assert "ownership record is absent" in result.stdout
    assert "remove=false userdel=false groupdel=false" in result.stdout
