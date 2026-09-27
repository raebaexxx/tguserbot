"""Tests for reading a chat into something a model can be sent.

The mime types are not invented. Each was checked against the live API with a
real message: a Telegram voice note (OGG/Opus, 64 KB) came back transcribed in
Russian, and a "кружок" (MP4, 576 KB) came back described. The tests below pin
those exact values, because a wrong one does not fail loudly -- the model is
handed something it cannot read and simply omits it from the summary, which is a
very quiet wrong answer.

The other half is what happens when something cannot be included. Skipped media
is recorded with a reason and the reason reaches the user, because a summary that
quietly ignores half a conversation is worse than one that says so.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from conftest import shipped_submodule


@pytest.fixture(scope="module")
def sum_package() -> Any:
    """The plugin package, loaded once, with both private submodules resolved.

    Loading the package separately for each submodule gives both the same module
    name, so the first fixture's teardown removes modules the second is still
    using -- the two then disagree about what is loaded.
    """
    import importlib

    loaded, entry = shipped_submodule("sum", "plugin")
    # loaded.module is the package; the entry point is a module inside it.
    prefix = loaded.module.__name__
    package = sys.modules[prefix]
    yield SimpleNamespace(
        entry=entry,
        collect=importlib.import_module(f"{prefix}._collect"),
        prompt=importlib.import_module(f"{prefix}._prompt"),
        package=package,
    )
    from userbot.loader import cleanup_loaded_plugin

    cleanup_loaded_plugin(loaded)


@pytest.fixture(scope="module")
def collect_module(sum_package: Any) -> Any:
    return sum_package.collect


@pytest.fixture(scope="module")
def prompt_module(sum_package: Any) -> Any:
    return sum_package.prompt


class File:
    def __init__(self, name: str | None = None, mime: str | None = None) -> None:
        self.name = name
        self.mime_type = mime


class Sender:
    def __init__(self, first: str = "Аня", last: str = "Петрова", uid: int = 7) -> None:
        self.first_name = first
        self.last_name = last
        self.title: str | None = None
        self.username: str | None = None
        self.id = uid


class Msg:
    """A message with only the attributes the plugin reads."""

    def __init__(self, **kwargs: Any) -> None:
        self.id = kwargs.get("id", 1)
        self.message = kwargs.get("message", "")
        self.date = kwargs.get("date")
        self.sender = kwargs.get("sender", Sender())
        self.file = kwargs.get("file", File("f.bin"))
        self.action = kwargs.get("action")
        self.voice = kwargs.get("voice")
        self.video_note = kwargs.get("video_note")
        self.photo = kwargs.get("photo")
        self.audio = kwargs.get("audio")
        self.video = kwargs.get("video")
        self.document = kwargs.get("document")


# --- mime types, from the live check ----------------------------------------


def test_a_voice_note_declares_ogg(collect_module: Any) -> None:
    """A voice note is an OGG container holding Opus. Verified end to end."""
    message = Msg(voice=object(), file=File("voice.ogg", "audio/ogg"))
    assert collect_module.classify(message) == "голосовое"
    assert collect_module.mime_for(message, "голосовое") == "audio/ogg"


def test_a_video_message_declares_mp4(collect_module: Any) -> None:
    """The "кружок" arrives as MP4. Verified end to end."""
    message = Msg(video_note=object(), file=File("video.mp4", "video/mp4"))
    assert collect_module.classify(message) == "кружок"
    assert collect_module.mime_for(message, "кружок") == "video/mp4"


def test_a_photo_declares_jpeg(collect_module: Any) -> None:
    message = Msg(photo=object(), file=File(None, None))
    assert collect_module.classify(message) == "фото"
    assert collect_module.mime_for(message, "фото") == "image/jpeg"


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".mp3", "audio/mp3"),
        (".m4a", "audio/mp4"),
        (".wav", "audio/wav"),
        (".flac", "audio/flac"),
        (".ogg", "audio/ogg"),
        (".png", "image/png"),
    ],
)
def test_documents_use_their_extension(collect_module: Any, suffix: str, expected: str) -> None:
    message = Msg(document=object(), file=File(f"file{suffix}", None))
    assert collect_module.mime_for(message, "документ") == expected


def test_an_unknown_format_is_refused_rather_than_guessed(collect_module: Any) -> None:
    """A guessed mime gives a model error about a file the user never mentioned."""
    message = Msg(document=object(), file=File("archive.qqq", "application/x-weird"))
    assert collect_module.mime_for(message, "документ") is None


def test_a_message_with_named_media_uses_telegrams_own_type(collect_module: Any) -> None:
    message = Msg(audio=object(), file=File("song.bin", "audio/mpeg"))
    assert collect_module.classify(message) == "аудио"
    assert collect_module.mime_for(message, "аудио") == "audio/mpeg"


# --- classification order ---------------------------------------------------


def test_voice_wins_over_document(collect_module: Any) -> None:
    """A voice note arrives as both voice and a document."""
    message = Msg(voice=object(), document=object(), file=File("v.ogg", "audio/ogg"))
    assert collect_module.classify(message) == "голосовое"


def test_video_note_wins_over_video(collect_module: Any) -> None:
    message = Msg(video_note=object(), video=object(), file=File("v.mp4", "video/mp4"))
    assert collect_module.classify(message) == "кружок"


def test_a_text_only_message_has_no_kind(collect_module: Any) -> None:
    assert collect_module.classify(Msg(message="привет")) is None


# --- the transcript ---------------------------------------------------------


def test_the_transcript_names_who_spoke(collect_module: Any) -> None:
    entries = [
        (Msg(message="привет"), "привет"),
        (Msg(message="пока"), "пока"),
    ]
    text, truncated = collect_module.build_transcript(entries, 10_000)
    assert "Аня Петрова: привет" in text
    assert "Аня Петрова: пока" in text
    assert truncated is False


def test_an_empty_attachment_is_named_rather_than_blank(collect_module: Any) -> None:
    """A voice message has no text; the line has to say what it was."""
    entries = [(Msg(), "(голосовое)")]
    text, _ = collect_module.build_transcript(entries, 10_000)
    assert "(голосовое)" in text


def test_an_over_long_transcript_drops_the_oldest_lines(collect_module: Any) -> None:
    """The recent turns are what the user is asking about."""
    entries = [(Msg(message="x" * 100, id=index), "x" * 100) for index in range(50)]
    text, truncated = collect_module.build_transcript(entries, 500)
    assert truncated is True
    assert len(text) <= 500
    # The last entry must survive; the first must not.
    assert text.strip().splitlines()[-1].endswith("x" * 100)


def test_a_transcript_within_the_limit_is_untouched(collect_module: Any) -> None:
    entries = [(Msg(message="привет"), "привет")]
    text, truncated = collect_module.build_transcript(entries, 100_000)
    assert truncated is False
    assert "привет" in text


# --- notices ----------------------------------------------------------------


def test_a_clean_collection_has_no_notices(collect_module: Any) -> None:
    assert collect_module.Collected(transcript="x", message_count=3).notices() == []


def test_a_truncated_transcript_is_reported(collect_module: Any) -> None:
    collected = collect_module.Collected(transcript="x", truncated_transcript=True)
    assert any("обрезан" in note for note in collected.notices())


def test_skipped_media_is_reported_with_a_reason(collect_module: Any) -> None:
    collected = collect_module.Collected(
        skipped=[collect_module.SkippedMedia(7, "голосовое больше лимита")]
    )
    notices = collected.notices()
    assert any("больше лимита" in note for note in notices)


def test_repeated_reasons_are_counted(collect_module: Any) -> None:
    collected = collect_module.Collected(
        skipped=[collect_module.SkippedMedia(i, "голосовое больше лимита") for i in range(3)]
    )
    notices = collected.notices()
    assert len(notices) == 1
    assert "3 шт." in notices[0], notices


def test_a_general_note_is_reported(collect_module: Any) -> None:
    collected = collect_module.Collected(notes=["медиа не отправляются"])
    assert any("медиа не отправляются" in note for note in collected.notices())


# --- the budget -------------------------------------------------------------


def test_media_within_the_budget_is_kept(collect_module: Any, tmp_path: Path) -> None:
    collected = collect_module.Collected()
    file = tmp_path / "a.ogg"
    file.write_bytes(b"x" * 100)
    item = collect_module.MediaItem(1, "голосовое", "audio/ogg", file, 100)
    collect_module.take_media(collected, [(None, item)], max_file_bytes=1000, max_total_bytes=1000)
    assert len(collected.media) == 1
    assert collected.skipped == []


def test_a_file_over_the_per_file_limit_is_skipped(collect_module: Any, tmp_path: Path) -> None:
    collected = collect_module.Collected()
    file = tmp_path / "big.ogg"
    file.write_bytes(b"x" * 2000)
    item = collect_module.MediaItem(1, "голосовое", "audio/ogg", file, 2000)
    collect_module.take_media(
        collected, [(None, item)], max_file_bytes=1000, max_total_bytes=10_000
    )
    assert collected.media == []
    assert "больше лимита" in collected.notices()[0]


def test_the_total_budget_is_respected(collect_module: Any, tmp_path: Path) -> None:
    collected = collect_module.Collected()
    items = []
    for index in range(5):
        file = tmp_path / f"{index}.ogg"
        file.write_bytes(b"x" * 100)
        items.append((None, collect_module.MediaItem(index, "голосовое", "audio/ogg", file, 100)))
    collect_module.take_media(collected, items, max_file_bytes=1000, max_total_bytes=250)
    assert collected.total_media_bytes <= 250
    assert len(collected.media) == 2
    assert len(collected.skipped) == 3, "the rest must be reported, not dropped silently"


def test_the_newest_media_is_kept(collect_module: Any, tmp_path: Path) -> None:
    """The order handed in is newest-first, so that is what the budget buys."""
    collected = collect_module.Collected()
    items = []
    for index in range(4):
        file = tmp_path / f"{index}.ogg"
        file.write_bytes(b"x" * 100)
        items.append((None, collect_module.MediaItem(index, "голосовое", "audio/ogg", file, 100)))
    collect_module.take_media(collected, items, max_file_bytes=1000, max_total_bytes=200)
    assert [item.message_id for item in collected.media] == [0, 1]


# --- the request ------------------------------------------------------------


def test_the_transcript_comes_before_the_media(prompt_module: Any) -> None:
    """The prompt says "the media below", so the media has to be below."""
    from userbot.gemini import Part

    parts = prompt_module.build_request_parts("слова", [Part.media("audio/ogg", "AAA")], [])
    assert "слова" in parts[0].text
    # A header names the attachments before they appear.
    assert "ложения" in parts[1].text
    assert parts[2].inline_data == {"mime_type": "audio/ogg", "data": "AAA"}


def test_media_is_omitted_entirely_when_there_is_none(prompt_module: Any) -> None:
    parts = prompt_module.build_request_parts("слова", [], [])
    assert all(part.inline_data is None for part in parts)
    assert len(parts) == 2, "transcript plus the instruction is enough"


def test_notices_travel_with_the_request(prompt_module: Any) -> None:
    parts = prompt_module.build_request_parts("слова", [], ["медиа не отправляются"])
    assert any("не отправляются" in (part.text or "") for part in parts)


def test_the_prompt_asks_for_the_language_of_the_conversation(prompt_module: Any) -> None:
    assert "language" in prompt_module.SUMMARY_SYSTEM


def test_the_prompt_forbids_inventing(prompt_module: Any) -> None:
    assert (
        "not invent" in prompt_module.SUMMARY_SYSTEM
        or "Do not invent" in prompt_module.SUMMARY_SYSTEM
    )


def test_the_prompt_explains_the_parenthesised_placeholders(prompt_module: Any) -> None:
    """A voice note with no text is the most common confusing case."""
    assert "parentheses" in prompt_module.SUMMARY_SYSTEM
