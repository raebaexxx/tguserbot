"""Replying in the right forum topic.

Telegram forums put a thread on the message's reply header. ``telethon`` 1.45
reads it and then throws it away: ``send_message`` and ``send_file`` both build
``InputReplyToMessage(reply_to)`` from a single integer, so ``top_msg_id`` never
reaches Telegram and every reply lands in the main thread. There is no public
Telethon call that targets a topic, which is why this exists.

Two rules shape it:

* **Only the topic path is new.** Outside a topic, ``respond`` is whatever it
  always was. And any keyword argument not handled here -- ``silent``, buttons,
  ``schedule``, ``comment_to`` -- falls through to Telethon untouched, because a
  userbot that quietly drops ``silent=True`` is worse than one that answers in
  the wrong thread.
* **Text is sent as plain text.** The bot's replies are already clipped plain
  strings, and reproducing Telethon's parse-mode handling here would be a second
  implementation to keep in step with the first.

The wrapper is installed at the dispatcher, which is the single place every
plugin's reply passes through, so plugins get correct behaviour without knowing
topics exist.
"""

from __future__ import annotations

import logging
from typing import Any

from telethon.tl import functions, types

logger = logging.getLogger("userbot.topics")

#: Keyword arguments ``respond_in_topic`` handles itself. Anything outside this
#: set is Telethon's, and is passed straight through.
_OWN_KEYWORDS = frozenset({"file"})


def _is_plain_text(text: str | None, kwargs: dict[str, Any]) -> bool:
    """Whether this call is one the topic path fully understands.

    Strict on purpose. An earlier version routed a ``file=`` call down the text
    path because ``file`` was a recognised keyword, and the video was silently
    replaced by its caption. Anything not exactly a text message goes to
    Telethon, where a wrong thread is a visible annoyance and a dropped file is
    not.
    """
    return text is not None and not kwargs


def topic_of(event: Any) -> int | None:
    """The forum topic an event belongs to, or ``None`` outside a topic.

    Telegram describes a topic in two ways, and reading only the first one makes
    a fresh message in a topic look like it has no topic at all.

    From a live forum, the ``/ub version`` that was reported going astray::

        reply_to={..., 'forum_topic': True, 'reply_to_msg_id': 60,
                  'reply_to_top_id': None, ...}

    ``reply_to_top_id`` is set only on a message that is itself a *reply*. A
    message posted fresh in a topic instead points at the topic's root message
    and flags itself with ``forum_topic``. So:

    * ``reply_to_top_id`` when present -- the explicit field, and a reply inside a
      topic carries it;
    * otherwise ``reply_to_msg_id``, but only when ``forum_topic`` says so.

    The flag is what makes the fallback safe. Without it, ``reply_to_msg_id`` is
    just "the message being replied to", and treating that as a topic would put
    every ordinary reply in a plain group into a made-up thread.

    Topic 1 is "General" and is returned like any other: a message in General
    still belongs to a specific topic, and treating it as "no topic" is how
    replies end up outside the thread the user typed in.
    """
    header = getattr(event, "reply_to", None)
    if header is None:
        return None

    top = getattr(header, "reply_to_top_id", None)
    if top is not None:
        try:
            return int(top)
        except (TypeError, ValueError):
            return None

    if not getattr(header, "forum_topic", False):
        return None
    root = getattr(header, "reply_to_msg_id", None)
    if root is None:
        return None
    try:
        value = int(root)
    except (TypeError, ValueError):
        return None
    return value or None


def describe(event: Any) -> str:
    """The decision, in words, for the journal.

    A reply that lands in the wrong thread is invisible from the outside, and
    this bug cost two rounds of guessing because the log said nothing about which
    thread had been chosen. The decision is now always readable.
    """
    topic = topic_of(event)
    if topic is None:
        return "main thread (no topic on the message)"
    return f"topic {topic}"


def reply_spec(event: Any) -> types.InputReplyToMessage | None:
    """The reply specification that keeps a message inside its topic."""
    top = topic_of(event)
    if top is None:
        return None
    message_id = getattr(event, "id", 0) or 0
    return types.InputReplyToMessage(int(message_id), top)


def _client_of(event: Any) -> Any:
    client = getattr(event, "client", None)
    if client is None:
        raise RuntimeError("The event has no Telegram client attached")
    return client


async def respond_in_topic(event: Any, text: str | None = None, **kwargs: Any) -> Any:
    """Reply to an event, staying in its topic when there is one.

    Delegates to the event unchanged when there is no topic, or when the call
    uses something this does not implement.
    """
    kwargs = dict(kwargs)
    unsupported = set(kwargs) - _OWN_KEYWORDS
    if unsupported:
        return await event.respond(text, **kwargs)
    spec = reply_spec(event)
    logger.info("replying in %s", describe(event))
    if spec is None:
        return await event.respond(text, **kwargs)

    file = kwargs.get("file")
    if file is not None and not _is_plain_text(text, kwargs):
        return await _send_file_in_topic(event, text or "", file, spec)
    if not _is_plain_text(text, kwargs):
        return await event.respond(text, **kwargs)

    client = _client_of(event)
    try:
        peer = await client.get_input_entity(event.chat_id)
        request = functions.messages.SendMessageRequest(
            peer=peer,
            message=text,
            entities=None,
            no_webpage=False,
            silent=False,
            background=False,
            clear_draft=False,
            reply_to=spec,
        )
        return await client(request)
    except Exception as exc:
        # Falling back beats losing the reply. A plain respond() puts it in the
        # main thread, which is wrong but visible; an exception here is neither
        # delivered nor logged anywhere the user can see.
        logger.warning("topic reply failed, falling back to a plain reply: %s", exc)
        return await event.respond(text, **kwargs)


async def _send_file_in_topic(
    event: Any, caption: str, file: Any, spec: types.InputReplyToMessage
) -> Any:
    """Upload a file and send it into the topic.

    ``send_file`` cannot do this -- it collapses ``reply_to`` to a single integer
    like ``send_message`` does -- so the upload and the request are built here.
    Telethon's own media detection is reused rather than reimplemented; the
    tests assert those internals still exist, because a version bump that removes
    them must fail loudly instead of quietly dropping every file reply.
    """
    client = _client_of(event)
    try:
        peer = await client.get_input_entity(event.chat_id)
        uploaded = await client.upload_file(file)
        media = await client._file_to_media(uploaded)
        request = functions.messages.SendMediaRequest(
            peer=peer,
            media=media,
            message=caption,
            silent=False,
            background=False,
            clear_draft=False,
            reply_to=spec,
        )
        return await client(request)
    except Exception as exc:
        logger.warning("topic file reply failed, falling back to a plain reply: %s", exc)
        return await event.respond(caption or None, file=file)


class TopicAwareEvent:
    """An event whose ``respond`` keeps the reply inside the same topic.

    Delegates every other attribute to the real event, so a plugin that uses
    ``event.id``, ``event.edit_text()`` or ``event.delete()`` is unaffected. The
    wrapper exists at the dispatcher rather than inside each plugin because this
    is the one place every reply passes through.
    """

    __slots__ = ("_event",)

    def __init__(self, event: Any) -> None:
        object.__setattr__(self, "_event", event)

    @property
    def underlying(self) -> Any:
        """The wrapped event, for code that needs the real object.

        Plugins sometimes check types or reach into Telethon, and hiding that the
        wrapper exists would only move the failure somewhere more confusing.
        """
        return self._event

    async def respond(self, text: str | None = None, **kwargs: Any) -> Any:
        return await respond_in_topic(self._event, text, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._event, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._event, name, value)

    def __repr__(self) -> str:
        return f"TopicAwareEvent({self._event!r})"


async def wrap_for_topics(event: Any) -> Any:
    """Wrap an event so replies stay in their topic. Idempotent."""
    if isinstance(event, TopicAwareEvent):
        return event
    return TopicAwareEvent(event)
