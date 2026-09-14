"""Password-management contracts for Task 4 Phase 1."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import unicodedata

import pytest
from argon2 import Type
from argon2.exceptions import HashingError, VerifyMismatchError

import auth


ROOT = Path(__file__).resolve().parents[1]


def _bash() -> str | None:
    if os.name == "nt":
        candidate = Path(r"C:\\Program Files\\Git\\bin\\bash.exe")
        return str(candidate) if candidate.exists() else None
    return shutil.which("bash")


def _run_bash_contract(body: str) -> subprocess.CompletedProcess[str]:
    bash = _bash()
    if bash is None:
        pytest.skip("Bash is required to exercise installer contracts")
    return subprocess.run(
        [bash, "--noprofile", "--norc", "-c", body],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("length, accepted", [(14, False), (15, True), (128, True), (129, False)])
def test_password_length_boundaries(length: int, accepted: bool) -> None:
    password = "a" * length
    if accepted:
        assert auth.normalize_password(password) == password
    else:
        with pytest.raises(auth.PasswordPolicyError):
            auth.normalize_password(password)


def test_password_is_normalized_to_nfc_and_spaces_are_accepted() -> None:
    decomposed = "e\u0301" + " password with spaces"
    assert auth.normalize_password(decomposed) == unicodedata.normalize("NFC", decomposed)


def test_password_prompt_rejects_mismatch(monkeypatch) -> None:
    entries = iter(["a" * 15, "b" * 15])
    monkeypatch.setattr(auth.getpass, "getpass", lambda _prompt: next(entries))
    with pytest.raises(auth.PasswordPolicyError, match="did not match"):
        auth.prompt_for_password()


def test_created_state_uses_the_approved_argon2id_parameters() -> None:
    password = "correct horse battery staple"
    state = auth.create_auth_state(password)
    assert state["version"] == 1
    assert state["password_hash"].startswith("$argon2id$v=19$m=19456,t=2,p=1$")
    assert auth.PASSWORD_HASHER.type is Type.ID
    assert auth.PASSWORD_HASHER.memory_cost == 19_456
    assert auth.PASSWORD_HASHER.time_cost == 2
    assert auth.PASSWORD_HASHER.parallelism == 1
    assert auth.PASSWORD_HASHER.salt_len == 16
    assert auth.PASSWORD_HASHER.hash_len == 32
    assert auth.PASSWORD_HASHER.verify(state["password_hash"], password)
    with pytest.raises(VerifyMismatchError):
        auth.PASSWORD_HASHER.verify(state["password_hash"], password + "!")


def test_auth_state_does_not_store_the_plaintext_password(tmp_path: Path) -> None:
    password = "plain text never belongs on disk"
    destination = tmp_path / "auth.json"
    auth.publish_auth_state(destination, auth.create_auth_state(password))
    contents = destination.read_text(encoding="utf-8")
    assert password not in contents
    assert auth.load_auth_state(destination)["password_hash"] in contents
    if os.name != "nt":
        assert destination.stat().st_mode & 0o777 == 0o600


def test_reset_state_rotates_session_generation() -> None:
    original = auth.create_auth_state("a" * 15)
    replacement = auth.create_auth_state("b" * 15)
    assert original["session_generation"] != replacement["session_generation"]


def test_atomic_publish_failure_retains_prior_complete_state_and_cleans_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "auth.json"
    original = auth.create_auth_state("a" * 15)
    auth.publish_auth_state(destination, original)
    monkeypatch.setattr(auth.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(auth.AuthStatePublicationError) as error:
        auth.publish_auth_state(destination, auth.create_auth_state("b" * 15))
    assert not error.value.published
    assert auth.load_auth_state(destination) == original
    assert not list(tmp_path.glob(".auth.json.*.tmp"))


def test_directory_sync_failure_reports_published_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "auth.json"
    original = auth.create_auth_state("a" * 15)
    replacement = auth.create_auth_state("b" * 15)
    auth.publish_auth_state(destination, original)
    monkeypatch.setattr(auth, "_fsync_directory", lambda _directory: (_ for _ in ()).throw(OSError("sync")))
    with pytest.raises(auth.AuthStatePublicationError) as error:
        auth.publish_auth_state(destination, replacement)
    assert error.value.published
    assert auth.load_auth_state(destination) == replacement


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership is unavailable on Windows")
def test_atomic_publish_applies_requested_owner_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(auth.os, "fchown", lambda fd, uid, gid: calls.append((fd, uid, gid)))
    auth.publish_auth_state(tmp_path / "auth.json", auth.create_auth_state("a" * 15), owner=(123, 456))
    assert len(calls) == 1
    assert calls[0][1:] == (123, 456)


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"version": 1, "password_hash": "bad", "session_generation": "bad"},
        {"version": 2, "password_hash": "bad", "session_generation": "bad"},
    ],
)
def test_malformed_state_is_refused(state: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        auth.validate_auth_state(state)


def test_initialize_preserves_existing_valid_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "auth.json"
    original = auth.create_auth_state("a" * 15)
    auth.publish_auth_state(destination, original)
    monkeypatch.setattr(auth, "prompt_for_password", lambda: pytest.fail("prompted on update"))
    monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    auth._initialize(destination)
    assert auth.load_auth_state(destination) == original


def test_initialize_creates_state_when_absent_for_fresh_or_pre_auth_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth, "prompt_for_password", lambda: "a" * 15)
    monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    destination = tmp_path / "auth.json"
    auth._initialize(destination)
    assert auth.load_auth_state(destination)["version"] == 1


@pytest.mark.parametrize("failure", [KeyboardInterrupt(), EOFError(), HashingError("unavailable")])
def test_initialize_cli_reports_interrupted_or_hashing_failure_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: BaseException
) -> None:
    destination = tmp_path / "auth.json"
    monkeypatch.setattr(sys, "argv", ["auth.py", "initialize", "--auth-file", str(destination)])
    if isinstance(failure, HashingError):
        monkeypatch.setattr(auth, "create_auth_state", lambda _password: (_ for _ in ()).throw(failure))
        monkeypatch.setattr(auth, "prompt_for_password", lambda: "a" * 15)
        monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    else:
        monkeypatch.setattr(auth, "prompt_for_password", lambda: (_ for _ in ()).throw(failure))
    assert auth.main() == 1
    assert "Traceback" not in capsys.readouterr().err
    assert not destination.exists()


def test_reset_requires_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "auth.json"
    auth.publish_auth_state(destination, auth.create_auth_state("a" * 15))
    monkeypatch.setattr(auth.os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(PermissionError):
        auth._reset(destination, "simple-network-dashboard")


def test_reset_rotates_state_and_restarts_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "auth.json"
    before = auth.create_auth_state("a" * 15)
    auth.publish_auth_state(destination, before)
    calls: list[list[str]] = []
    monkeypatch.setattr(auth.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(auth, "prompt_for_password", lambda: "b" * 15)
    monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda command, check: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    auth._reset(destination, "simple-network-dashboard")
    assert auth.load_auth_state(destination)["session_generation"] != before["session_generation"]
    assert calls == [
        ["systemctl", "stop", "simple-network-dashboard"],
        ["systemctl", "start", "simple-network-dashboard"],
        ["systemctl", "is-active", "--quiet", "simple-network-dashboard"],
    ]


def test_reset_reports_restart_failure_after_publishing_new_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "auth.json"
    before = auth.create_auth_state("a" * 15)
    auth.publish_auth_state(destination, before)
    monkeypatch.setattr(auth.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(auth, "prompt_for_password", lambda: "b" * 15)
    monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    results = iter([0, 0, 1])
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda command, check: subprocess.CompletedProcess(command, next(results)),
    )
    with pytest.raises(RuntimeError, match="startup could not be confirmed"):
        auth._reset(destination, "simple-network-dashboard")
    assert auth.load_auth_state(destination)["session_generation"] != before["session_generation"]


def test_reset_does_not_mutate_state_when_service_stop_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "auth.json"
    before = auth.create_auth_state("a" * 15)
    auth.publish_auth_state(destination, before)
    monkeypatch.setattr(auth.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(auth, "prompt_for_password", lambda: "b" * 15)
    monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda command, check: calls.append(command) or subprocess.CompletedProcess(command, 1),
    )
    with pytest.raises(RuntimeError, match="was not changed"):
        auth._reset(destination, "simple-network-dashboard")
    assert auth.load_auth_state(destination) == before
    assert calls == [["systemctl", "stop", "simple-network-dashboard"]]


def test_reset_resolves_service_owner_before_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "auth.json"
    before = auth.create_auth_state("a" * 15)
    auth.publish_auth_state(destination, before)
    calls: list[list[str]] = []
    monkeypatch.setattr(auth.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(auth, "prompt_for_password", lambda: "b" * 15)
    monkeypatch.setattr(
        auth,
        "_service_owner_ids",
        lambda: (_ for _ in ()).throw(RuntimeError("service account unavailable")),
    )
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda command, check: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    with pytest.raises(RuntimeError, match="service account unavailable"):
        auth._reset(destination, "simple-network-dashboard")
    assert calls == []
    assert auth.load_auth_state(destination) == before


def test_reset_keeps_service_stopped_when_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "auth.json"
    before = auth.create_auth_state("a" * 15)
    auth.publish_auth_state(destination, before)
    monkeypatch.setattr(auth.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(auth, "prompt_for_password", lambda: "b" * 15)
    monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda command, check: calls.append(command) or subprocess.CompletedProcess(command, 0),
    )
    monkeypatch.setattr(
        auth,
        "publish_auth_state",
        lambda *_args: (_ for _ in ()).throw(auth.AuthStatePublicationError(False, OSError("denied"))),
    )
    with pytest.raises(RuntimeError, match="was kept stopped"):
        auth._reset(destination, "simple-network-dashboard")
    assert calls == [["systemctl", "stop", "simple-network-dashboard"]]
    assert auth.load_auth_state(destination) == before


def test_installer_and_uninstaller_contracts_include_only_the_approved_auth_files() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    uninstaller = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert 'AUTH_FILE="${DATA_DIR}/auth.json"' in installer
    assert 'SESSIONS_FILE="${DATA_DIR}/sessions.json"' in installer
    assert 'install -o root -g root -m 0755 "$APP_DIR/snd-reset-password" "$RESET_COMMAND"' in installer
    assert 'cp main.py metrics_poller.py ssh_manager.py auth.py session_manager.py snd-reset-password requirements.txt uninstall.sh "$APP_DIR/"' in installer
    assert 'RESET_COMMAND="/usr/local/sbin/snd-reset-password"' in uninstaller
    assert 'remove_owned_reset_command()' in uninstaller
    assert 'auth.json' not in uninstaller.split("backup_private_config()", 1)[1].split("finalize_private_backup_dir", 1)[0]
    assert 'sessions.json' not in uninstaller.split("backup_private_config()", 1)[1].split("finalize_private_backup_dir", 1)[0]


def test_reset_wrapper_has_no_password_arguments_and_requires_root() -> None:
    wrapper = (ROOT / "snd-reset-password").read_text(encoding="utf-8")
    assert '"${EUID}" -ne 0' in wrapper
    assert "sudo snd-reset-password" in wrapper
    assert "--password" not in wrapper
    assert '"$@"' not in wrapper


def test_installer_refuses_an_unowned_reset_command_without_replacing_it() -> None:
    result = _run_bash_contract(
        """
source ./install.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
RESET_COMMAND="$TEST_ROOT/snd-reset-password"
printf foreign > "$RESET_COMMAND"
if verify_reset_command_destination; then exit 10; fi
test "$(cat "$RESET_COMMAND")" = foreign
"""
    )
    assert result.returncode == 0, result.stderr


def test_installer_updates_a_verified_owned_reset_command() -> None:
    result = _run_bash_contract(
        """
source ./install.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
APP_DIR="$TEST_ROOT/app"
RESET_COMMAND="$TEST_ROOT/snd-reset-password"
mkdir -p "$APP_DIR"
cp snd-reset-password "$APP_DIR/snd-reset-password"
cp snd-reset-password "$RESET_COMMAND"
stat() { printf '0:0:755\\n'; }
install() { cp "$7" "$8"; }
install_reset_command
cmp -- "$APP_DIR/snd-reset-password" "$RESET_COMMAND"
"""
    )
    assert result.returncode == 0, result.stderr


def test_uninstaller_preserves_unowned_reset_command() -> None:
    result = _run_bash_contract(
        """
source ./uninstall.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
RESET_COMMAND="$TEST_ROOT/snd-reset-password"
printf foreign > "$RESET_COMMAND"
remove_owned_reset_command
test "$(cat "$RESET_COMMAND")" = foreign
"""
    )
    assert result.returncode == 0, result.stderr


def test_installer_rejects_marker_and_metadata_spoof_without_matching_deployed_wrapper() -> None:
    result = _run_bash_contract(
        """
source ./install.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
APP_DIR="$TEST_ROOT/app"
RESET_COMMAND="$TEST_ROOT/snd-reset-password"
mkdir -p "$APP_DIR"
cp snd-reset-password "$APP_DIR/snd-reset-password"
cp snd-reset-password "$RESET_COMMAND"
printf spoof >> "$RESET_COMMAND"
stat() { printf '0:0:755\\n'; }
if verify_reset_command_destination; then exit 10; fi
"""
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX links are unavailable on Windows")
def test_load_auth_state_rejects_symlink_and_hard_link_paths(tmp_path: Path) -> None:
    destination = tmp_path / "auth.json"
    auth.publish_auth_state(destination, auth.create_auth_state("a" * 15))
    symlink = tmp_path / "auth-symlink.json"
    symlink.symlink_to(destination)
    hard_link = tmp_path / "auth-hard-link.json"
    os.link(destination, hard_link)
    with pytest.raises(ValueError, match="safe regular file"):
        auth.load_auth_state(symlink)
    with pytest.raises(ValueError, match="safe regular file"):
        auth.load_auth_state(hard_link)


def test_installer_restoration_helper_only_starts_a_previously_active_service() -> None:
    result = _run_bash_contract(
        """
source ./install.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
systemctl() { printf '%s\\n' "$*" >> "$TEST_ROOT/calls"; return 0; }
DASHBOARD_WAS_ACTIVE=true
restore_previously_active_service
DASHBOARD_WAS_ACTIVE=false
restore_previously_active_service
test "$(cat "$TEST_ROOT/calls")" = "start simple-network-dashboard
is-active --quiet simple-network-dashboard"
"""
    )
    assert result.returncode == 0, result.stderr


def test_installer_contains_failure_restoration_and_hard_link_guards() -> None:
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "before.st_nlink != 1" in installer
    assert "opened.st_nlink != 1" in installer
    assert "restore_previously_active_service" in installer
    assert "if ! initialize_authentication; then\n    restore_previously_active_service" in installer
    assert "if ! install_reset_command; then\n    echo \"Error: dashboard password-reset command installation failed.\"\n    restore_previously_active_service" in installer


def test_reset_rejects_successful_start_when_service_is_not_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "auth.json"
    auth.publish_auth_state(destination, auth.create_auth_state("a" * 15))
    results = iter([0, 0, 1])
    monkeypatch.setattr(auth.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(auth, "prompt_for_password", lambda: "b" * 15)
    monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda command, check: subprocess.CompletedProcess(command, next(results)),
    )
    with pytest.raises(RuntimeError, match="startup could not be confirmed"):
        auth._reset(destination, "simple-network-dashboard")


def test_cli_interruption_after_replacement_does_not_claim_nothing_was_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "auth.json"
    monkeypatch.setattr(sys, "argv", ["auth.py", "initialize", "--auth-file", str(destination)])
    monkeypatch.setattr(auth, "prompt_for_password", lambda: "a" * 15)
    monkeypatch.setattr(auth, "_service_owner_ids", lambda: None)
    monkeypatch.setattr(auth, "_fsync_directory", lambda _directory: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert auth.main() == 1
    assert destination.exists()
    error = capsys.readouterr().err
    assert "inspect the authentication file and service status" in error
    assert "no authentication state was published" not in error


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer link behavior is unavailable on Windows")
@pytest.mark.parametrize("link_kind", ["symbolic", "hard"])
def test_installer_rejects_linked_auth_state_before_metadata_mutation(
    tmp_path: Path, link_kind: str
) -> None:
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "import grp, os, pwd, types\n"
        "pwd.getpwnam = lambda _name: types.SimpleNamespace(pw_uid=os.getuid())\n"
        "grp.getgrnam = lambda _name: types.SimpleNamespace(gr_gid=os.getgid())\n",
        encoding="utf-8",
    )
    python_path = shlex.quote(str(tmp_path))
    link_command = (
        'ln -s "$TEST_ROOT/target" "$AUTH_FILE"'
        if link_kind == "symbolic"
        else 'ln "$TEST_ROOT/target" "$AUTH_FILE"'
    )
    result = _run_bash_contract(
        f"""
source ./install.sh
TEST_ROOT=$(mktemp -d)
trap 'rm -rf -- "$TEST_ROOT"' EXIT
DATA_DIR="$TEST_ROOT/data"
LOG_DIR="$TEST_ROOT/log"
AUTH_FILE="$DATA_DIR/auth.json"
APP_DIR="$TEST_ROOT/app"
ACCOUNT_STATE=fresh
mkdir -p "$DATA_DIR" "$LOG_DIR" "$APP_DIR"
printf protected > "$TEST_ROOT/target"
chmod 0644 "$TEST_ROOT/target"
{link_command}
export PYTHONPATH={python_path}
if repair_runtime_storage; then exit 10; fi
test "$(cat "$TEST_ROOT/target")" = protected
test "$(stat -c %a "$TEST_ROOT/target")" = 644
"""
    )
    assert result.returncode == 0, result.stderr
