"""The API key must never be in the repository.

A Telegram userbot already holds a session that can read and send everything the
account can. A committed Gemini key would add a billable third-party credential to
the same blast radius, and a public repository is a poor place to find out.

These tests read every tracked file. They are cheap, they run in CI, and they are
the only thing standing between a careless ``export`` and a leaked key.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Text files worth scanning. Binary content is skipped: a false positive there
#: would be noise nobody could act on.
TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".md",
    ".txt",
    ".cfg",
    ".ini",
    ".yml",
    ".yaml",
    ".sh",
    ".env",
    ".example",
    ".service",
    ".json",
}

SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache"}

#: A real Google API key. The length is deliberately approximate -- the point is
#: the unmistakable prefix, not an exact match against a format that can change.
KEY_SHAPES = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\bAQ\.[0-9A-Za-z_\-]{30,}"),
]

#: Assigned, as opposed to referenced. ``TGUSERBOT_GEMINI_API_KEY=`` in an example
#: is documentation; ``KEY = "AIza..."`` is a leak.
ASSIGNMENT = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD)\w*\s*[:=]\s*[\"']?((?:AIza|AQ\.)[0-9A-Za-z_\-]{20,})",
    re.IGNORECASE,
)


def tracked_files() -> list[Path]:
    """Every file git knows about, plus the untracked ones git would add.

    Reading the working tree rather than ``git ls-files`` means an uncommitted
    key is caught before it is ever staged.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        path = REPO_ROOT / line
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {
            ".env",
            ".env.example",
            "userbotctl",
        }:
            paths.append(path)
    return paths


@pytest.fixture(scope="module")
def files() -> list[Path]:
    return tracked_files()


def test_the_scan_actually_finds_files(files: list[Path]) -> None:
    """A scan that silently matches nothing is worse than no scan."""
    assert len(files) > 20, f"only found {len(files)} files to scan"
    names = {path.name for path in files}
    assert "pyproject.toml" in names
    assert "README.md" in names


def test_the_scanner_itself_detects_a_key(files: list[Path]) -> None:
    """Guard the guard: prove the patterns match before trusting a clean result.

    Run against a real file so the same read path is exercised, then removed
    immediately -- a key must never be written into the tree, not even briefly.
    """
    target = REPO_ROOT / "README.md"
    original = target.read_text(encoding="utf-8")
    # Assembled at runtime. A literal here would be found by this file's own
    # scan, which is the correct behaviour and not something to special-case.
    fake = "AI" + "za" + "Sy" + "FAKEKEY" + "FOR" + "THEGUARD" + "ONLY1234567890"
    try:
        target.write_text(original + f"\n{original}\nKEY = {fake!r}\n", encoding="utf-8")
        content = target.read_text(encoding="utf-8")
        assert ASSIGNMENT.search(content), "the assignment pattern does not match"
        assert any(pattern.search(content) for pattern in KEY_SHAPES)
    finally:
        target.write_text(original, encoding="utf-8")
    assert target.read_text(encoding="utf-8") == original, "the file was not restored"


def test_no_api_key_is_committed(files: list[Path]) -> None:
    offenders: list[str] = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in (*KEY_SHAPES, ASSIGNMENT):
            match = pattern.search(content)
            if match:
                redacted = match.group(0)
                # Do not echo a real key into the test output.
                offenders.append(
                    f"{path.relative_to(REPO_ROOT)}: "
                    f"{redacted[:6]}...{redacted[-4:]} (len {len(redacted)})"
                )
    assert not offenders, (
        "an API key appears to be committed. Revoke it, then remove it from "
        "git history and rotate:\n" + "\n".join(offenders)
    )


def test_the_env_examples_ship_the_key_empty() -> None:
    """Opt-in, and with a pointer to where to get one."""
    for name in (".env.example", "deploy/userbot.env.example"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert re.search(r"^TGUSERBOT_GEMINI_API_KEY=\s*$", text, re.M), name
        assert "aistudio.google.com" in text, f"{name} must say where to get a key"
        assert "NEVER commit" in text, f"{name} must warn about committing"


def test_the_key_is_only_ever_read_from_the_environment() -> None:
    """No config file may carry a key, only the name of a variable."""
    manifest = (REPO_ROOT / "plugins" / "ai" / "plugin.toml").read_text(encoding="utf-8")
    assert "api_key_env" in manifest
    for forbidden in ("api_key =", "key =", "token ="):
        assert forbidden not in manifest, f"{forbidden!r} would put a key in a file"


def test_the_docs_say_where_the_key_belongs() -> None:
    doc = (REPO_ROOT / "docs" / "ai-plugin.md").read_text(encoding="utf-8")
    assert "TGUSERBOT_GEMINI_API_KEY" in doc
    assert "TGUSERBOT_GEMINI_API_KEYS" in doc
    assert "/etc/tguserbot/userbot.env" in doc


def test_no_key_leaks_through_the_client(files: list[Path]) -> None:
    """The client must scrub keys from anything it puts in front of a user."""
    client = (REPO_ROOT / "plugins" / "ai" / "_client.py").read_text(encoding="utf-8")
    assert "_redact" in client
    assert "<redacted>" in client
    # And the key is a header, never a query parameter the URL could log.
    assert "x-goog-api-key" in client
    assert 'params={"key"' not in client
