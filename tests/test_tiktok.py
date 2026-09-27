"""Tests for the TikTok plugin's interaction with yt-dlp.

The important ones here build a *real* ``YoutubeDL`` from the plugin's *real*
option mapping. Every previous test replaced ``_download_sync`` wholesale, which
meant the option mapping was never executed -- and that is exactly how
``"impersonate": "chrome"`` shipped: yt-dlp only accepts an ``ImpersonateTarget``
(the string-to-enum conversion lives in its CLI path), so the constructor raised
``AssertionError`` and every download failed. Nothing noticed, because nothing
ran the code.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from conftest import FakeContext, FakeEvent, shipped_module

# --- the option mapping is accepted by the installed yt-dlp ----------------


@pytest.fixture(scope="module")
def tiktok_module() -> Any:
    """The shipped plugin's module, loaded once.

    Loading is the expensive part; each test gets a *fresh* instance from it, so
    no test inherits mutated state (a config override, a temporary directory)
    from the one before it.
    """
    from userbot.loader import cleanup_loaded_plugin

    loaded, module = shipped_module("tiktok")
    yield module
    cleanup_loaded_plugin(loaded)


@pytest.fixture
def tiktok(tiktok_module: Any) -> tuple[Any, Any]:
    """A fresh, wired-up plugin instance plus the module it came from."""
    plugin = tiktok_module.Plugin()
    plugin.ctx = FakeContext()
    plugin.download_lock = asyncio.Lock()
    plugin.progress_lock = asyncio.Lock()
    plugin._temporary_dir = Path("/tmp")
    return plugin, tiktok_module


def make_plugin(tiktok: tuple[Any, Any]) -> Any:
    plugin, _module = tiktok
    return plugin


def test_the_real_youtube_accepts_the_real_options(tiktok: tuple[Any, Any]) -> None:
    """The mapping the plugin builds must construct a real YoutubeDL.

    This is the test that was missing: it is what catches an option whose value
    has the wrong type or is in the wrong form, which the constructor rejects
    long before a download would.
    """
    from yt_dlp import YoutubeDL

    plugin = make_plugin(tiktok)
    options = plugin.build_options(lambda _data: None)
    # No network, no filesystem writes: constructing YoutubeDL only resolves
    # request handlers.
    with YoutubeDL(options):
        pass


def test_options_use_an_impersonate_target_not_a_string(tiktok: tuple[Any, Any]) -> None:
    """Regression: a plain string makes YoutubeDL.__init__ raise AssertionError."""
    from yt_dlp.networking.impersonate import ImpersonateTarget

    plugin = make_plugin(tiktok)
    options = plugin.build_options(lambda _data: None)
    assert "impersonate" in options, "impersonation must stay enabled"
    assert isinstance(options["impersonate"], ImpersonateTarget)
    assert options["impersonate"].client == "chrome"


def test_impersonation_is_dropped_rather_than_crashing_when_unavailable(
    tiktok: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from yt_dlp import YoutubeDL

    plugin, module = tiktok
    plugin = make_plugin(tiktok)
    monkeypatch.setattr(module, "_impersonate_target", lambda: None)
    options = plugin.build_options(lambda _data: None)
    assert "impersonate" not in options
    with YoutubeDL(options):
        pass


def test_size_limit_comes_from_the_configuration(tiktok: tuple[Any, Any]) -> None:
    plugin = make_plugin(tiktok)
    plugin.max_file_size = 4 * 1024 * 1024
    assert plugin.build_options(lambda _d: None)["max_filesize"] == 4 * 1024 * 1024


def test_options_never_touch_process_environment(tiktok: tuple[Any, Any]) -> None:
    """Regression: YTDLP_NO_PLUGINS was set from a worker thread."""
    import os

    before = dict(os.environ)
    make_plugin(tiktok).build_options(lambda _d: None)
    assert dict(os.environ) == before


def test_outtmpl_follows_the_temporary_directory(tiktok: tuple[Any, Any]) -> None:
    plugin, _module = tiktok
    plugin = make_plugin(tiktok)
    plugin._temporary_dir = Path("/tmp/somewhere")
    template = plugin.build_options(lambda _d: None)["outtmpl"]
    assert template.startswith("/tmp/somewhere/")
    assert "%(ext)s" in template


def test_options_suppress_output_and_caches(tiktok: tuple[Any, Any]) -> None:
    options = make_plugin(tiktok).build_options(lambda _d: None)
    assert options["quiet"] is True
    assert options["noprogress"] is True
    assert options["no_warnings"] is True
    assert options["cachedir"] is False
    assert options["noplaylist"] is True
    # Not max_downloads: it made yt-dlp raise on a single video. See
    # tests/test_tiktok_retry.py for the live failure it caused.
    assert "max_downloads" not in options


def test_progress_hook_is_registered(tiktok: tuple[Any, Any]) -> None:
    def hook(_data: dict[str, Any]) -> None:
        return None

    options = make_plugin(tiktok).build_options(hook)
    assert options["progress_hooks"] == [hook]


def test_logger_is_the_plugin_logger(tiktok: tuple[Any, Any]) -> None:
    plugin = make_plugin(tiktok)
    options = plugin.build_options(lambda _d: None)
    assert options["logger"].logger is plugin.ctx.logger


# --- option names are real --------------------------------------------------


def ytdlp_option_names() -> set[str]:
    """Every ``params`` key the installed yt-dlp reads, plus its CLI dests.

    Derived from the installed package rather than hardcoded, so the check keeps
    working across yt-dlp versions. ``params.get`` is read from more than
    ``YoutubeDL`` itself -- ``continuedl``, for instance, is consumed by the
    downloader layer -- so the whole package is scanned.
    """
    import yt_dlp

    root = Path(yt_dlp.__file__).parent
    names: set[str] = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError, ValueError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "params"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "params"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                names.add(node.slice.value)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_option"
            ):
                for keyword in node.keywords:
                    value = keyword.value
                    if (
                        keyword.arg == "dest"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        names.add(value.value)
    return names


#: Options yt-dlp consumes into attributes or handlers rather than reading back
#: out of ``params``, so no static scan can see them.
PROGRAMMATIC_OPTIONS = {"progress_hooks", "postprocessor_hooks", "logger"}


def test_every_option_name_is_a_real_yt_dlp_option(tiktok: tuple[Any, Any]) -> None:
    """yt-dlp stores unknown keys in params without complaining.

    That is how "plugin_dirs" (wrong type, and a no-op on the library path) and a
    misspelled key could sit in the options unnoticed. Every key is checked
    against the params the installed yt-dlp actually reads.
    """
    known = ytdlp_option_names() | PROGRAMMATIC_OPTIONS
    used = set(make_plugin(tiktok).build_options(lambda _d: None))
    unknown = used - known
    assert not unknown, f"yt-dlp never reads these options: {sorted(unknown)}"


def test_the_option_reference_set_works() -> None:
    """Guard the guard: the reference set must be non-trivial and discriminating."""
    known = ytdlp_option_names() | PROGRAMMATIC_OPTIONS
    assert len(known) > 100, "the reference set collapsed"
    for real in ("impersonate", "outtmpl", "max_filesize", "continuedl", "retries"):
        assert real in known, real
    for bogus in ("totally_bogus_key", "plugin_dir", "impersonate_target", "noplaylists"):
        assert bogus not in known, bogus


def test_ytdlp_silently_accepts_unknown_keys() -> None:
    """Documents *why* the allow-list test exists."""
    from typing import Any as _Any

    from yt_dlp import YoutubeDL

    # Deliberately bogus: the point is that yt-dlp keeps it in params without
    # complaint, which is what let a mistyped option sit unnoticed for months.
    options: dict[str, _Any] = {"quiet": True, "totally_bogus_key": 1}
    with YoutubeDL(cast(_Any, options)) as downloader:
        # yt-dlp types params as a closed TypedDict; the runtime keeps the key,
        # which is precisely why a typo is invisible to the type checker.
        assert cast(dict[str, _Any], downloader.params)["totally_bogus_key"] == 1


# --- url validation --------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "valid"),
    [
        ("https://www.tiktok.com/@u/video/1", True),
        ("https://vm.tiktok.com/short/", True),
        ("https://m.tiktokv.com/x", True),
        ("https://tiktok.com.evil.example/x", False),
        ("https://notiktok.com/x", False),
        ("http://www.tiktok.com/x", False),
        ("https://www.tiktok.com:8443/x", False),
        ("https://user:pw@www.tiktok.com/x", False),
        ("", False),
    ],
)
def test_url_validation(url: str, valid: bool) -> None:
    _, module = shipped_module("tiktok")
    if valid:
        assert module.extract_tiktok_url(url) == url
    else:
        with pytest.raises(module.TikTokDownloadError):
            module.extract_tiktok_url(url)


def test_url_extraction_requires_exactly_one_link() -> None:
    _, module = shipped_module("tiktok")
    with pytest.raises(module.TikTokDownloadError, match="ровно одна"):
        module.extract_tiktok_url("https://a.tiktok.com/1 https://b.tiktok.com/2")
    with pytest.raises(module.TikTokDownloadError, match="ровно одна"):
        module.extract_tiktok_url("no links here")


def test_url_extraction_strips_trailing_punctuation() -> None:
    _, module = shipped_module("tiktok")
    assert module.extract_tiktok_url("смотри https://vm.tiktok.com/x.") == "https://vm.tiktok.com/x"


def test_url_extraction_rejects_an_overlong_link() -> None:
    _, module = shipped_module("tiktok")
    with pytest.raises(module.TikTokDownloadError, match="слишком длинная"):
        module.extract_tiktok_url("https://vm.tiktok.com/" + "x" * 4000)


def test_allowed_domains_are_configurable(tiktok: tuple[Any, Any]) -> None:
    plugin = make_plugin(tiktok)
    plugin.allowed_domains = ("example.com",)
    _, module = shipped_module("tiktok")
    assert module.extract_tiktok_url("https://cdn.example.com/v", ("example.com",))
    with pytest.raises(module.TikTokDownloadError):
        module.extract_tiktok_url("https://www.tiktok.com/x", ("example.com",))


# --- download flow ---------------------------------------------------------


def make_fake_download(
    video_bytes: bytes = b"video",
    *,
    size: int | None = None,
) -> Any:
    def fake_download(_url: str, temporary_dir: Path, progress_hook: Any) -> Path:
        loop = progress_hook
        assert loop is not None
        path = temporary_dir / "video.mp4"
        path.write_bytes(video_bytes * ((size or len(video_bytes)) // max(1, len(video_bytes))))
        return path

    return fake_download


def test_the_real_download_reports_the_video_file(tiktok: tuple[Any, Any]) -> None:
    """Exercise _download_sync up to the point where yt-dlp would fetch.

    extract_info is replaced, but the real option mapping and the real result
    handling run.
    """
    plugin, module = shipped_module_instance(tiktok)
    captured: dict[str, Any] = {}

    class FakeYDL:
        def __init__(self, options: dict[str, Any]) -> None:
            captured["options"] = options

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def extract_info(self, url: str, download: bool = True) -> dict[str, Any]:
            captured["url"] = url
            captured["download"] = download
            return {"_type": "video", "id": "1"}

    import yt_dlp

    monkey = pytest.MonkeyPatch()
    monkey.setattr(yt_dlp, "YoutubeDL", FakeYDL)
    try:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "video.mp4").write_bytes(b"data")
            with monkey.context():
                result = plugin._download_sync("https://vm.tiktok.com/x", target, lambda _d: None)
            assert result == target / "video.mp4"
            assert captured["download"] is True
            assert captured["url"] == "https://vm.tiktok.com/x"
            assert "impersonate" in captured["options"]
    finally:
        monkey.undo()
    _ = module


def shipped_module_instance(tiktok: tuple[Any, Any]) -> tuple[Any, Any]:
    return make_plugin(tiktok), tiktok[1]


async def test_owner_downloads_and_the_command_is_deleted(
    tiktok: tuple[Any, Any],
) -> None:
    plugin = make_plugin(tiktok)
    plugin._download_sync = make_fake_download()
    event = FakeEvent(raw_text="/ub tt https://vm.tiktok.com/example/", sender_id=1)
    await plugin.handle_command(SimpleNamespace(event=event, args=event.raw_text.split(" ", 2)[2]))
    assert event.files
    assert event.deleted
    assert not await asyncio.to_thread(Path(event.files[0]).exists)
    assert any("Отправляю видео" in text for text in event.edits)
    assert not any(text.startswith("⏳") for text in event.edits[-1:])


async def test_non_owner_direct_command_is_ignored(tiktok: tuple[Any, Any]) -> None:
    plugin = make_plugin(tiktok)
    plugin._download_sync = make_fake_download()
    event = FakeEvent(raw_text="/tt https://vm.tiktok.com/example/", sender_id=2)
    await plugin.handle_direct_command(event)
    assert not event.deleted
    assert not event.files


async def test_oversized_file_is_reported_and_the_command_kept(
    tiktok: tuple[Any, Any],
) -> None:
    plugin = make_plugin(tiktok)
    plugin.max_file_size = 4
    plugin._download_sync = make_fake_download(b"x" * 64)
    event = FakeEvent(raw_text="/ub tt https://vm.tiktok.com/x", sender_id=1)
    await plugin.handle_command(SimpleNamespace(event=event, args="https://vm.tiktok.com/x"))
    assert not event.files
    assert not event.deleted
    assert any("слишком большое" in text for text in event.edits)


async def test_a_bad_url_never_reaches_yt_dlp(tiktok: tuple[Any, Any]) -> None:
    plugin = make_plugin(tiktok)
    called: list[str] = []

    def spy(url: str, temporary_dir: Path, progress_hook: Any) -> Path:
        called.append(url)
        raise AssertionError("should not be called")

    plugin._download_sync = spy
    event = FakeEvent(raw_text="/ub tt https://example.com/video", sender_id=1)
    await plugin.handle_command(SimpleNamespace(event=event, args="https://example.com/video"))
    assert called == []
    assert any("tiktok.com" in text for text in event.edits)


async def test_a_download_failure_reports_an_error_status(tiktok: tuple[Any, Any]) -> None:
    plugin = make_plugin(tiktok)

    def boom(url: str, temporary_dir: Path, progress_hook: Any) -> Path:
        raise RuntimeError("network down")

    plugin._download_sync = boom
    event = FakeEvent(raw_text="/ub tt https://vm.tiktok.com/x", sender_id=1)
    await plugin.handle_command(SimpleNamespace(event=event, args="https://vm.tiktok.com/x"))
    assert not event.files
    assert not event.deleted
    assert any("Не удалось скачать" in text for text in event.edits)


async def test_the_progress_reporter_is_throttled_and_stopped_before_upload(
    tiktok: tuple[Any, Any],
) -> None:
    """Regression: the reporter used to edit ~2x/second for the whole upload."""
    plugin, module = tiktok
    plugin = make_plugin(tiktok)
    loop = asyncio.get_running_loop()
    state = module._ProgressState()
    state.update(100, 10, 10, None, None)
    event = FakeEvent(raw_text="/ub tt x", sender_id=1)
    stop = asyncio.Event()
    stop.set()
    await plugin._report_progress(event, state, stop)
    assert event.edits == [], "a finished download must not keep editing"

    # While running, edits are rate-limited to PROGRESS_MIN_INTERVAL.
    edits: list[str] = []
    stop.clear()

    async def fake_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    original = module.PROGRESS_TICK
    monkey = pytest.MonkeyPatch()
    monkey.setattr(module, "PROGRESS_TICK", 0.01)
    try:
        task = loop.create_task(plugin._report_progress(event, state, stop))
        for _ in range(50):
            await fake_sleep(0)
        stop.set()
        await task
    finally:
        monkey.undo()
        module.PROGRESS_TICK = original
    edits = event.edits
    assert len(edits) <= 2, f"throttle not applied: {len(edits)} edits"


async def test_an_edit_failure_stops_the_reporter(tiktok: tuple[Any, Any]) -> None:
    plugin, module = tiktok
    plugin = make_plugin(tiktok)

    class BrokenEvent(FakeEvent):
        async def edit_text(self, text: str, **kwargs: Any) -> None:
            raise RuntimeError("message gone")

    state = module._ProgressState()
    state.update(10, 1, 10, None, None)
    stop = asyncio.Event()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(module, "PROGRESS_TICK", 0.01)
    try:
        await asyncio.wait_for(
            plugin._report_progress(BrokenEvent(raw_text="/ub tt x", sender_id=1), state, stop),
            timeout=2,
        )
    except TimeoutError:
        pytest.fail("the reporter kept retrying after editing started failing")
    finally:
        monkey.undo()


# --- the shipped manifest --------------------------------------------------


def test_manifest_declares_the_documented_config_keys() -> None:
    from userbot.loader import PluginManifest

    manifest = PluginManifest.from_path(REPO_PLUGINS / "tiktok")
    assert manifest.config["max_file_mib"] == 50
    assert manifest.config["allowed_domains"] == ["tiktok.com", "tiktokv.com"]


REPO_PLUGINS = Path(__file__).resolve().parent.parent / "plugins"


def test_logger_class_routes_to_python_logging() -> None:
    _, module = shipped_module("tiktok")
    records: list[tuple[str, str]] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append((record.levelname, record.getMessage()))

    logger = logging.getLogger("test.tiktok.ytdlp")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(Capture())
    try:
        ydl_logger = module._YDLLogger(logger)
        ydl_logger.debug("d")
        ydl_logger.info("i")
        ydl_logger.warning("w")
        ydl_logger.error("e")
    finally:
        logger.handlers.clear()
    assert [level for level, _ in records] == ["DEBUG", "DEBUG", "WARNING", "ERROR"]
