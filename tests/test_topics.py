"""Tests for replying in the right forum topic.

The live bug: every ``/ub ...`` command answered in the main thread, even when
written inside a topic. Telegram forums carry the thread on the reply header, and
``telethon`` 1.45 drops it: both ``send_message`` and ``send_file`` build
``InputReplyToMessage(reply_to)`` from a single integer, so ``top_msg_id`` never
reaches Telegram and the message lands in the main topic.

The tests here are mostly about *not* breaking things. Any keyword argument this
does not understand must fall through to the original call untouched, because a
userbot that silently drops ``silent=True`` or a button is worse than one that
answers in the wrong thread.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from telethon.tl import types

# --- reading the topic off the event ---------------------------------------


class ForumTopicStub:
    """Only the fact that a topic exists matters; nothing reads its fields."""


class ReplyHeader:
    def __init__(self, top: int | None, forum: bool = True) -> None:
        self.reply_to_top_id = top
        self.forum_topic = ForumTopicStub() if forum else None


class FakeEvent:
    """Just the attributes topic detection reads, plus recording of sends."""

    def __init__(self, top: int | None, message_id: int = 42, chat: Any = None) -> None:
        self.id = message_id
        self.chat_id = 100
        self.reply_to = ReplyHeader(top) if top is not None else None
        self.client = chat or FakeClient()
        self.sender_id = 1
        self.raw_text = "raw"
        self.out = True
        self.respond_calls: list[tuple[Any, dict[str, Any]]] = []
        self.raw_calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def respond(self, text: str | None = None, **kwargs: Any) -> Any:
        self.respond_calls.append((text, kwargs))
        return "responded"


class FakeClient:
    """A client double with telethon's *real* ``_file_to_media`` contract.

    ``_file_to_media`` returns ``(file_handle, media, as_image)``. The double used
    to return the media alone, which is what let the defect this file guards
    against ship: the production code took the whole tuple as the media, the
    request failed to serialise, and the file fell back to the main thread. The
    tuple is returned here so the suite fails if the unpacking is ever dropped.
    """

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.peer = types.InputPeerChat(100)
        self.parse_mode = None
        #: Set when a request is built with something telethon cannot serialise.
        #: Serialising is the check: it is what fails on the live path, and it
        #: needs no network.
        self.serialise_requests = True
        self._sent_id = 4242

    async def get_input_entity(self, _entity: Any) -> Any:
        return self.peer

    async def _parse_message_text(self, text: str, _mode: Any) -> tuple[str, Any]:
        return text, None

    async def upload_file(self, file: Any, **kwargs: Any) -> Any:
        return types.InputFile(id=1, parts=0, name="video.mp4", md5_checksum="")

    async def _file_to_media(self, file: Any, **kwargs: Any) -> Any:
        # Telethon's own helper uploads and converts in one call, taking the
        # original file; the double does the same so the code under test passes
        # the same thing to both.
        handle = await self.upload_file(file, **kwargs)
        media = types.InputMediaUploadedDocument(file=handle, mime_type="video/mp4", attributes=[])
        return handle, media, False

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        if self.serialise_requests:
            # Raises "Cannot cast tuple to any kind of InputMedia" for a bad media.
            request._bytes()
        # What a real request returns: Updates, not a Message. Telethon's own
        # send_message narrows it with _get_response_message, and a double that
        # returned a Message here would hide a caller getting Updates.
        return types.UpdateShortSentMessage(
            id=self._sent_id,
            pts=1,
            pts_count=1,
            date=None,
            out=True,
        )

    def _get_response_message(self, request: Any, result: Any, input_chat: Any) -> Any:
        """Telethon's own narrowing, so the double reports what it really is."""
        if isinstance(result, types.UpdateShortSentMessage):
            message = result.message
            message._finish_init(None, {}, None)
            return message
        return None


@pytest.fixture(scope="module")
def topics() -> Any:
    from userbot import topics as module

    return module


# --- detection --------------------------------------------------------------


def test_a_topic_is_read_off_the_reply_header(topics: Any) -> None:
    assert topics.topic_of(FakeEvent(top=77)) == 77


def test_a_chat_without_topics_has_none(topics: Any) -> None:
    assert topics.topic_of(FakeEvent(top=None)) is None


def test_the_general_topic_is_a_real_topic(topics: Any) -> None:
    """Topic id 1 is "General". Replying there is still a specific topic."""
    assert topics.topic_of(FakeEvent(top=1)) == 1


def test_an_event_with_no_reply_header_at_all(topics: Any) -> None:
    event = FakeEvent(top=None)
    event.reply_to = None
    assert topics.topic_of(event) is None


def test_the_reply_spec_carries_the_topic(topics: Any) -> None:
    spec = topics.reply_spec(FakeEvent(top=77, message_id=42))
    assert isinstance(spec, types.InputReplyToMessage)
    assert spec.reply_to_msg_id == 42
    assert spec.top_msg_id == 77


def test_no_spec_outside_a_topic(topics: Any) -> None:
    assert topics.reply_spec(FakeEvent(top=None)) is None


# --- sending text into a topic ---------------------------------------------


async def test_text_lands_in_the_topic(topics: Any) -> None:
    event = FakeEvent(top=77)
    await topics.respond_in_topic(event, "привет")
    assert len(event.client.requests) == 1
    request = event.client.requests[0]
    assert isinstance(request.reply_to, types.InputReplyToMessage)
    assert request.reply_to.top_msg_id == 77
    assert request.message == "привет"
    assert event.respond_calls == [], "the topic path must not also call respond"


async def test_a_topic_reply_hands_back_a_message_not_updates(topics: Any) -> None:
    """``client(request)`` returns ``Updates``; callers expect a ``Message``.

    Telethon converts that for its own ``send_message`` by calling
    ``_get_response_message``, which picks the new message out of the updates by
    matching the request's ``random_id``. The topic path called the client
    directly and returned the raw result, so inside a topic ``respond()`` returned
    an object that has no ``edit_text`` and no ``delete``: the ``ai`` plugin's
    streaming placeholder could not be edited or removed there, and only inside a
    topic. That is the live symptom this defect produced.
    """
    event = FakeEvent(top=77)
    sent = await topics.respond_in_topic(event, "привет")
    assert isinstance(sent, types.Message), (
        f"respond() returned {type(sent).__name__}; a plugin will try to edit or delete it and fail"
    )
    assert sent.id > 0


async def test_a_chat_without_topics_uses_respond_unchanged(topics: Any) -> None:
    """Zero behaviour change where there is no topic to miss."""
    event = FakeEvent(top=None)
    await topics.respond_in_topic(event, "привет")
    assert event.client.requests == []
    assert event.respond_calls == [("привет", {})]


async def test_unsupported_keywords_fall_through_to_telethon(topics: Any) -> None:
    """A userbot that drops silent= or a button is worse than one in the wrong thread."""
    event = FakeEvent(top=77)
    for kwargs in (
        {"silent": True},
        {"buttons": "markup"},
        {"parse_mode": "markdown"},
        {"schedule": "later"},
        {"comment_to": 5},
        {"link_preview": False},
        {"no_webpage": True},
        {"reply_to": 3},
    ):
        event.respond_calls.clear()
        await topics.respond_in_topic(event, "text", **kwargs)
        assert event.respond_calls == [("text", kwargs)], kwargs
        assert event.client.requests == [], kwargs


async def test_a_file_with_unsupported_options_falls_through(topics: Any) -> None:
    """A file plus anything this does not implement goes to Telethon whole.

    Never partly: a call that went down the topic path with an argument silently
    dropped is worse than one in the wrong thread.
    """
    event = FakeEvent(top=77)
    for kwargs in (
        {"file": "/tmp/video.mp4", "silent": True},
        {"file": "/tmp/video.mp4", "buttons": "markup"},
    ):
        event.respond_calls.clear()
        event.client.requests.clear()
        await topics.respond_in_topic(event, "cap", **kwargs)
        assert event.respond_calls == [("cap", kwargs)], kwargs
        assert event.client.requests == [], kwargs


async def test_a_file_outside_a_topic_uses_telethon(topics: Any) -> None:
    event = FakeEvent(top=None)
    await topics.respond_in_topic(event, "cap", file="/tmp/video.mp4")
    assert event.client.requests == []
    assert event.respond_calls == [("cap", {"file": "/tmp/video.mp4"})]


# --- the wrapper -----------------------------------------------------------


async def test_the_wrapper_routes_responses_into_the_topic(topics: Any) -> None:
    event = FakeEvent(top=77)
    wrapped = topics.TopicAwareEvent(event)
    await wrapped.respond("привет")
    assert len(event.client.requests) == 1
    assert event.client.requests[0].reply_to.top_msg_id == 77


async def test_the_wrapper_delegates_everything_else(topics: Any) -> None:
    event = FakeEvent(top=77)
    wrapped = topics.TopicAwareEvent(event)
    assert wrapped.id == 42
    assert wrapped.chat_id == 100
    assert wrapped.sender_id == 1
    assert wrapped.raw_text == "raw"
    assert wrapped.reply_to is event.reply_to


async def test_the_wrapper_forwards_edits_and_deletes(topics: Any) -> None:
    edited: list[str] = []
    deletions = 0

    async def edit_text(self: Any, text: str, **kwargs: Any) -> None:
        edited.append(text)

    async def delete(self: Any) -> None:
        nonlocal deletions
        deletions += 1

    class Editable(FakeEvent):
        pass

    Editable.edit_text = edit_text  # type: ignore[attr-defined]
    Editable.delete = delete  # type: ignore[attr-defined]
    wrapped = topics.TopicAwareEvent(Editable(top=77))
    await wrapped.edit_text("новый текст")
    await wrapped.delete()
    assert edited == ["новый текст"]
    assert deletions == 1


async def test_the_wrapper_keeps_the_underlying_event_reachable(topics: Any) -> None:
    """Code that needs the real event -- isinstance checks, Telethon internals --
    must be able to get it."""
    event = FakeEvent(top=77)
    wrapped = topics.TopicAwareEvent(event)
    assert wrapped.underlying is event


def test_wrapping_a_plain_event_still_works(topics: Any) -> None:
    """Not every event is a Telethon one; the wrapper must not require it."""
    wrapped = topics.TopicAwareEvent(object())
    assert wrapped.underlying is not None


# --- telethon internals this depends on ------------------------------------


def test_the_telethon_internals_used_still_exist() -> None:
    """This module reaches into telethon for the topic path.

    The version is pinned, but a pin is a promise, not a guarantee. If an upgrade
    removes any of these, this fails here rather than at the moment a user asks
    a question in a topic.
    """
    from telethon import TelegramClient

    for name in ("_file_to_media", "upload_file", "get_input_entity"):
        assert hasattr(TelegramClient, name), name


def test_the_reply_type_still_has_top_msg_id() -> None:
    """Without this field there is no way to target a topic at all."""
    import inspect

    parameters = inspect.signature(types.InputReplyToMessage.__init__).parameters
    assert "top_msg_id" in parameters


# --- files ------------------------------------------------------------------
#
# A TikTok video sent from a topic landed in the main thread, because send_file
# cannot target a topic either. Falling through to Telethon was the safe default
# and the wrong answer at the same time, so the file path is implemented here.


async def test_a_file_lands_in_the_topic(topics: Any) -> None:
    event = FakeEvent(top=77)
    await topics.respond_in_topic(event, "caption", file="/tmp/video.mp4")
    assert len(event.client.requests) == 1
    request = event.client.requests[0]
    assert isinstance(request.reply_to, types.InputReplyToMessage)
    assert request.reply_to.top_msg_id == 77
    assert request.message == "caption"
    assert event.respond_calls == []


async def test_a_file_reply_is_built_from_the_real_telethon_media(
    topics: Any, tmp_path: Path
) -> None:
    """The reported bug: a TikTok video answered in the main thread.

    ``TelegramClient._file_to_media`` returns ``(file_handle, media, as_image)``.
    The code took that whole tuple as the ``media`` field, so
    ``SendMediaRequest`` could not serialise it, the call raised, and the
    fallback put the video in the chat's main thread -- exactly the symptom the
    report described, one layer below where it was looked for.

    The assertion is on a real Telethon request built by the real method, not on
    a double shaped to agree with the bug.
    """
    from telethon import TelegramClient

    class RecordingClient(TelegramClient):
        """A real client with only the network replaced.

        Subclassed rather than patched onto an instance: ``client(request)``
        resolves on the type, so an instance attribute would be ignored and the
        call would fail as "disconnected" instead of exercising the request.
        """

        def __init__(self) -> None:
            super().__init__(
                str(Path(__file__).resolve().parent / "no-such-session"),
                api_id=1,
                api_hash="0" * 32,
            )
            self.sent: list[Any] = []

        async def upload_file(self, *_args: Any, **_kwargs: Any) -> Any:
            return types.InputFile(id=1, parts=0, name="video.mp4", md5_checksum="")

        async def get_input_entity(self, _entity: Any) -> Any:
            # Answered here rather than through ``__call__``: resolving a peer is
            # an internal request, and a double that returned the same fake for
            # every request would make the *lookup* fail instead of the media.
            return types.InputPeerChat(100)

        async def __call__(self, request: Any) -> Any:
            self.sent.append(request)
            request._bytes()
            return types.Updates(
                updates=[
                    types.UpdateMessageID(random_id=request.random_id, id=555),
                    types.UpdateNewMessage(
                        message=types.Message(
                            id=555,
                            peer_id=types.InputPeerChat(100),
                            message=request.message,
                            date=None,
                            out=True,
                        ),
                        pts=1,
                        pts_count=1,
                    ),
                ],
                users=[],
                chats=[],
                date=None,
                seq=1,
            )

    real_client = RecordingClient()

    # A real file on disk, because that is what telethon's helper inspects to
    # work out the mime type and the video attributes.
    video = tmp_path / "video.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)

    uploaded = await real_client.upload_file(str(video))
    assert isinstance(await real_client._file_to_media(uploaded), tuple), (
        "telethon's shape changed: _file_to_media must still return a 3-tuple, "
        "and topics.py must unpack it rather than pass it as the media"
    )

    event = FakeEvent(top=77)
    event.client = cast(Any, real_client)

    await topics.respond_in_topic(event, "caption", file=str(video))

    assert event.respond_calls == [], (
        "the topic file path fell back to a plain reply, so the video went to "
        "the main thread: " + repr(event.respond_calls)
    )
    assert len(real_client.sent) == 1
    assert isinstance(real_client.sent[0].media, types.TypeInputMedia)
    assert real_client.sent[0].reply_to.top_msg_id == 77


async def test_a_topic_reply_is_narrowed_from_a_group_updates_result(
    topics: Any, tmp_path: Path
) -> None:
    """A group answers with ``Updates``, not with the short sent-message shape.

    ``UpdateShortSentMessage`` is what a private chat returns. A forum topic does
    not, so the narrowing that the other test exercises by hand is the one that
    actually runs in production -- and it is Telethon's private
    ``_get_response_message``, so it is checked against Telethon's own code rather
    than a copy of it.
    """
    from telethon import TelegramClient

    class GroupClient(TelegramClient):
        def __init__(self) -> None:
            super().__init__(
                str(Path(__file__).resolve().parent / "no-such-session"),
                api_id=1,
                api_hash="0" * 32,
            )

        async def get_input_entity(self, _entity: Any) -> Any:
            return types.InputPeerChat(100)

        async def __call__(self, request: Any) -> Any:
            request._bytes()
            message = types.Message(
                id=777,
                peer_id=types.InputPeerChat(100),
                message=request.message,
                date=None,
                out=True,
                # The server echoes the reply header back, topic included.
                reply_to=types.MessageReplyHeader(
                    reply_to_msg_id=request.reply_to.reply_to_msg_id,
                    reply_to_top_id=request.reply_to.top_msg_id,
                    forum_topic=True,
                ),
            )
            return types.Updates(
                updates=[
                    types.UpdateMessageID(random_id=request.random_id, id=777),
                    types.UpdateNewMessage(message=message, pts=1, pts_count=1),
                ],
                users=[],
                chats=[],
                date=None,
                seq=1,
            )

    event = FakeEvent(top=77)
    event.client = cast(Any, GroupClient())

    sent = await topics.respond_in_topic(event, "привет")

    assert isinstance(sent, types.Message), f"got {type(sent).__name__}"
    assert sent.id == 777, "the new message must be found by its random_id"
    assert sent.reply_to.reply_to_top_id == 77, "the message must carry the topic it went to"


async def test_a_file_without_a_caption_still_works(topics: Any) -> None:
    event = FakeEvent(top=77)
    await topics.respond_in_topic(event, file="/tmp/video.mp4")
    request = event.client.requests[0]
    assert request.message == ""
    assert request.reply_to.top_msg_id == 77


async def test_file_reply_failure_falls_back_to_telethon(topics: Any) -> None:
    """A lost video is worse than one in the wrong thread."""

    class Broken(FakeClient):
        async def upload_file(self, file: Any, **kwargs: Any) -> Any:
            raise RuntimeError("upload failed")

    event = FakeEvent(top=77, chat=Broken())
    await topics.respond_in_topic(event, "caption", file="/tmp/video.mp4")
    assert event.respond_calls == [("caption", {"file": "/tmp/video.mp4"})]


# --- the dispatcher actually wraps the event -------------------------------
#
# The tests above cover the module. This covers the wiring, which is the part
# that makes the fix reach a plugin: the dispatcher wraps the event, so a plugin
# that has never heard of topics replies in the right one. Removing the wrapper
# here would leave every test above still passing.


class DispatcherEvent(FakeEvent):
    """Enough of a Telethon event for the dispatcher to accept it."""

    def __init__(self, top: int | None, raw: str, sender: int = 1) -> None:
        super().__init__(top=top)
        self.raw_text = raw
        self.sender_id = sender
        self.message = None


async def test_a_command_reply_reaches_the_topic(topics: Any) -> None:
    """The reported bug, end to end through the dispatcher."""
    from userbot.commands import CommandDispatcher

    dispatcher = CommandDispatcher({1})
    event = DispatcherEvent(top=77, raw="/ub version")
    dispatcher.register("version", _reply_with_command_response)

    await dispatcher.handle_event(event)

    assert len(event.client.requests) == 1, event.respond_calls
    assert event.client.requests[0].reply_to.top_msg_id == 77
    assert event.respond_calls == []


async def test_an_unknown_command_also_reaches_the_topic(topics: Any) -> None:
    """The early replies answer before the command is even resolved.

    They are still replies, and before the wrapper was applied to the whole
    handler they went to the main thread -- so a typo in a topic produced a
    "Неизвестная команда" in the wrong place.
    """
    from userbot.commands import CommandDispatcher

    dispatcher = CommandDispatcher({1})
    event = DispatcherEvent(top=77, raw="/ub nonsense")

    await dispatcher.handle_event(event)

    assert len(event.client.requests) == 1, event.respond_calls
    assert event.client.requests[0].reply_to.top_msg_id == 77
    assert "Неизвестная" in event.client.requests[0].message


async def test_a_cooldown_notice_reaches_the_topic(topics: Any) -> None:
    from userbot.commands import CommandDispatcher

    dispatcher = CommandDispatcher({1}, cooldown=60.0)
    event = DispatcherEvent(top=77, raw="/ub version")
    dispatcher.register("version", _reply_with_command_response)
    dispatcher._claim_cooldown = lambda _key: False  # type: ignore[assignment]

    await dispatcher.handle_event(event)

    assert len(event.client.requests) == 1, event.respond_calls
    assert event.client.requests[0].reply_to.top_msg_id == 77
    assert "Слишком часто" in event.client.requests[0].message


async def test_outside_a_topic_nothing_changes(topics: Any) -> None:
    from userbot.commands import CommandDispatcher

    dispatcher = CommandDispatcher({1})
    event = DispatcherEvent(top=None, raw="/ub version")
    dispatcher.register("version", _reply_with_command_response)

    await dispatcher.handle_event(event)

    assert event.client.requests == []
    assert event.respond_calls, "outside a topic, respond() must be used as before"


async def test_a_plugin_can_still_reach_the_real_event(topics: Any) -> None:
    """Hiding the wrapper would only move the failure somewhere less obvious."""
    from userbot.commands import CommandDispatcher

    seen: list[Any] = []

    async def callback(command: Any) -> None:
        seen.append(command.event.underlying)
        await command.respond("ok")

    dispatcher = CommandDispatcher({1})
    event = DispatcherEvent(top=77, raw="/ub version")
    dispatcher.register("version", callback)

    await dispatcher.handle_event(event)
    assert seen == [event]


async def _reply_with_command_response(command: Any) -> None:
    await command.respond("version 0.2.0")


# --- plugin handlers get the same treatment --------------------------------
#
# The dispatcher wrapper covers /ub commands. A plugin that answers from
# ctx.register_handler receives the raw Telethon event instead, and the echo
# plugin's "pong" was a live example: it replied in the main thread from inside a
# topic.


async def test_a_handler_reply_reaches_the_topic(topics: Any) -> None:
    from userbot.health import HealthService
    from userbot.plugin_api import PluginContext
    from userbot.rate_limit import RateLimiter

    event = FakeEvent(top=77)
    seen: list[str] = []

    async def callback(handled: Any) -> None:
        seen.append(handled.id)
        await handled.respond("pong")

    from conftest import FakeClient

    class Client(FakeClient):
        pass

    context = PluginContext(
        plugin_name="t",
        plugin_path=Path("."),
        client=Client(),
        settings=SimpleNamespace(),
        storage=cast(Any, None),
        dispatcher=SimpleDispatcher(),
        rate_limiter=RateLimiter(min_interval=0),
        health=HealthService(),
        manager=cast(Any, None),
        instance=cast(Any, None),
        logger=logging.getLogger("test.topics"),
    )
    context.register_handler(callback, object())
    await context._handlers[0].guarded(event)

    assert seen == [42]
    assert len(event.client.requests) == 1
    assert event.client.requests[0].reply_to.top_msg_id == 77


class SimpleDispatcher:
    owner_ids: set[int] = {1}

    def register(self, *args: Any, **kwargs: Any) -> None:
        pass

    def unregister(self, *args: Any, **kwargs: Any) -> None:
        pass

    def commands(self) -> list[Any]:
        return []

    def is_owner(self, sender_id: Any) -> bool:
        return sender_id in self.owner_ids


# --- what Telegram actually sends ------------------------------------------
#
# Observed on a real forum, not guessed:
#
#   id=1085586  reply_to=None                            'Как вы?'    <- typed in a topic
#   id=1085583  reply_to=MessageReplyHeader top_msg_id=1085403          <- reply in a topic
#
# A message posted fresh inside a topic arrives with no topic marker at all.
# The topic id exists on the message only when the message is itself a reply.
# That is a property of the API, and it caps what any bot can promise: with
# nothing in the update, there is no topic to answer in.


def test_a_fresh_message_in_a_topic_carries_no_marker(topics: Any) -> None:
    assert topics.topic_of(FakeEvent(top=None)) is None


def test_a_reply_in_a_topic_carries_the_marker(topics: Any) -> None:
    spec = topics.reply_spec(FakeEvent(top=1085403, message_id=1085583))
    assert spec is not None
    assert spec.top_msg_id == 1085403
    assert spec.reply_to_msg_id == 1085583


def test_the_decision_is_reported_for_the_journal(topics: Any) -> None:
    """A silently wrong thread is what made this hard to diagnose twice.

    The decision -- which topic, or that Telegram sent none -- has to be
    readable in the journal, or the next report is another round of guessing.
    """
    assert topics.describe(FakeEvent(top=77, message_id=42)) == "topic 77"
    assert topics.describe(FakeEvent(top=None)) == "main thread (no topic on the message)"


async def test_the_decision_is_logged_on_every_reply(topics: Any) -> None:
    """The log line is the only evidence of which thread was chosen.

    A handler is attached to the logger itself rather than using ``caplog``:
    the project's logging setup stops the ``userbot`` loggers propagating, so a
    test that depends on propagation passes alone and fails in a full run. That
    is a trap worth avoiding rather than working around quietly.
    """
    import logging

    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("userbot.topics")
    handler = Capture()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        await topics.respond_in_topic(FakeEvent(top=77), "привет")
        assert any("topic 77" in r.getMessage() for r in records), [r.getMessage() for r in records]
        records.clear()
        await topics.respond_in_topic(FakeEvent(top=None), "привет")
        assert any("no topic" in r.getMessage() for r in records), [r.getMessage() for r in records]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


async def test_a_fresh_topic_message_gets_a_plain_reply(topics: Any) -> None:
    """With no marker, the honest outcome is a reply in the chat's thread.

    Guessing a topic from recency would be worse: an answer in the wrong thread
    is harder to notice than one in the thread the chat is showing.
    """
    event = FakeEvent(top=None)
    await topics.respond_in_topic(event, "привет")
    assert event.client.requests == []
    assert event.respond_calls == [("привет", {})]


# --- the real shape, from a live forum --------------------------------------
#
# Read off the actual chat the bug was reported in (Fuxilover's Warehouse,
# 3738198858), message 40887 -- the `/ub version` that went astray:
#
#   reply_to={..., 'forum_topic': True, 'reply_to_msg_id': 60,
#             'reply_to_top_id': None, ...}
#
# A message posted fresh in a topic has a reply header after all. What it lacks
# is `reply_to_top_id`: the topic is the *root* the header points at instead,
# flagged by `forum_topic`. Reading only `top_msg_id` -- as the first attempt
# did -- found nothing and concluded the topic was unknowable. It was on the
# message the whole time.
#
# A reply inside a topic carries both, and they agree:
#
#   id=40892  'да'  reply_to_msg_id=40885  reply_to_top_id=60
#
# And an ordinary reply in a plain group must not be mistaken for a topic:
#
#   forum_topic=False  reply_to_msg_id=42  reply_to_top_id=None


class RealHeader:
    """A MessageReplyHeader as Telethon actually fills it in."""

    def __init__(
        self,
        *,
        forum_topic: bool = False,
        reply_to_msg_id: int = 0,
        reply_to_top_id: int | None = None,
    ) -> None:
        self.forum_topic = forum_topic
        self.reply_to_msg_id = reply_to_msg_id
        self.reply_to_top_id = reply_to_top_id


class WithHeader(FakeEvent):
    def __init__(self, header: Any, message_id: int = 40887) -> None:
        super().__init__(top=None, message_id=message_id)
        self.reply_to = header
        self.message = None


def test_a_fresh_message_in_a_topic_is_found_by_its_root(topics: Any) -> None:
    """The reported bug, with the message the report was about."""
    event = WithHeader(RealHeader(forum_topic=True, reply_to_msg_id=60, reply_to_top_id=None))
    assert topics.topic_of(event) == 60
    spec = topics.reply_spec(event)
    assert spec is not None
    assert spec.top_msg_id == 60
    assert spec.reply_to_msg_id == 40887, "it must still reply to the command message"


def test_a_reply_in_a_topic_uses_top_msg_id(topics: Any) -> None:
    event = WithHeader(RealHeader(forum_topic=True, reply_to_msg_id=40885, reply_to_top_id=60))
    assert topics.topic_of(event) == 60


def test_a_plain_group_reply_is_not_a_topic(topics: Any) -> None:
    """reply_to_msg_id alone must never be read as a topic."""
    event = WithHeader(RealHeader(forum_topic=False, reply_to_msg_id=42, reply_to_top_id=None))
    assert topics.topic_of(event) is None
    assert topics.reply_spec(event) is None


def test_a_header_without_the_flag_is_not_a_topic(topics: Any) -> None:
    """Old servers may omit forum_topic entirely; absence is not a yes."""

    class Bare:
        reply_to_msg_id = 60
        reply_to_top_id = None

    assert topics.topic_of(WithHeader(Bare())) is None


def test_top_msg_id_wins_over_the_root(topics: Any) -> None:
    """When both are present they agree, and top_msg_id is the documented field."""
    event = WithHeader(RealHeader(forum_topic=True, reply_to_msg_id=40885, reply_to_top_id=60))
    assert topics.topic_of(event) == 60


def test_the_general_topic_is_not_mistaken_for_a_root(topics: Any) -> None:
    """Topic 1 is General; a reply to message 1 outside a topic means nothing."""
    event = WithHeader(RealHeader(forum_topic=False, reply_to_msg_id=1, reply_to_top_id=None))
    assert topics.topic_of(event) is None


async def test_the_reported_command_replies_into_its_topic(topics: Any) -> None:
    """The whole fix, end to end on the message from the bug report."""
    from userbot.commands import CommandDispatcher

    dispatcher = CommandDispatcher({1})
    event = WithHeader(RealHeader(forum_topic=True, reply_to_msg_id=60, reply_to_top_id=None))
    event.raw_text = "/ub version"
    event.sender_id = 1
    event.message = None
    dispatcher.register("version", _reply_with_command_response)

    await dispatcher.handle_event(event)

    assert len(event.client.requests) == 1, event.respond_calls
    assert event.client.requests[0].reply_to.top_msg_id == 60
    assert event.respond_calls == []


def test_the_decision_names_the_discovered_topic(topics: Any) -> None:
    event = WithHeader(RealHeader(forum_topic=True, reply_to_msg_id=60, reply_to_top_id=None))
    assert topics.describe(event) == "topic 60"
