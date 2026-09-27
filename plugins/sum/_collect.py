"""Collecting a chat into something a model can read.

The transcript is the easy half. The half that needs care is media: Telegram
hands back a dozen different shapes for what a person would call "a voice
message", and the mime type has to be right or the model is handed something
it cannot read, or the API rejects the request outright.

Each format below was checked against the live API with a real message rather
than assumed:

* voice notes arrive as OGG/Opus (``audio/ogg``) -- accepted, transcribed
* video messages, "кружочки", arrive as MP4 (``video/mp4``) -- accepted, described
* photos arrive as JPEG
* music files arrive as MP4 or whatever the uploader chose

Two rules run through this module. **Unknown media is skipped and reported, never
guessed at** -- a wrong mime type produces a confusing model error about a file
the user never mentioned. And **the budget is spent newest-first**: when the cap
is reached, the oldest attachments are dropped and the summary says so, because a
summary that silently ignores half a conversation is worse than one that admits
it.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Telegram shapes mapped to the mime type the model accepts.
#:
#: ``voice`` is an OGG container holding Opus -- the mime has to say ``audio/ogg``
#: even though the codec inside is not Ogg Vorbis, and that was verified by
#: sending a real voice note.
VOICE_MIME = "audio/ogg"
VIDEO_NOTE_MIME = "video/mp4"
PHOTO_MIME = "image/jpeg"

#: Extensions the API is known to accept, for the document cases where Telegram
#: does not tell us what it is.
DOCUMENT_MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mp3": "audio/mp3",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

#: Kinds, in the order they are reported to the user.
KINDS = ("голосовое", "кружок", "фото", "аудио", "видео", "документ")


@dataclass(slots=True)
class MediaItem:
    """One attachment, ready to be turned into an inline part."""

    message_id: int
    kind: str
    mime: str
    path: Path
    size: int = 0

    @property
    def label(self) -> str:
        return f"{self.kind} ({self.size // 1024} КБ)"


@dataclass(slots=True)
class SkippedMedia:
    """Something the user may have expected to be summarised."""

    message_id: int
    reason: str

    def __str__(self) -> str:
        return f"сообщение #{self.message_id}: {self.reason}"


@dataclass(slots=True)
class Collected:
    """A chat, read and reduced to what will actually be sent."""

    transcript: str = ""
    media: list[MediaItem] = field(default_factory=list)
    skipped: list[SkippedMedia] = field(default_factory=list)
    message_count: int = 0
    truncated_transcript: bool = False
    #: Catches that are not tied to one message, such as media being off.
    notes: list[str] = field(default_factory=list)

    @property
    def total_media_bytes(self) -> int:
        return sum(item.size for item in self.media)

    def notices(self) -> list[str]:
        """What the model did not get, in the user's terms.

        Returned even when empty, so the caller can show the same shape of reply
        every time rather than adding a caveat only when something went wrong.
        """
        lines: list[str] = list(self.notes)
        if self.truncated_transcript:
            lines.append("текст обрезан — не поместился целиком")
        if self.skipped:
            reasons: dict[str, int] = {}
            for item in self.skipped:
                reasons[item.reason] = reasons.get(item.reason, 0) + 1
            for reason, count in sorted(reasons.items()):
                suffix = "" if count == 1 else f" ({count} шт.)"
                lines.append(f"{reason}{suffix}")
        return lines


def _extension_of(message: Any) -> str:
    document = getattr(message, "file", None)
    name = getattr(document, "name", None) if document is not None else None
    if not isinstance(name, str) or not name:
        return ""
    suffix = Path(name).suffix.lower()
    if suffix:
        return suffix
    mime = getattr(document, "mime_type", None)
    if isinstance(mime, str):
        return mimetypes.guess_extension(mime) or ""
    return ""


def classify(message: Any) -> str | None:
    """What kind of attachment this message carries, or ``None``.

    Ordered by specificity, because Telegram will happily put several of these on
    one message and a voice note arrives as both ``voice`` and a document.
    """
    if getattr(message, "voice", None) is not None:
        return "голосовое"
    if getattr(message, "video_note", None) is not None:
        return "кружок"
    if getattr(message, "photo", None) is not None:
        return "фото"
    if getattr(message, "audio", None) is not None:
        return "аудио"
    if getattr(message, "video", None) is not None:
        return "видео"
    if getattr(message, "document", None) is not None:
        return "документ"
    return None


def mime_for(message: Any, kind: str) -> str | None:
    """The mime type to declare, or ``None`` when it cannot be determined.

    Returning ``None`` on purpose: a guessed mime type produces a model error
    about a file the user never mentioned, which is harder to act on than the
    plugin saying plainly that it skipped one attachment.
    """
    if kind == "голосовое":
        return VOICE_MIME
    if kind == "кружок":
        return VIDEO_NOTE_MIME
    if kind == "фото":
        return PHOTO_MIME
    suffix = _extension_of(message)
    if suffix in DOCUMENT_MIME:
        return DOCUMENT_MIME[suffix]
    document = getattr(message, "file", None)
    mime = getattr(document, "mime_type", None) if document is not None else None
    if isinstance(mime, str) and mime.startswith(("audio/", "video/", "image/")):
        return mime
    return None


def build_transcript(entries: list[tuple[Any, str]], max_chars: int) -> tuple[str, bool]:
    """Render messages oldest-first, with an author and a timestamp.

    Oldest first on purpose: a model reads a conversation in order, and the tail
    is usually the point. Over the limit the *oldest* lines go, because the
    recent turns are what the user is asking about.
    """
    lines: list[str] = []
    for message, text in entries:
        author = _author_of(message)
        stamp = _stamp_of(message)
        prefix = f"{stamp} {author}: " if stamp and author else f"{author or '?'}: "
        lines.append(f"{prefix}{text}".strip())
    full = "\n".join(lines)
    if len(full) <= max_chars:
        return full, False
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        # 2 for the newline the join will add, so the last line has room.
        if total + len(line) + 2 > max_chars:
            break
        kept.append(line)
        total += len(line) + 2
    kept.reverse()
    return "\n".join(kept), True


def _author_of(message: Any) -> str:
    """The best available name for whoever sent this.

    People are named before channels, because a summary of who said what is the
    whole point, and a channel's username is a poor stand-in for a person.
    """
    sender = getattr(message, "sender", None)
    if sender is None:
        return "?"

    def text(*names: str) -> str:
        parts = [
            value
            for value in (getattr(sender, name, None) for name in names)
            if isinstance(value, str) and value.strip()
        ]
        return " ".join(dict.fromkeys(parts))

    person = text("first_name", "last_name")
    if person:
        return person
    for attribute in ("title", "username", "phone"):
        value = getattr(sender, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    user_id = getattr(sender, "id", None)
    return str(user_id) if user_id is not None else "?"


def _stamp_of(message: Any) -> str:
    import datetime

    date = getattr(message, "date", None)
    if not isinstance(date, datetime.datetime):
        return ""
    return date.strftime("%d.%m %H:%M")


def take_media(
    collected: Collected,
    downloaded: list[tuple[Any, MediaItem]],
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> None:
    """Add attachments newest-first until the budget runs out.

    Skipped ones are recorded with a reason, which is what the reply shows.
    """
    budget = max_total_bytes - collected.total_media_bytes
    for _message, item in downloaded:
        if item.size > max_file_bytes:
            collected.skipped.append(SkippedMedia(item.message_id, f"{item.kind} больше лимита"))
            continue
        if item.size > budget:
            collected.skipped.append(
                SkippedMedia(item.message_id, f"{item.kind} не влез в лимит на медиа")
            )
            continue
        budget -= item.size
        collected.media.append(item)
