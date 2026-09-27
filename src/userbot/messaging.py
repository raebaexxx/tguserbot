"""Editing a message, whichever library is underneath.

Telethon 1.45's ``Message`` has ``edit`` and no ``edit_text``. Pyrogram's has
``edit_text`` and no ``edit``. Both names are the obvious one to reach for, and
writing the wrong one costs a feature rather than raising: the ai plugin called
``edit_text`` on a real message, every progress edit hit ``AttributeError``,
and streaming quietly did nothing in production until somebody looked at the
journal and found ``'Message' object has no attribute 'edit_text'``.

So the name lives here, once, instead of in every plugin that will hit it.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger("userbot.messaging")

#: The names to try, in order. ``edit_text`` first: it takes only the text and
#: cannot be confused with an edit that expects an entity list.
EDIT_METHODS = ("edit_text", "edit")


def can_edit(event: Any) -> bool:
    """Whether this object exposes any way to edit itself."""
    return any(callable(getattr(event, name, None)) for name in EDIT_METHODS)


async def edit_message(event: Any, text: str, **kwargs: Any) -> bool:
    """Edit a message, reporting whether it worked.

    Returns ``False`` rather than raising. Both failure modes are routine and
    neither is worth aborting over: the object may not be a message that can be
    edited, or Telegram may be refusing the edit because the message is gone or
    the account is throttled. A caller in the middle of a stream has already
    chosen how to deliver its answer.
    """
    if event is None:
        return False
    for name in EDIT_METHODS:
        method = getattr(event, name, None)
        if not callable(method):
            continue
        try:
            result = method(text, **kwargs)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            logger.debug("could not edit the message via %s: %s", name, exc)
            return False
    return False


async def delete_message(event: Any) -> bool:
    """Delete a message if it can be deleted. Never raises."""
    if event is None:
        return False
    method = getattr(event, "delete", None)
    if not callable(method):
        return False
    try:
        result = method()
        if inspect.isawaitable(result):
            await result
        return True
    except Exception as exc:
        logger.debug("could not delete the message: %s", exc)
        return False


__all__ = ["EDIT_METHODS", "can_edit", "delete_message", "edit_message"]
