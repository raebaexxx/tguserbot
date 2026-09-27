"""Tests for the deployment artefacts.

The shell scripts and the systemd unit are as much a part of the product as
the Python is, and both shipped with defects that no Python test could see: a
`touch` on a directory that is never created, a hardcoded log path that ignored
TGUSERBOT_LOG_DIR, two disagreeing definitions of "system mode", and a
TimeoutStopSec below the application's own worst-case shutdown.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CTL = REPO_ROOT / "userbotctl"
INSTALL = REPO_ROOT / "deploy" / "install.sh"
UNIT = REPO_ROOT / "deploy" / "systemd" / "tguserbot.service"


def run_bash(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", *args], cwd=cwd, capture_output=True, text=True, check=False)


# --- shell syntax ----------------------------------------------------------


@pytest.mark.parametrize("script", [CTL, INSTALL], ids=["userbotctl", "install.sh"])
def test_script_parses(script: Path) -> None:
    assert run_bash("-n", str(script)).returncode == 0


@pytest.mark.parametrize("script", [CTL, INSTALL], ids=["userbotctl", "install.sh"])
def test_script_uses_strict_mode(script: Path) -> None:
    assert re.search(r"^set -euo pipefail$", script.read_text(encoding="utf-8"), re.M)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed")
@pytest.mark.parametrize("script", [CTL, INSTALL], ids=["userbotctl", "install.sh"])
def test_script_is_shellcheck_clean(script: Path) -> None:
    result = subprocess.run(
        ["shellcheck", "-S", "style", str(script)], capture_output=True, text=True, check=False
    )
    # SC2317 is a false positive here: the installer calls log() from a function
    # that shellcheck cannot see being reached.
    findings = [
        line for line in result.stdout.splitlines() if line.strip() and "SC2317" not in line
    ]
    assert not findings, result.stdout


# --- userbotctl behaviour --------------------------------------------------


def test_ctl_is_executable() -> None:
    import os

    assert os.access(CTL, os.X_OK), "userbotctl must stay executable"


def test_ctl_help_lists_every_subcommand() -> None:
    result = run_bash(str(CTL), "help")
    assert result.returncode == 0
    for command in (
        "setup",
        "first-run",
        "auth",
        "start",
        "stop",
        "restart",
        "status",
        "logs",
        "update",
        "health",
    ):
        assert re.search(rf"^\s+{command}\s", result.stdout, re.M), command


def test_ctl_rejects_an_unknown_subcommand() -> None:
    result = run_bash(str(CTL), "frobnicate")
    assert result.returncode == 2
    assert "Unknown command" in result.stderr


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_ctl_help_flags(flag: str) -> None:
    result = run_bash(str(CTL), flag)
    assert result.returncode == 0
    assert "Usage:" in result.stdout


def run_ctl(command: str, app_dir: Path, **env: str) -> subprocess.CompletedProcess[str] | None:
    """Run `userbotctl <command>` in local mode.

    Returns ``None`` when the command blocked (e.g. `tail -f`) and the timeout
    fired, which is a successful outcome for the behaviour under test.
    """
    environment = {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(app_dir.parent),
        "TGUSERBOT_APP_DIR": str(app_dir),
        **env,
    }
    try:
        return subprocess.run(
            ["bash", str(CTL), command],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return None


def test_ctl_logs_creates_a_missing_log_directory(tmp_path: Path) -> None:
    """Regression: `touch` failed because nothing created var/logs first."""
    run_ctl("logs", tmp_path / "app")
    assert (tmp_path / "app" / "var" / "logs" / "userbot.log").is_file()


def test_ctl_honours_a_configured_log_directory(tmp_path: Path) -> None:
    """Regression: the log path was hardcoded to <app>/var/logs."""
    app = tmp_path / "app"
    app.mkdir()
    custom_logs = tmp_path / "elsewhere" / "logs"
    custom_logs.mkdir(parents=True)
    (custom_logs / "userbot.log").write_text("marker-from-custom-location\n", encoding="utf-8")
    env_file = tmp_path / "custom.env"
    env_file.write_text(f"TGUSERBOT_LOG_DIR={custom_logs}\n", encoding="utf-8")

    result = run_ctl("status", app, TGUSERBOT_ENV_FILE=str(env_file))
    assert result is not None
    assert "marker-from-custom-location" in result.stdout
    assert not (app / "var" / "logs" / "userbot.log").exists()


def test_ctl_reports_a_missing_python_environment(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(CTL), "start"],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "TGUSERBOT_APP_DIR": str(tmp_path / "app"),
        },
    )
    assert result.returncode != 0
    assert "Python environment not found" in result.stdout + result.stderr


def test_ctl_auth_reports_a_missing_env_file(tmp_path: Path) -> None:
    (tmp_path / "app" / ".venv" / "bin").mkdir(parents=True)
    python = tmp_path / "app" / ".venv" / "bin" / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    result = subprocess.run(
        ["bash", str(CTL), "auth"],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(tmp_path),
            "TGUSERBOT_APP_DIR": str(tmp_path / "app"),
        },
    )
    assert result.returncode != 0
    assert "Environment file not found" in result.stdout + result.stderr


def function_body(script: Path, name: str) -> str:
    """Return the text of a bash function body, brace-balanced."""
    text = script.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unterminated function {name} in {script.name}")


def test_ctl_system_mode_is_derived_from_one_answer() -> None:
    """Regression: APP_DIR and is_system_mode were computed separately."""
    body = function_body(CTL, "is_system_mode")
    assert "${APP_DIR}" in body
    assert "/opt/tguserbot" not in body, (
        "is_system_mode must not hardcode a path; it must use the resolved APP_DIR"
    )


def test_ctl_runs_auth_with_a_pseudo_terminal() -> None:
    """Regression: runuser without -p left no TTY for the interactive login."""
    text = CTL.read_text(encoding="utf-8")
    assert 'runuser -u "${SERVICE_USER}" -p --' in text


# --- install.sh ------------------------------------------------------------


def test_installer_requires_root() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert 'if [[ "${EUID}" -ne 0 ]]' in text


def test_installer_preflights_its_prerequisites() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "preflight()" in text
    for probe in ("command -v git", "command -v python3", "import venv", "version_info"):
        assert probe in text, f"missing preflight probe: {probe}"


def test_installer_respects_the_app_dir_override() -> None:
    """Regression: APP_DIR was `readonly`, silently ignoring the documented
    TGUSERBOT_APP_DIR override."""
    text = INSTALL.read_text(encoding="utf-8")
    assert 'readonly APP_DIR="${TGUSERBOT_APP_DIR:-/opt/tguserbot}"' in text


def test_installer_does_not_chown_the_whole_checkout() -> None:
    """Regression: chown -R of /opt/tguserbot let the service user rewrite its
    own code and break the next `git pull --ff-only`."""
    text = INSTALL.read_text(encoding="utf-8")
    assert 'chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"' not in text
    assert 'chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}/.venv"' in text


def test_installer_creates_the_log_directory() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert '"${DATA_DIR}/logs"' in text


def test_installer_resets_a_failed_unit_state() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    assert "systemctl reset-failed" in text


# --- systemd unit ----------------------------------------------------------


def unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


def unit_sections(path: Path = UNIT) -> dict[str, list[str]]:
    """Parse an ini-style unit file into section -> directive names."""
    sections: dict[str, list[str]] = {}
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            continue
        if current and "=" in line:
            sections[current].append(line.split("=", 1)[0].strip())
    return sections


#: systemd silently ignores these in the wrong section, so a misplaced directive
#: looks like it works. Confirmed against a live systemd, which logged
#: "Unknown key name 'OnFailure' in section 'Service', ignoring."
UNIT_ONLY_DIRECTIVES = {
    "OnFailure",
    "OnFailureJobMode",
    "StartLimitIntervalSec",
    "StartLimitBurst",
    "After",
    "Before",
    "Requires",
    "Wants",
    "PartOf",
    "RefuseManualStart",
}

SERVICE_ONLY_DIRECTIVES = {
    "ExecStart",
    "ExecStartPre",
    "Type",
    "User",
    "Group",
    "EnvironmentFile",
    "WorkingDirectory",
    "MemoryMax",
    "LimitNOFILE",
    "WatchdogSec",
    "TimeoutStopSec",
    "NotifyAccess",
    "UMask",
    "ReadWritePaths",
}


def test_directives_are_in_the_section_systemd_expects() -> None:
    """A directive in the wrong section is dropped without any error."""
    for path in (UNIT, REPO_ROOT / "deploy" / "systemd" / "tguserbot-alert@.service"):
        sections = unit_sections(path)
        assert "Unit" in sections and "Service" in sections, path.name
        for directive in sections["Service"]:
            assert directive not in UNIT_ONLY_DIRECTIVES, (
                f"{path.name}: {directive}= belongs in [Unit], not [Service]"
            )
        for directive in sections["Unit"]:
            assert directive not in SERVICE_ONLY_DIRECTIVES, (
                f"{path.name}: {directive}= belongs in [Service], not [Unit]"
            )


def test_alerting_is_wired_to_a_unit_that_exists() -> None:
    assert "OnFailure=tguserbot-alert@%n.service" in unit_text()
    alert = (REPO_ROOT / "deploy" / "systemd" / "tguserbot-alert@.service").read_text(
        encoding="utf-8"
    )
    assert "Type=oneshot" in alert
    assert "Restart=no" in alert, "a failed alert must not retry forever"
    assert "deploy/alert.sh" in alert
    assert (REPO_ROOT / "deploy" / "alert.sh").is_file()
    assert "tguserbot-alert@.service" in (REPO_ROOT / "deploy" / "install.sh").read_text(
        encoding="utf-8"
    ), "the OnFailure= target must be installed or the hook never fires"


def test_alerting_is_opt_in_and_documented() -> None:
    for example in (REPO_ROOT / ".env.example", REPO_ROOT / "deploy" / "userbot.env.example"):
        text = example.read_text(encoding="utf-8")
        assert re.search(r"^TGUSERBOT_ALERT_CHAT=\s*$", text, re.M), (
            f"{example.name} must ship TGUSERBOT_ALERT_CHAT empty so alerting is opt-in"
        )
    assert "TGUSERBOT_ALERT_CHAT" in (REPO_ROOT / "deploy" / "README.md").read_text(
        encoding="utf-8"
    )


def test_alert_script_is_sound() -> None:
    alert = REPO_ROOT / "deploy" / "alert.sh"
    assert alert.is_file()
    assert run_bash("-n", str(alert)).returncode == 0
    text = alert.read_text(encoding="utf-8")
    assert "set -uo pipefail" in text
    assert "except Exception" in text, "alerting must never raise"
    # An unreadable journal must not suppress the alert: the operator needs to
    # know the service is down even if the excerpt is missing.
    assert "sending the alert without it" in text


def test_alert_unit_can_read_the_journal() -> None:
    """The alert unit runs as the service user, which is not in the journal group."""
    alert = (REPO_ROOT / "deploy" / "systemd" / "tguserbot-alert@.service").read_text(
        encoding="utf-8"
    )
    assert "SupplementaryGroups=systemd-journal" in alert
    assert "User=tguserbot" in alert


def test_unit_runs_as_the_service_user() -> None:
    text = unit_text()
    assert "User=tguserbot" in text
    assert "Group=tguserbot" in text


def test_unit_timeout_exceeds_the_application_shutdown_deadline() -> None:
    """Regression: TimeoutStopSec=30 was below the app's worst case.

    Four plugins at 10s of task cancellation plus 10s of stop() each is 80s, so
    systemd SIGKILLed the process mid-unload.
    """
    from userbot.app import SHUTDOWN_TIMEOUT

    match = re.search(r"^TimeoutStopSec=(\d+)$", unit_text(), re.M)
    assert match, "TimeoutStopSec is not set"
    assert int(match.group(1)) > SHUTDOWN_TIMEOUT * 2


def test_unit_keeps_the_plugin_directory_read_only() -> None:
    """ProtectSystem=strict makes the shipped watcher inert; document it."""
    text = unit_text()
    assert "ProtectSystem=strict" in text
    assert "ReadWritePaths=/var/lib/tguserbot" in text
    assert "watcher" in text.lower(), "the read-only plugin dir must be called out"


def test_unit_rate_limits_restarts() -> None:
    text = unit_text()
    assert "StartLimitIntervalSec" in text
    assert "StartLimitBurst" in text


def test_unit_watches_liveness() -> None:
    """A wedged process must be restarted, and the heartbeat is the only signal.

    The heartbeat file existed with nothing reading it; the unit now uses
    Type=notify with WatchdogSec, so ``start()`` has to send READY=1 and the
    heartbeat loop has to ping or systemd will kill a healthy-looking bot.
    """
    text = unit_text()
    assert "Type=notify" in text
    assert "NotifyAccess=main" in text
    assert "WatchdogSec=" in text
    from userbot.app import HEARTBEAT_INTERVAL

    match = re.search(r"^WatchdogSec=(\d+)$", text, re.M)
    assert match, "WatchdogSec is not set"
    seconds = int(match.group(1))
    assert HEARTBEAT_INTERVAL * 2 <= seconds, (
        f"the app pings every {HEARTBEAT_INTERVAL}s but WatchdogSec is {seconds}"
    )
    from userbot.notify import SystemdNotifier

    for method in ("ready", "ping", "stopping", "reset"):
        assert hasattr(SystemdNotifier, method), method


def test_unit_bounds_resources() -> None:
    text = unit_text()
    assert "MemoryMax=" in text
    assert "LimitNOFILE=" in text


def test_unit_applies_the_expected_hardening() -> None:
    text = unit_text()
    for directive in (
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectHome=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectControlGroups=true",
        "RestrictSUIDSGID=true",
        "RestrictNamespaces=true",
        "LockPersonality=true",
        "RestrictRealtime=true",
        "UMask=0077",
    ):
        assert directive in text, directive


def test_unit_uses_the_configured_environment_file() -> None:
    assert "EnvironmentFile=/etc/tguserbot/userbot.env" in unit_text()


# --- packaged env examples -------------------------------------------------


@pytest.mark.parametrize(
    "example", [REPO_ROOT / ".env.example", REPO_ROOT / "deploy" / "userbot.env.example"]
)
def test_env_examples_declare_the_documented_keys(example: Path) -> None:
    text = example.read_text(encoding="utf-8")
    for key in (
        "TGUSERBOT_API_ID",
        "TGUSERBOT_API_HASH",
        "TGUSERBOT_OWNER_IDS",
        "TGUSERBOT_DATA_DIR",
        "TGUSERBOT_PLUGIN_DIR",
    ):
        assert key in text, f"{example.name} is missing {key}"


def test_env_examples_document_the_new_knobs() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for key in (
        "TGUSERBOT_WATCH",
        "TGUSERBOT_WATCH_INTERVAL",
        "TGUSERBOT_COMMAND_TIMEOUT",
        "TGUSERBOT_LOG_JSON",
        "TGUSERBOT_FLOOD_THRESHOLD",
        "TGUSERBOT_MIN_INTERVAL",
    ):
        assert key in text, f".env.example is missing {key}"


# --- dependency locking ----------------------------------------------------

LOCK = REPO_ROOT / "requirements.lock"
UV_LOCK = REPO_ROOT / "uv.lock"


def test_lock_carries_hashes() -> None:
    """The deploy path installs from this file as root, so the bytes matter."""
    text = LOCK.read_text(encoding="utf-8")
    assert "--hash=sha256:" in text, "requirements.lock must pin hashes"
    requirements = [line for line in text.splitlines() if line and not line.startswith((" ", "#"))]
    assert requirements, "the lock is empty"
    assert all(re.match(r"^[A-Za-z0-9_.-]+==", line) for line in requirements), requirements


def test_lock_omits_the_project() -> None:
    """A local directory cannot be hashed, so --require-hashes would reject it."""
    text = LOCK.read_text(encoding="utf-8")
    assert not re.search(r"^-e ", text, re.M), "the lock must not contain an editable install"
    assert not re.search(r"^\. ", text, re.M)


def test_every_pinned_dependency_has_a_hash() -> None:
    text = LOCK.read_text(encoding="utf-8")
    for match in re.finditer(r"^([A-Za-z0-9_.-]+)==(\S+)", text, re.M):
        name = match.group(1)
        tail = text[match.end() :]
        next_requirement = re.search(r"^[A-Za-z0-9_.-]+==", tail, re.M)
        block = tail[: next_requirement.start()] if next_requirement else tail
        assert "--hash=sha256:" in block, f"{name} has no hash"


def test_install_scripts_install_hashed_deps_and_the_project_separately() -> None:
    """--require-hashes rejects an un-hashed -e . line, so the two steps are
    separate everywhere the lock is consumed."""
    for script in (CTL, INSTALL):
        text = script.read_text(encoding="utf-8")
        assert "--require-hashes" in text, script.name
        assert "--no-deps" in text, f"{script.name} must install the project with --no-deps"
        # The plain editable install of the whole project must not stand alone
        # next to a hashed requirements file.
        assert not re.search(r"pip install -r requirements\.lock(?!\s|$)", text), script.name


def test_uv_lock_is_in_sync_with_pyproject() -> None:
    """Regression: four dev dependencies were added to pyproject.toml while
    uv.lock kept the old resolution, so `uv sync` produced a different
    environment from the one CI installs."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    uv_lock = UV_LOCK.read_text(encoding="utf-8")
    dev_block = re.search(r"dev = \[(.*?)\]", pyproject, re.S)
    assert dev_block, "no dev extras in pyproject.toml"
    for line in dev_block.group(1).splitlines():
        name = re.match(r'\s*"([A-Za-z0-9_.-]+)', line)
        if not name:
            continue
        assert f'name = "{name.group(1)}"' in uv_lock, (
            f"{name.group(1)} is in pyproject dev extras but not in uv.lock; run `uv lock`"
        )


# --- the update must not pull out from under a running process -------------


def _update_code_body() -> str:
    """The body of ``update_code``, for ordering assertions."""
    text = CTL.read_text(encoding="utf-8")
    start = text.index("update_code() {")
    end = text.index("\n}", start)
    return text[start:end]


def test_update_stops_the_service_before_pulling() -> None:
    """A live process must not see a half-updated tree.

    The watcher is on by default -- ``TGUSERBOT_WATCH`` defaults to 1 -- so
    while the service is running, a ``git pull`` looks like a plugin edit and
    triggers a hot reload. The process still holds the old ``userbot.gemini`` in
    memory while the new plugin source on disk imports from it, and the reload
    fails:

        Cannot load plugin sum: cannot import name 'ModelRouter' from
        'userbot.gemini'

    which is not a real defect in either file. It is the pull happening while
    the process is live. Stopping first costs a few seconds of downtime and
    removes the whole class.
    """
    body = _update_code_body()
    stop = body.find("systemctl_cmd stop")
    pull = body.find('git -C "${APP_DIR}" pull --ff-only')
    assert stop != -1, "update never stops the service before changing the tree"
    assert pull != -1, "update no longer pulls"
    assert stop < pull, (
        "the pull must come after the stop, or the running process sees a partially updated tree"
    )


def test_a_failed_update_still_leaves_the_service_running() -> None:
    """Stopping first must not turn a bad update into a stopped bot.

    The recovery lives in one helper rather than being repeated per path: a
    copy per path is a copy that gets missed the next time a step is added.
    """
    body = _update_code_body()
    assert body.count("systemctl_cmd start") >= 1, (
        "the service has to be started explicitly after the stop"
    )
    # Every `exit 1` from a failure path goes through the recovery.
    for exit_at in re.finditer(r"exit 1", body):
        before = body[: exit_at.start()].rstrip().splitlines()[-3:]
        assert any("rollback_and_start" in line for line in before), (
            f"an exit 1 does not restore the service: {before!r}"
        )


def test_a_failing_pull_does_not_leave_the_bot_stopped() -> None:
    """Stopping first moves the burden onto the failure paths.

    Under `set -e` a failed `git pull` used to exit with the service still
    running, which was the safe outcome. After the stop was added it would exit
    with the bot down and never started again -- an update that fails to apply
    is not a reason to take the bot offline. Each step therefore checks its own
    result and comes back.
    """
    body = _update_code_body()
    # No bare `run_as_service_user git ... pull` left to trip set -e.
    assert not re.search(r"^\s*run_as_service_user git .*pull", body, re.M), (
        "the pull is still unguarded; a failure would exit with the bot stopped"
    )
    assert "if ! run_as_service_user git" in body, body
    assert body.count("rollback_and_start") >= 4, (
        "every failure path has to restore the service, not just the health check"
    )


def test_rollback_brings_the_service_back_up() -> None:
    """The helper is the recovery, so it has to start something."""
    text = CTL.read_text(encoding="utf-8")
    start = text.index("rollback_and_start() {")
    helper = text[start : text.index("\n}", start)]
    assert "systemctl_cmd start" in helper or "systemctl_cmd restart" in helper, helper
    assert "git" in helper, "the half-updated tree has to be put back"
