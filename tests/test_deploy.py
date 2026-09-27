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
