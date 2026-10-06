"""Tests for the deployment artefacts.

The shell scripts and the systemd unit are as much a part of the product as
the Python is, and both shipped with defects that no Python test could see: a
`touch` on a directory that is never created, a hardcoded log path that ignored
TGUSERBOT_LOG_DIR, two disagreeing definitions of "system mode", and a
TimeoutStopSec below the application's own worst-case shutdown.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

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


# --- the installer, run -----------------------------------------------------
#
# Everything above reads the script. That cannot tell you the *order* of its steps,
# and order is the whole of the next two tests.


def run_installer(tmp_path: Path, *, service_active: bool) -> list[str]:
    # The shim is a shell script, so the flag travels as "yes"/"no" rather than as
    # a Python bool -- which is how the first version of this harness answered
    # every question "no" without anyone noticing.
    active = "yes" if service_active else "no"
    """Run ``install.sh`` with every side effect recorded, and return the log.

    ``systemctl``, ``git``, ``pip``, ``runuser`` and ``useradd`` are replaced with
    shims on PATH that append their arguments to a log file. Nothing is installed
    and nothing is started; what comes back is the sequence the script asked for,
    which is the only thing that can settle whether the service was running when
    the code changed under it.
    """
    log = tmp_path / "calls.log"
    app_dir = tmp_path / "app"
    bin_dir = tmp_path / "bin"
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir()
    app_dir.mkdir()
    # An existing checkout, so the installer takes the `git pull` path. That is the
    # one that matters here: a fresh clone has no running bot under it.
    (app_dir / ".git").mkdir()

    for name in ("systemctl", "git", "pip", "useradd", "chown", "install", "ln", "rm"):
        shim = bin_dir / name
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s %s\\n" "{name}" "$*" >> "{log}"\n'
            'if [[ "$1" == "is-active" ]]; then\n'
            f'  [[ "{active}" == "yes" ]] && exit 0\n'
            "  exit 3\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)

    # `runuser -u user -p -- cmd` and the venv's pip are the real commands here.
    runuser = bin_dir / "runuser"
    runuser.write_text(
        '#!/usr/bin/env bash\nprintf \'runuser %s\\n\' "$*" >> "' + str(log) + '"\nexit 0\n',
        encoding="utf-8",
    )
    runuser.chmod(0o755)

    # The venv python has to exist for the installer's guard to pass.
    python = app_dir / ".venv" / "bin"
    python.mkdir(parents=True)
    fake_python = python / "python"
    fake_python.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)

    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TGUSERBOT_APP_DIR": str(app_dir),
        "TGUSERBOT_DATA_DIR": str(tmp_path / "data"),
        "TGUSERBOT_CONFIG_DIR": str(tmp_path / "etc"),
        "TGUSERBOT_SERVICE_USER": os.environ.get("USER", "root"),
        "TGUSERBOT_REPO_URL": "https://example.invalid/repo.git",
    }
    # Skip the root check: CI is not root and the sequence is what is under test.
    installer = INSTALL.read_text(encoding="utf-8").replace(
        'if [[ "${EUID}" -ne 0 ]]; then', "if false; then"
    )
    script = tmp_path / "install.sh"
    script.write_text(installer, encoding="utf-8")
    subprocess.run(
        ["bash", str(script)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if not log.is_file():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def test_the_installer_stops_the_service_before_changing_the_code(
    tmp_path: Path,
) -> None:
    """A pull under a running bot is what this fixes.

    ``install.sh`` fetched with ``git pull --ff-only`` while the service was up.
    Two things went wrong at once: the process kept running code that no longer
    matched its checkout, and the plugin watcher inside it noticed plugin files
    changing mid-pull and reloaded from a half-written tree. Neither is visible in
    the script's text -- it is the order of the calls that matters.
    """
    calls = run_installer(tmp_path, service_active=True)

    def index_of(prefix: str) -> int:
        for position, call in enumerate(calls):
            if call.startswith(prefix):
                return position
        return -1

    stop = index_of("systemctl stop")
    pull = index_of("git -C")
    assert stop >= 0, f"the installer never stops the service: {calls}"
    assert pull >= 0, f"the installer never fetched: {calls}"
    assert stop < pull, (
        f"the code was pulled at step {pull} but the service was only stopped at "
        f"{stop}; a running bot reloads plugins out of a half-updated tree: {calls}"
    )


def test_the_installer_puts_the_service_back(tmp_path: Path) -> None:
    """Stopping it is only half the fix; a stopped bot is the other failure.

    The installer must start the service again when it found it running, and must
    not start one that was not running before -- otherwise an installer run used
    to prepare a box would start the bot on it.
    """
    was_running = run_installer(tmp_path / "running", service_active=True)
    assert any(call.startswith("systemctl start") for call in was_running), (
        "the service was running before the install and was left stopped"
    )

    was_stopped = run_installer(tmp_path / "stopped", service_active=False)
    assert not any(call.startswith("systemctl start") for call in was_stopped), (
        "the installer started a service that was not running before"
    )
    assert not any(call.startswith("systemctl stop") for call in was_stopped), (
        "nothing was running, so nothing needed stopping"
    )


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


def test_the_alert_reuses_the_bot_session() -> None:
    """The alert opened its own session, which was never authorized.

    ``TelegramClient`` was pointed at ``<data_dir>/alert-session``. Nothing in this
    repository ever signs that session in -- ``auth`` writes ``<data_dir>/session``
    -- so ``is_user_authorized()`` was False on every run, and the alert exited 1
    having sent nothing. The failure mode is the worst one for an alert: it looks
    like it ran, and the service it exists to report is down.

    It reuses the bot's own session instead. That is safe here precisely because
    the service has already failed: nothing else holds it, and the whole point is
    to use the one credential that is known to work.
    """
    text = (REPO_ROOT / "deploy" / "alert.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "alert-session" not in code, (
        "the alert opens a session nothing ever authorizes; it must reuse the bot's"
    )
    assert 'f"{data_dir}/session"' in code, "the alert must use the authorized session path"


def test_the_alert_takes_the_session_lock() -> None:
    """Reusing the session means honouring the single-writer rule.

    The main unit may still be reaped when ``OnFailure=`` fires, and a second
    writer on one Telegram session corrupts the auth key -- taking the bot's
    session out of service *because* it went down. ``gateway.acquire_session_lock``
    is the existing implementation of that rule.
    """
    text = (REPO_ROOT / "deploy" / "alert.sh").read_text(encoding="utf-8")
    assert "flock" in text, "the alert opens the session without taking the lock"
    assert ".lock" in text, "the lock file name must match the gateway's"


def test_the_alert_reports_why_it_could_not_send() -> None:
    """A silent failure is the failure mode being fixed.

    Once alerting is configured, every remaining exit has to be non-zero and say
    what stopped it. The one honest ``0`` is "not configured", which is checked
    before anything can fail.
    """
    text = (REPO_ROOT / "deploy" / "alert.sh").read_text(encoding="utf-8")
    assert "is_user_authorized" in text, "an unauthorized session must be checked for"
    assert "could not send" in text, "a failed send must say so"
    assert "is not authorized" in text, "an unauthorized session must say so"

    body = text.split("def main", 1)[1].split("sys.exit(", 1)[0]
    lines = [line.strip() for line in body.splitlines()]
    # The only ``return 0`` allowed is the last statement of the function, reached
    # after a send that succeeded. Anywhere earlier, it reports success having sent
    # nothing -- which is the failure mode this script had.
    for index, line in enumerate(lines):
        if line.startswith("return 0"):
            remainder = [item for item in lines[index + 1 :] if item and not item.startswith("#")]
            assert not remainder, (
                "main() returns 0 and then does more work, so a later failure is "
                f"reported as success: {remainder[:3]}"
            )


def alert_python_body() -> str:
    """The Python embedded in ``alert.sh``, as the shell would hand it to python.

    Reading the file cannot tell you this program works: the first version of the
    lock helper was a generator used as a context manager and passed ``bash -n``,
    ``py_compile`` and every assertion on the file's text. It is extracted the
    same way ``exec ... <<'PYTHON'`` does, so what is checked here is what runs.
    """
    text = (REPO_ROOT / "deploy" / "alert.sh").read_text(encoding="utf-8")
    match = re.search(
        r"^exec \"\$\{PYTHON\}\" - .*<<'PYTHON'\n(.*?)^PYTHON$",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "the embedded python block was not found"
    return match.group(1)


def test_the_alerts_embedded_python_compiles() -> None:
    """The cheapest check that catches a broken script: does it parse at all."""
    compile(alert_python_body(), "alert.sh:PYTHON", "exec")


def run_alert_body(
    tmp_path: Path, env: dict[str, str], session: bool
) -> subprocess.CompletedProcess[str]:
    """Run the embedded program against a temporary environment, for real.

    Order of operations is the only way to check a shell script: the paths that
    matter are which failure fires first, and whether anything is written before
    it. Every assertion about this script so far has been about its text, and text
    is what hid the defect above.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env_file = tmp_path / "userbot.env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
    if session:
        (data_dir / "session.session").touch()
    body = alert_python_body().replace(
        'path: str = "/etc/tguserbot/userbot.env"', f'path: str = "{env_file}"'
    )
    script = tmp_path / "body.py"
    script.write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), "tguserbot.service", os.devnull],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_alert_refuses_without_a_session_and_says_why(tmp_path: Path) -> None:
    """No session means no alert, and the reason has to be in the output.

    This is the path the whole change is about, and it is only observable by
    running: a text assertion cannot tell that the check happens before the
    client is opened.
    """
    result = run_alert_body(
        tmp_path,
        {
            "TGUSERBOT_ALERT_CHAT": "12345",
            "TGUSERBOT_API_ID": "12345",
            "TGUSERBOT_API_HASH": "hash",
            "TGUSERBOT_DATA_DIR": str(tmp_path / "data"),
        },
        session=False,
    )
    assert result.returncode != 0, "no session is not a success"
    assert "session" in result.stderr, result.stderr


def test_the_alert_never_opens_a_session_of_its_own(tmp_path: Path) -> None:
    """Checked by what lands on disk, which is what reading the script cannot do.

    The client opens ``<data_dir>/session`` and Telethon writes that file, so a
    run that got as far as connecting leaves exactly one session behind. The old
    script left ``alert-session.session`` and nothing else.
    """
    result = run_alert_body(
        tmp_path,
        {
            "TGUSERBOT_ALERT_CHAT": "12345",
            "TGUSERBOT_API_ID": "0",
            "TGUSERBOT_API_HASH": "hash",
            "TGUSERBOT_DATA_DIR": str(tmp_path / "data"),
        },
        session=True,
    )
    produced = sorted(path.name for path in (tmp_path / "data").iterdir())
    assert "alert-session.session" not in produced, produced
    assert result.returncode != 0, "an unreachable API is not a success"


def test_a_bad_configuration_is_reported_before_the_session_is_touched(
    tmp_path: Path,
) -> None:
    """A non-numeric API id is caught by reading the file, not by dialling out.

    Ordering: the operator gets "TGUSERBOT_API_ID is not an id" and nothing is
    written, instead of a connection attempt that takes the timeout to fail.
    """
    result = run_alert_body(
        tmp_path,
        {
            "TGUSERBOT_ALERT_CHAT": "12345",
            "TGUSERBOT_API_ID": "not-an-id",
            "TGUSERBOT_API_HASH": "hash",
            "TGUSERBOT_DATA_DIR": str(tmp_path / "data"),
        },
        session=True,
    )
    assert "API_ID" in result.stderr, result.stderr
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == ["session.session"]


def _write_body(tmp_path: Path, *, without_entrypoint: bool = False) -> Path:
    """The embedded program, with its environment path pointed at a temp file.

    ``without_entrypoint`` drops the final ``sys.exit(asyncio.run(main()))`` so the
    module can be *imported* and its helpers driven directly. Only the entry point
    is removed; the code under test is the file's own.
    """
    env_file = tmp_path / "userbot.env"
    env_file.write_text("", encoding="utf-8")
    body = alert_python_body().replace(
        'path: str = "/etc/tguserbot/userbot.env"', f'path: str = "{env_file}"'
    )
    if without_entrypoint:
        # The entry point reads sys.argv, which belongs to pytest here, and exits
        # through it. Only that is neutralised; the helpers below are the file's
        # own, untouched.
        body = body.replace(
            "SERVICE, JOURNAL = sys.argv[1], sys.argv[2]",
            'SERVICE, JOURNAL = "tguserbot.service", ""',
        )
        body = body.replace("sys.exit(asyncio.run(main()))", "")
    script = tmp_path / "body.py"
    script.write_text(body, encoding="utf-8")
    return script


def _import_body(tmp_path: Path) -> Any:
    """Load the embedded program so its helpers can be called."""
    import importlib.machinery
    import importlib.util

    path = _write_body(tmp_path, without_entrypoint=True)
    spec = importlib.util.spec_from_loader(
        "alert_body", importlib.machinery.SourceFileLoader("alert_body", str(path))
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_session_lock_helper_is_a_context_manager(tmp_path: Path) -> None:
    """The helper is *used* as one, and the first version was not.

    Every failure path in this script exits before the lock is reached, so no
    end-to-end run gets far enough to notice -- which is how a generator with no
    ``@contextlib.contextmanager`` survived a passing suite, ``bash -n``,
    ``py_compile`` and every assertion on the file's text. This drives the helper
    the way ``main()`` does.
    """

    module = _import_body(tmp_path)

    session = tmp_path / "session"
    with module.hold_session_lock(str(session)):
        assert Path(f"{session}.lock").is_file(), "the lock file must exist while held"
    # Re-entering must not deadlock: the lock is released on the way out, so the
    # second acquisition succeeds rather than waiting out the timeout.
    with module.hold_session_lock(str(session)):
        pass


def test_a_contended_lock_does_not_stop_the_alert(tmp_path: Path) -> None:
    """An alert that refuses to run because of a lock is worse than a late one.

    ``OnFailure=`` can fire while the failed process is still being reaped. The
    lock is waited for, then the alert proceeds with a warning: the whole reason
    this script exists is to say the service is down, and it must not be silenced
    by a lock the dead process has not dropped yet.
    """
    import fcntl

    module = _import_body(tmp_path)

    session = tmp_path / "session"
    session.with_suffix(".session").touch()
    holder = open(f"{session}.lock", "a+b")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        # A fresh open in this process cannot take it either -- flock is per open
        # file description, so this is genuine contention, not a re-entry.
        previous = module.LOCK_TIMEOUT
        module.LOCK_TIMEOUT = 0.1
        try:
            with module.hold_session_lock(str(session)):
                pass
        finally:
            module.LOCK_TIMEOUT = previous
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_the_alert_unit_can_read_the_journal() -> None:
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


def _rollback_body() -> str:
    """The body of ``rollback_and_start``, which is where the recovery lives."""
    text = CTL.read_text(encoding="utf-8")
    start = text.index("rollback_and_start() {")
    return text[start : text.index("\n}", start)]


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
    # Only the system branch matters: there is no service to leave stopped in the
    # developer path, so an unguarded pull there is fine.
    system_branch = body[: body.index("\n  else")]
    # A bare git call would trip set -e and exit with the bot stopped.
    assert not re.search(r"^\s*git -C .*pull", system_branch, re.M), (
        "the pull is still unguarded; a failure would exit with the bot stopped"
    )
    assert 'if ! git -C "${APP_DIR}" pull' in body, body
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


# --- the update path has to work on the installed layout -------------------


def test_git_runs_as_root_not_as_the_service_user() -> None:
    """The checkout is root-owned on purpose, and git has to write inside it.

    `install.sh` creates `/opt/tguserbot` root-owned and chowns only `.venv`, so
    that a compromised bot cannot rewrite the code the next update installs. A
    `git pull` run as the service user cannot write `.git/FETCH_HEAD`, and git
    refuses the repository outright first:

        $ su tguserbot -c 'git -C /opt/tguserbot pull --ff-only'
        fatal: detected dubious ownership in repository at '/opt/tguserbot'

    So `tguserbotctl update` could never update anything. The installer already
    pulls as root; the update path has to do the same.
    """
    body = _update_code_body()
    assert "run_as_service_user git" not in body, (
        "git has to run as root here, like install.sh does; as the service user "
        "it cannot write a root-owned .git"
    )
    installer = INSTALL.read_text(encoding="utf-8")
    assert re.search(r"^\s*git -C .*pull", installer, re.M), (
        "the installer is the reference: it pulls as root"
    )


def test_rollback_returns_to_a_branch_not_a_detached_head() -> None:
    """`git checkout <sha>` detaches HEAD, and a pull there fails forever.

    After any rollback the checkout is left detached, so every later
    `git pull --ff-only` errors with "You are not currently on a branch", and
    `install.sh` aborts at its fetch step before it configures anything. Recovery
    then needs a human to type `git checkout main`.
    """
    helper = _rollback_body()
    assert 'checkout --quiet "${previous}"' not in helper, (
        "checking out a commit detaches HEAD and permanently breaks the pull"
    )
    assert "reset --hard" in helper, "the rollback has to move the branch, not detach it"
    assert re.search(r"rev-parse --abbrev-ref HEAD", _update_code_body()), (
        "the branch has to be recorded before the pull to be restored to it"
    )


def test_a_failed_health_check_restarts_rather_than_being_a_no_op() -> None:
    """`systemctl start` on an already-active unit succeeds and does nothing.

    The health check had just failed, so the process running is the one that
    failed it. Starting an active unit is a no-op, `restart` is never reached,
    and the build that just failed keeps running while the tree is rolled back
    underneath it.
    """
    text = CTL.read_text(encoding="utf-8")
    start = text.index("rollback_and_start() {")
    helper = text[start : text.index("\n}", start)]
    restart = helper.index("systemctl_cmd restart")
    start_cmd = helper.index("systemctl_cmd start")
    assert restart < start_cmd, (
        "start succeeds on an active unit, so the restart after it never runs; "
        f"restart has to come first:\n{helper}"
    )


def test_the_application_directory_is_not_owned_by_the_service_user() -> None:
    """Writing the checkout is how a compromised bot survives the next update.

    `install.sh` states the opposite in a comment three lines below the line that
    does it: "The checkout stays root-owned; only the venv and the runtime data
    belong to the service user." A write bit on `/opt/tguserbot` lets the bot
    unlink and replace `userbotctl`, which `/usr/local/bin/tguserbotctl` is a
    symlink to, and which the operator then runs under sudo.
    """
    installer = INSTALL.read_text(encoding="utf-8")
    line = re.search(r"^\s*install -d .*\$\{APP_DIR\}.*$", installer, re.M)
    assert line, "the app directory is not created with install -d any more"
    assert "-o root" in line.group(0), f"APP_DIR must be root-owned: {line.group(0).strip()!r}"
    assert '"${APP_DIR}" "${DATA_DIR}"' not in line.group(0), (
        "APP_DIR and DATA_DIR must not share one install -d: they need different owners"
    )


def test_the_recovery_clears_a_failed_unit_state() -> None:
    """`systemctl start` refuses a unit that tripped the start rate limiter.

    Once `StartLimitBurst` is hit, systemd answers "Start request repeated too
    quickly" with result `start-limit-hit` and keeps answering that way, so every
    recovery attempt fails for a reason that has nothing to do with the code. The
    bot stays down until someone knows to type `systemctl reset-failed` by hand.

    Reached here in practice: several updates in a row, each stopping and
    starting the unit, tripped the limiter, and the rollback that was supposed to
    bring the service back could not start it at all. The installer already
    clears this; the update path has to as well.
    """
    helper = _rollback_body()
    assert "reset-failed" in helper, (
        "the recovery has to clear start-limit-hit, or it cannot start the "
        f"service it is trying to restore:\n{helper}"
    )
    installer = INSTALL.read_text(encoding="utf-8")
    assert "reset-failed" in installer, "the installer is the reference implementation"


def test_the_rollback_does_not_silently_discard_local_edits() -> None:
    """`git reset --hard` throws away uncommitted work with no warning.

    The checkout is meant to be managed by `tguserbotctl update`, but an operator
    who edits a file on the server — and a hotfix applied under pressure is
    exactly that — loses it on the first failed update, and the file reverts
    without a word. It also undoes the very script doing the rollback, which is
    how a hand-installed fix disappears: observed, not hypothetical.
    """
    helper = _rollback_body()
    assert "reset --hard" in helper, (
        "the rollback still has to move the branch, so this is about warning"
    )
    assert re.search(r"status --porcelain", helper), (
        "an uncommitted change has to be noticed before it is discarded"
    )
