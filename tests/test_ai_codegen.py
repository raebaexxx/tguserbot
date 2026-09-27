"""Tests for turning a model reply into files on disk.

The model is the untrusted party here, not the operator. It is constrained to a
JSON schema, which makes its output parseable, but every *path* in that output is
still attacker-influenced data if the prompt was influenced -- and a path like
``../../.bashrc`` would put generated code outside the staging directory entirely.

So the tests below are mostly negative: traversal, absolute paths, symlink-ish
names, oversized trees, duplicate files, and the required file set. A generator
that merely fails to produce something is annoying; one that writes outside its
sandbox is a remote code execution path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import shipped_submodule


@pytest.fixture(scope="module")
def codegen() -> Any:
    loaded, module = shipped_submodule("ai", "_codegen")
    yield module
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


def reply(**overrides: Any) -> str:
    payload = {
        "name": "ping",
        "summary": "Отвечает pong.",
        "files": [
            {"path": "plugin.toml", "content": 'name = "ping"\n'},
            {"path": "__init__.py", "content": "from .plugin import Plugin\n"},
            {"path": "plugin.py", "content": "class Plugin:\n    pass\n"},
        ],
    }
    payload.update(overrides)
    return json.dumps(payload)


# --- path validation --------------------------------------------------------

ESCAPES = [
    "../evil.py",
    "../../etc/passwd",
    "a/../../b.py",
    "./../x.py",
    "..",
    "../",
    "/etc/passwd",
    "/tmp/x.py",
    "C:/Windows/x.py",
    "c:x.py",
    "..\\..\\evil.py",
    "sub/../../../evil.py",
    "\x00.py",
    "",
    "   ",
    ".",
    "./",
]

TRAVERSAL = [
    "../evil.py",
    "../../etc/passwd",
    "a/../../b.py",
    "./../x.py",
    "..",
    "../",
    "..\\..\\evil.py",
    "sub/../../../evil.py",
]


@pytest.mark.parametrize("path", ESCAPES)
def test_unsafe_paths_are_rejected(codegen: Any, path: str) -> None:
    with pytest.raises(codegen.GeneratedFileError):
        codegen.validate_relative_path(path)


@pytest.mark.parametrize("path", TRAVERSAL)
def test_traversal_names_the_reason(codegen: Any, path: str) -> None:
    """The error has to say why, or the owner cannot judge the retry."""
    with pytest.raises(codegen.GeneratedFileError, match="запрещён|выход"):
        codegen.validate_relative_path(path)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("plugin.py", "plugin.py"),
        ("a/b/c.py", "a/b/c.py"),
        ("./plugin.py", "plugin.py"),
        ("a//b.py", "a/b.py"),
        ("sub/plugin.toml", "sub/plugin.toml"),
        ("_private.py", "_private.py"),
        ("file.with.dots.py", "file.with.dots.py"),
    ],
)
def test_safe_paths_are_normalised(codegen: Any, path: str, expected: str) -> None:
    """Normalised, so what is reviewed is what is written."""
    assert codegen.validate_relative_path(path) == expected


@pytest.mark.parametrize("path", ["with space.py", "-leading.py", ".hidden", "a$b.py", "x" * 100])
def test_awkward_names_are_rejected(codegen: Any, path: str) -> None:
    with pytest.raises(codegen.GeneratedFileError):
        codegen.validate_relative_path(path)


def test_a_path_deeper_than_the_limit_is_rejected(codegen: Any) -> None:
    deep = "/".join(["a"] * (codegen.MAX_DEPTH + 1))
    with pytest.raises(codegen.GeneratedFileError, match="глубок"):
        codegen.validate_relative_path(deep)


def test_a_path_at_the_limit_is_accepted(codegen: Any) -> None:
    ok = "/".join(["a"] * codegen.MAX_DEPTH)
    assert codegen.validate_relative_path(ok) == ok


# --- parsing ----------------------------------------------------------------


def test_a_good_reply_is_parsed(codegen: Any) -> None:
    plugin = codegen.parse_generated(reply())
    assert plugin.name == "ping"
    assert plugin.summary == "Отвечает pong."
    assert [f.path for f in plugin.files] == ["plugin.toml", "__init__.py", "plugin.py"]
    assert plugin.as_mapping()["plugin.py"] == "class Plugin:\n    pass\n"


def test_the_requested_name_must_match(codegen: Any) -> None:
    """Otherwise /ub ai new ping could stage something called notes."""
    with pytest.raises(codegen.GeneratedFileError, match="назвала"):
        codegen.parse_generated(reply(), expected_name="other")


def test_a_matching_name_is_accepted(codegen: Any) -> None:
    assert codegen.parse_generated(reply(), expected_name="ping").name == "ping"


def test_a_fenced_reply_is_unwrapped(codegen: Any) -> None:
    """Some models fence JSON despite the schema; the content is still usable."""
    fenced = f"```json\n{reply()}\n```"
    assert codegen.parse_generated(fenced).name == "ping"


def test_a_name_with_uppercase_is_lowercased(codegen: Any) -> None:
    assert codegen.parse_generated(reply(name="Ping")).name == "ping"


@pytest.mark.parametrize("name", ["", "   ", "Плагин", "-bad", "a" * 100, "with space"])
def test_bad_names_are_rejected(codegen: Any, name: str) -> None:
    with pytest.raises(codegen.GeneratedFileError):
        codegen.parse_generated(reply(name=name))


@pytest.mark.parametrize("raw", ["not json at all", "{", "[1,2,3]", '"a string"', "42"])
def test_non_object_replies_are_rejected(codegen: Any, raw: str) -> None:
    with pytest.raises(codegen.GeneratedFileError):
        codegen.parse_generated(raw)


def test_a_reply_with_no_files_is_rejected(codegen: Any) -> None:
    with pytest.raises(codegen.GeneratedFileError, match="нет ни одного файла"):
        codegen.parse_generated(reply(files=[]))


def test_a_missing_required_file_is_named(codegen: Any) -> None:
    """The model must be told exactly what it forgot."""
    with pytest.raises(codegen.GeneratedFileError, match="plugin.toml"):
        codegen.parse_generated(
            reply(
                files=[
                    {"path": "__init__.py", "content": "x"},
                    {"path": "plugin.py", "content": "y"},
                ]
            )
        )


def test_all_three_required_files_must_be_present(codegen: Any) -> None:
    for missing in ("plugin.toml", "__init__.py", "plugin.py"):
        files = [f for f in reply_files() if not f["path"].endswith(missing)]
        with pytest.raises(codegen.GeneratedFileError, match=missing.replace(".", r"\.")):
            codegen.parse_generated(reply(files=files))


def reply_files() -> list[dict[str, str]]:
    return [
        {"path": "plugin.toml", "content": "x"},
        {"path": "__init__.py", "content": "y"},
        {"path": "plugin.py", "content": "z"},
    ]


def test_a_duplicate_file_is_rejected(codegen: Any) -> None:
    """Otherwise the second copy silently wins and the review shows the first."""
    files = reply_files() + [{"path": "plugin.py", "content": "different"}]
    with pytest.raises(codegen.GeneratedFileError, match="дважды"):
        codegen.parse_generated(reply(files=files))


def test_two_paths_normalising_to_one_are_a_duplicate(codegen: Any) -> None:
    files = reply_files() + [{"path": "./plugin.py", "content": "different"}]
    with pytest.raises(codegen.GeneratedFileError, match="дважды"):
        codegen.parse_generated(reply(files=files))


def test_a_traversing_file_stops_the_whole_reply(codegen: Any) -> None:
    """One bad path is enough; nothing gets written."""
    files = reply_files() + [{"path": "../evil.py", "content": "import os"}]
    with pytest.raises(codegen.GeneratedFileError):
        codegen.parse_generated(reply(files=files))


def test_an_oversized_reply_is_rejected(codegen: Any) -> None:
    files = reply_files() + [{"path": "big.py", "content": "x" * (codegen.MAX_TOTAL_BYTES + 1)}]
    with pytest.raises(codegen.GeneratedFileError, match="объём"):
        codegen.parse_generated(reply(files=files))


def test_a_malformed_file_entry_is_rejected(codegen: Any) -> None:
    with pytest.raises(codegen.GeneratedFileError):
        codegen.parse_generated(reply(files=reply_files() + ["junk"]))


def test_non_string_content_is_rejected(codegen: Any) -> None:
    with pytest.raises(codegen.GeneratedFileError, match="строк"):
        codegen.parse_generated(reply(files=reply_files() + [{"path": "x.py", "content": 123}]))


def test_a_missing_summary_is_tolerated(codegen: Any) -> None:
    """Not worth failing a whole plugin over."""
    payload = json.loads(reply())
    payload.pop("summary")
    assert codegen.parse_generated(json.dumps(payload)).summary == ""


def test_extra_files_are_allowed(codegen: Any) -> None:
    files = reply_files() + [{"path": "helpers.py", "content": "X = 1"}]
    plugin = codegen.parse_generated(reply(files=files))
    assert "helpers.py" in plugin.as_mapping()


# --- the schema handed to the model ----------------------------------------


def test_the_schema_is_what_the_parser_expects(codegen: Any) -> None:
    """If these drift, generation silently starts failing for everyone."""
    schema = codegen.PLUGIN_SCHEMA
    assert set(schema["required"]) == {"name", "summary", "files"}
    item = schema["properties"]["files"]["items"]
    assert set(item["required"]) == {"path", "content"}


# --- writing ----------------------------------------------------------------


def test_write_plugin_creates_the_tree(codegen: Any, tmp_path: Path) -> None:
    plugin = codegen.parse_generated(reply())
    destination = codegen.write_plugin(tmp_path, plugin)
    assert destination == (tmp_path / "ping").resolve()
    assert (destination / "plugin.toml").is_file()
    assert (destination / "plugin.py").is_file()
    assert (destination / "plugin.py").read_text() == "class Plugin:\n    pass\n"


def test_write_plugin_creates_subdirectories(codegen: Any, tmp_path: Path) -> None:
    files = reply_files() + [{"path": "sub/deep/helper.py", "content": "X = 1"}]
    plugin = codegen.parse_generated(reply(files=files))
    destination = codegen.write_plugin(tmp_path, plugin)
    assert (destination / "sub" / "deep" / "helper.py").is_file()


def test_write_plugin_replaces_an_earlier_generation(codegen: Any, tmp_path: Path) -> None:
    """Regenerating must not merge with the old tree and leave stale files."""
    first = codegen.parse_generated(
        reply(
            files=[
                {"path": "plugin.toml", "content": "old"},
                {"path": "__init__.py", "content": "old"},
                {"path": "plugin.py", "content": "OLD"},
                {"path": "stale.py", "content": "gone"},
            ]
        )
    )
    destination = codegen.write_plugin(tmp_path, first)
    assert (destination / "stale.py").is_file()

    second = codegen.parse_generated(
        reply(
            files=[
                {"path": "plugin.toml", "content": "new"},
                {"path": "__init__.py", "content": "new"},
                {"path": "plugin.py", "content": "NEW"},
            ]
        )
    )
    destination = codegen.write_plugin(tmp_path, second)
    assert (destination / "plugin.py").read_text() == "NEW"
    assert not (destination / "stale.py").exists(), "a removed file must not survive"


def test_write_plugin_creates_the_tree_at_the_given_root(codegen: Any, tmp_path: Path) -> None:
    plugin = codegen.parse_generated(reply())
    target = codegen.write_plugin(tmp_path, plugin)
    assert target == (tmp_path / "ping").resolve()
    assert (target / "plugin.toml").is_file()


def test_write_plugin_refuses_a_traversing_name(codegen: Any, tmp_path: Path) -> None:
    """The second line of defence, behind validate_relative_path.

    Built directly instead of through parse_generated so the check is exercised
    on its own: if path validation is ever relaxed, this still holds.
    """
    plugin = codegen.GeneratedPlugin(
        name="../evil",
        summary="",
        files=(codegen.GeneratedFile(path="plugin.py", content="x"),),
    )
    with pytest.raises(codegen.GeneratedFileError, match="проверка пути"):
        codegen.write_plugin(tmp_path, plugin)
    assert not (tmp_path.parent / "evil").exists(), "nothing may be written outside the root"


def test_write_plugin_refuses_a_deeply_nested_name(codegen: Any, tmp_path: Path) -> None:
    plugin = codegen.GeneratedPlugin(
        name="a/b",
        summary="",
        files=(codegen.GeneratedFile(path="plugin.py", content="x"),),
    )
    with pytest.raises(codegen.GeneratedFileError, match="проверка пути"):
        codegen.write_plugin(tmp_path, plugin)


# --- the interaction with the safety review ---------------------------------


def test_a_generated_plugin_that_reaches_for_a_shell_is_flagged(codegen: Any) -> None:
    """The review the owner sees before typing /ub plugin adopt."""
    from userbot.safety import review_tree

    files = reply_files()
    files[2] = {
        "path": "plugin.py",
        "content": "import os\n\n\ndef f():\n    return os.system('id')\n",
    }
    plugin = codegen.parse_generated(reply(files=files))
    report = review_tree(plugin.as_mapping())
    assert not report.ok
    assert "system" in report.summary()


def test_a_generated_clean_plugin_passes_the_review(codegen: Any) -> None:
    from userbot.safety import review_tree

    report = review_tree(codegen.parse_generated(reply()).as_mapping())
    assert report.ok and not report.findings, report.summary()
