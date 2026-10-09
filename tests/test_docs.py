"""Tests that the documentation matches the code.

Both READMEs and the docs previously promised things the shipped artefacts did
not do: automatic plugin reload on a server where the plugin directory is
read-only, yt-dlp impersonation that was never enabled, and `pytest` available
on a server that installs without the dev extras. Those are cheap to reintroduce
and hard to notice, so the claims are asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
DEPLOY_README = REPO_ROOT / "deploy" / "README.md"
PLUGIN_DOC = REPO_ROOT / "docs" / "plugin-development.md"
TIKTOK_DOC = REPO_ROOT / "docs" / "tiktok-plugin.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
SECURITY = REPO_ROOT / "SECURITY.md"
UNIT = REPO_ROOT / "deploy" / "systemd" / "tguserbot.service"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --- environment variables -------------------------------------------------

DOCUMENTED_ENV_VARS = {
    "TGUSERBOT_API_ID",
    "TGUSERBOT_API_HASH",
    "TGUSERBOT_APP_VERSION",
    "TGUSERBOT_PHONE",
    "TGUSERBOT_OWNER_IDS",
    "TGUSERBOT_DISABLED_PLUGINS",
    "TGUSERBOT_DATA_DIR",
    "TGUSERBOT_PLUGIN_DIR",
    "TGUSERBOT_LOG_DIR",
    "TGUSERBOT_LOG_LEVEL",
    "TGUSERBOT_ROOT",
    "TGUSERBOT_DEVICE_MODEL",
    "TGUSERBOT_WATCH",
    "TGUSERBOT_WATCH_INTERVAL",
    "TGUSERBOT_COMMAND_TIMEOUT",
    "TGUSERBOT_LOG_JSON",
    "TGUSERBOT_FLOOD_THRESHOLD",
    "TGUSERBOT_MIN_INTERVAL",
    "TGUSERBOT_GIT_ALLOWED_REPOS",
    "TGUSERBOT_ALERT_CHAT",
    "TGUSERBOT_GEMINI_API_KEY",
}

#: Settings that no ``Settings`` field backs, because they are consumed somewhere
#: else. They must still be documented, but a Python-level scan cannot see them.
#:
#: ``alert.sh`` reads the environment file with ``sed``; the plugins read their
#: own keys by name, from whatever variable the manifest points at.
SHELL_SETTINGS = {"TGUSERBOT_ALERT_CHAT", "TGUSERBOT_GEMINI_API_KEY"}


def test_env_example_covers_every_documented_variable() -> None:
    text = read(REPO_ROOT / ".env.example")
    for name in DOCUMENTED_ENV_VARS:
        assert re.search(rf"^{name}=", text, re.M), f".env.example is missing {name}"


def test_every_env_var_the_code_reads_is_documented() -> None:
    """Regression: a new knob that only exists in code is a trap for operators."""
    sources = [path for path in (REPO_ROOT / "src" / "userbot").glob("*.py")]
    # A plugin may read its own settings by name, so it is scanned too. Without
    # this, TGUSERBOT_GEMINI_API_KEY would be in code and in the README but no
    # test would ever connect the two.
    sources += [path for path in (REPO_ROOT / "plugins").rglob("*.py")]
    code = "\n".join(read(path) for path in sources if path.name != "config.py")
    used = set(re.findall(r"TGUSERBOT_[A-Z_]+", code)) | set(
        re.findall(r'env\.get\("([A-Z_]+)"', read(REPO_ROOT / "src" / "userbot" / "config.py"))
    )
    used = {name for name in used if name.startswith("TGUSERBOT_")}
    assert used <= DOCUMENTED_ENV_VARS, f"undocumented: {sorted(used - DOCUMENTED_ENV_VARS)}"


def test_every_setting_the_shell_reads_is_documented() -> None:
    """alert.sh pulls values out of userbot.env with sed; nothing else sees them."""
    for script in (REPO_ROOT / "deploy" / "alert.sh", REPO_ROOT / "userbotctl"):
        used = set(re.findall(r"TGUSERBOT_[A-Z_]+", script.read_text(encoding="utf-8")))
        # These are wrapper-scoped knobs, not settings the bot itself reads.
        used -= {
            "TGUSERBOT_APP_DIR",
            "TGUSERBOT_REPO_URL",
            "TGUSERBOT_SERVICE_USER",
            "TGUSERBOT_ENV_FILE",
        }
        undocumented = used - set(DOCUMENTED_ENV_VARS)
        assert not undocumented, f"{script.name} reads undocumented settings: {undocumented}"


def test_readme_table_lists_only_real_settings() -> None:
    """Every setting named in the README table must be a real environment
    variable backed by a real field."""
    from userbot.config import Settings

    # Not every variable maps to a field of the same name.
    env_to_field = {
        "TGUSERBOT_OWNER_IDS": "owner_ids",
        "TGUSERBOT_DISABLED_PLUGINS": "disabled_plugins",
        "TGUSERBOT_GIT_ALLOWED_REPOS": "git_allowed_repositories",
        "TGUSERBOT_LOG_LEVEL": "log_level",
        "TGUSERBOT_FLOOD_THRESHOLD": "flood_sleep_threshold",
        "TGUSERBOT_MIN_INTERVAL": "min_request_interval",
        "TGUSERBOT_WATCH": "watch_enabled",
        "TGUSERBOT_WATCH_INTERVAL": "watch_interval",
        "TGUSERBOT_COMMAND_TIMEOUT": "command_timeout",
        "TGUSERBOT_LOG_JSON": "log_json",
    }
    fields = set(Settings.__dataclass_fields__)
    documented = set(re.findall(r"\| `(TGUSERBOT_[A-Z_]+)`", read(README)))
    unknown = documented - set(DOCUMENTED_ENV_VARS)
    assert not unknown, f"README documents settings that do not exist: {sorted(unknown)}"
    for name in documented - SHELL_SETTINGS:
        field = env_to_field.get(name, name.removeprefix("TGUSERBOT_").lower())
        assert field in fields, f"{name} has no backing Settings field ({field})"
    # The table is the quick reference, so it is expected to name several.
    assert len(documented) >= 5


# --- commands --------------------------------------------------------------

CORE_COMMANDS = ("help", "version", "plugins", "plugin")


@pytest.mark.parametrize("name", CORE_COMMANDS)
def test_readme_documents_every_core_command(name: str) -> None:
    assert f"/ub {name}" in read(README)


def test_readme_documents_every_plugin_subcommand() -> None:
    text = read(README)
    for action in ("list", "reload", "enable", "disable", "install", "update"):
        assert f"/ub plugin {action}" in text, action


def test_documented_subcommands_match_the_parser() -> None:
    from userbot.commands import PLUGIN_ACTIONS

    text = read(README)
    for action in PLUGIN_ACTIONS:
        assert f"/ub plugin {action}" in text, action
    for action in PLUGIN_ACTIONS:
        assert f'"{action}"' in read(REPO_ROOT / "src" / "userbot" / "commands.py")


# --- watcher honesty -------------------------------------------------------


def test_readme_mentions_the_read_only_plugin_dir_caveat() -> None:
    """Regression: both READMEs promised automatic reload on a server where
    ProtectSystem=strict makes the plugin directory read-only."""
    for document in (README, DEPLOY_README):
        text = read(document)
        assert "ProtectSystem" in text, document.name
        assert "read-only" in text or "read only" in text, document.name


def test_the_caveat_is_true_of_the_unit() -> None:
    assert "ProtectSystem=strict" in read(UNIT)
    assert "TGUSERBOT_PLUGIN_DIR=/opt/tguserbot/plugins" in read(
        REPO_ROOT / "deploy" / "userbot.env.example"
    )


def test_readme_documents_the_watcher_switches() -> None:
    text = read(README)
    assert "TGUSERBOT_WATCH_INTERVAL" in text
    assert "TGUSERBOT_WATCH" in text


# --- server deployment honesty --------------------------------------------


def test_deploy_readme_does_not_promise_pytest_on_the_server() -> None:
    """Regression: it recommended `sudo -u tguserbot ... pytest`, but the
    server venv installs requirements.lock, which excludes the dev extras."""
    assert "pytest" not in read(DEPLOY_README)
    assert "tguserbotctl health" in read(DEPLOY_README)


def test_deploy_readme_states_what_actually_loads_on_first_start() -> None:
    text = read(DEPLOY_README)
    assert "TGUSERBOT_DISABLED_PLUGINS" in text
    for name in ("tiktok", "sum", "status"):
        assert name in text, name
    # The removed plugins must not still be advertised as shipping.
    for name in ("notes", "echo"):
        assert name not in text, f"{name} was removed but the deploy README still lists it"


def test_deploy_readme_documented_shutdown_budget_is_real() -> None:
    from userbot.app import SHUTDOWN_TIMEOUT

    text = read(DEPLOY_README)
    assert f"{SHUTDOWN_TIMEOUT:.0f} second" in text
    assert f"TimeoutStopSec={150}" in read(UNIT) or "TimeoutStopSec=120" in read(UNIT)


def test_deploy_readme_mentions_the_session_lock_and_heartbeat() -> None:
    text = read(DEPLOY_README)
    assert "flock" in text
    assert "heartbeat" in text.lower()


# --- tiktok honesty --------------------------------------------------------


def test_tiktok_doc_matches_the_implementation() -> None:
    """Regression: the doc claimed impersonation that was never configured."""
    plugin = read(REPO_ROOT / "plugins" / "tiktok" / "plugin.py")
    assert '"impersonate"' in plugin
    # The env hack was a real os.environ mutation from a worker thread, not just
    # a mention in a comment.
    assert "os.environ" not in plugin
    assert "setdefault(" not in plugin
    assert '"plugin_dirs"' not in plugin
    doc = read(TIKTOK_DOC)
    assert "impersonation" in doc
    assert "never invokes that scanner" in doc


def test_tiktok_doc_documents_the_real_config_keys() -> None:
    manifest = read(REPO_ROOT / "plugins" / "tiktok" / "plugin.toml")
    doc = read(TIKTOK_DOC)
    for key in ("max_file_mib", "allowed_domains"):
        assert key in manifest, key
        assert key in doc, key


def test_tiktok_doc_documents_the_config_file_path() -> None:
    from userbot.config import Settings

    assert (
        "plugin-config.toml"
        in Settings(
            root_dir=Path("/x"),
            data_dir=Path("/x"),
            plugin_dir=Path("/x"),
            log_dir=Path("/x"),
            api_id=1,
            api_hash="h",
        ).plugin_config_path.name
    )
    assert "plugin-config.toml" in read(TIKTOK_DOC)


# --- plugin development doc ------------------------------------------------


def test_plugin_doc_documents_the_whole_lifecycle() -> None:
    from userbot.plugin_api import LIFECYCLE_HOOKS

    text = read(PLUGIN_DOC)
    for hook in LIFECYCLE_HOOKS:
        assert hook in text, hook


def test_plugin_doc_documents_every_context_member_it_lists() -> None:
    import inspect

    from userbot.plugin_api import PluginContext

    text = read(PLUGIN_DOC)
    source = inspect.getsource(PluginContext)
    for member in ("storage", "config", "client", "settings", "logger", "rate_limiter"):
        assert member in text, member
        # storage/config/settings are set in __init__; the rest are constructor
        # parameters, so check the class source rather than the class dict.
        assert f"self.{member}" in source or f"{member}:" in source, member
    # spawn is a method.
    assert "def spawn(" in source
    assert "ctx.spawn" in text


def test_plugin_doc_documents_the_manifest_fields() -> None:
    from userbot.loader import PluginManifest

    text = read(PLUGIN_DOC)
    for field in ("name", "version", "api", "entrypoint", "description", "schema_version"):
        assert f"`{field}`" in text, field
        assert field in PluginManifest.__dataclass_fields__, field


def test_plugin_doc_explains_the_rowid_trap() -> None:
    """Regression worth preserving: lastrowid is stale for DELETE."""
    assert "lastrowid" in read(PLUGIN_DOC)
    assert "execute_insert" in read(PLUGIN_DOC)


def test_plugin_doc_names_the_stable_import_path() -> None:
    assert "from userbot.plugin_api import" in read(PLUGIN_DOC)
    import userbot

    for name in userbot.__all__:
        assert hasattr(userbot, name), name


# --- release hygiene -------------------------------------------------------


def test_changelog_heading_matches_the_package_version() -> None:
    """A version bump with an unreleased changelog, or vice versa, misleads users."""
    import re as _re

    from userbot import __version__

    text = read(CHANGELOG)
    assert "### Fixed" in text
    assert "### Migration" in text, "the per-plugin data move must stay documented"
    first = _re.search(r"^## \[([^\]]+)\]", text, _re.M)
    assert first, "no release heading in the changelog"
    heading = first.group(1)
    if heading == "Unreleased":
        # Work in progress is legitimate. The newest *released* heading must
        # still be the version the package reports, or a release note is lost.
        released = _re.findall(r"^## \[([^\]]+)\]", text, _re.M)[1:]
        assert released, "an Unreleased section with nothing released under it"
        assert __version__ in released[0], (
            f"the newest released heading is {released[0]!r} but pyproject says {__version__}"
        )
    else:
        assert __version__ in heading, (
            f"changelog head is {heading!r}, pyproject says {__version__}"
        )


def test_changelog_documents_the_data_migration() -> None:
    """Per-plugin data moved files; operators need to know."""
    text = read(CHANGELOG)
    assert "Migration" in text
    assert "plugin-data" in text


def test_changelog_records_the_reproduced_defects() -> None:
    text = read(CHANGELOG)
    for topic in (
        "Zombie plugin contexts",
        "always reported success",
        "global rather than per key",
        "spammed `edit_message`",
    ):
        assert topic in text, topic


def test_security_doc_states_the_real_boundary() -> None:
    text = read(SECURITY)
    assert "not a sandbox" in text
    assert "ATTACH" in text
    assert "TGUSERBOT_GIT_ALLOWED_REPOS" in text


def test_license_is_present_and_referenced() -> None:
    license_file = REPO_ROOT / "LICENSE"
    assert license_file.is_file()
    assert "MIT" in license_file.read_text(encoding="utf-8")
    pyproject = read(REPO_ROOT / "pyproject.toml")
    assert 'license = "MIT"' in pyproject


def test_readme_is_not_stale_about_requirements() -> None:
    text = read(README)
    assert "3.12" in text
    assert "3.14" in text, "the CI matrix tests 3.14; the README should say so"


# --- what CI actually measures ---------------------------------------------
#
# ``pytest --cov=userbot`` overrides the ``source`` in pyproject, so the report was
# ``src/userbot`` only: no plugin file appeared in it at all. A quarter of the code
# that runs in production was invisible to the number the build publishes.


def test_ci_measures_what_pyproject_says_to_measure() -> None:
    """The flag that narrows the measurement must not be on the command.

    Only the commands are checked, not the file: a comment has to be able to name
    the flag it is warning about, which is most of why this defect survived.
    """
    commands = "\n".join(
        line for line in read(CI).splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    assert "--cov=userbot" not in commands, (
        "--cov=<package> replaces the configured source and silently drops plugins/ from the report"
    )
    # Bare ``--cov`` measures what the tests import, and the configured ``source``
    # is what names the tree; the two must not be swapped for one another.
    assert "--cov=" not in commands.replace("--cov-report", "").replace("--cov-fail-under", ""), (
        "an explicit --cov narrows the measurement away from pyproject's source"
    )


def test_ci_fails_when_the_floor_is_not_met() -> None:
    """A coverage number nobody fails on is a number that drifts down.

    The floor lives in pyproject, where a developer running pytest gets it. CI
    should name it too, so the build says what it insists on rather than relying on
    a setting two files away that a future edit could quietly drop.
    """
    text = read(CI)
    assert "--cov-fail-under" in text, "CI does not fail below the coverage floor"
    import tomllib

    floor = tomllib.loads(read(PYPROJECT))["tool"]["coverage"]["report"]["fail_under"]
    assert f"--cov-fail-under={floor}" in text, (
        f"CI and pyproject disagree about the floor (pyproject says {floor})"
    )


def test_the_configured_source_includes_the_plugins() -> None:
    """The other end of the flag: what CI now measures has to include them."""
    import tomllib

    source = tomllib.loads(read(PYPROJECT))["tool"]["coverage"]["run"]["source"]
    assert "plugins" in source, source
    assert "src/userbot" in source, source


def test_the_published_artifact_covers_the_plugins() -> None:
    """The uploaded XML is what a reader trusts; it must not be a subset."""
    text = read(CI)
    assert "if-no-files-found: error" in text
