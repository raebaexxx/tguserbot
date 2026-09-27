"""The prompt, and the shape of the request.

Kept apart from the command handler so the wording can be read on its own. The
instructions matter more than usual here: the model is handed a transcript plus a
pile of attachments with no way to tell which attachment goes with which line, so
it has to be told how to treat them.
"""

from __future__ import annotations

from userbot.gemini import Part

SUMMARY_SYSTEM = """\
You summarise conversations. You are given the recent messages of one chat as a
transcript, followed by voice notes, video messages, photos and audio files that
were attached to it.

The transcript looks like this:

    27.09 14:31 Anna: привет
    27.09 14:32 Ivan: (кружок)

Read it in order, oldest first. Notes in parentheses stand for an attachment: the
text was empty, so the media below is what was said instead. Several attachments
may belong to the same message, and you cannot tell which; say so rather than
inventing a pairing.

Write the summary in the language the conversation is in. Structure it as:

* one opening line: what this stretch of conversation is about;
* three to eight bullet points, each a fact someone actually said, not a guess
  about tone or intent;
* a final line naming anything you could not make out — an inaudible voice note,
  media you could not read, or a message that was cut off.

Do not invent names, decisions or numbers that are not in the transcript or the
media. If the conversation is trivial, say so in one line instead of padding.
Skip greetings, stickers and service notices unless they carry meaning.

Keep it under about 200 words. Plain text, no Markdown headers or tables."""


def build_request_parts(
    transcript: str, media_payloads: list[Part], notices: list[str]
) -> list[Part]:
    """Assemble the parts of one user turn.

    Transcript first, then the attachments, then the instructions: the model
    reads them in the order they appear, and referring to "the media below" only
    works if the media really is below.
    """
    parts: list[Part] = [Part(text=f"Транскрипт:\n{transcript}")]
    if media_payloads:
        parts.append(
            Part(text=(f"Вложения ({len(media_payloads)} шт.), в порядке от новых к старым:"))
        )
        parts.extend(media_payloads)
    if notices:
        lines = "\n".join(f"- {note}" for note in notices)
        parts.append(
            Part(text=(f"Что модель не получила, упомяни это в конце, если это важно:\n{lines}"))
        )
    parts.append(Part(text="Сделай сводку."))
    return parts
