"""The prompts, kept in one place so the plugin reads as orchestration.

Video summarising gets a real prompt rather than "summarise this": the model is
handed a transcript *and* frame descriptions, and it is told which parts exist.
A model that is not told the transcript is missing writes a summary of the two
lines it got as if they were everything.
"""

from __future__ import annotations

CHAT_SYSTEM = (
    "Ты — дружелюбный и точный ассистент. Отвечай на языке пользователя, "
    "кратко и по существу, без воды. Если не знаешь — скажи прямо. "
    "Не выдумывай факты и не ссылайся на источники, которых у тебя нет."
)

FRAME_PROMPT = (
    "Опиши, что происходит в этом кадре видео, одним-двумя предложениями. Только то, что видно."
)

FRAME_PROMPT_QUESTION = "Ответь на вопрос по этому изображению кратко и по делу."

VIDEO_MERGE_SYSTEM = (
    "Ты собираешь краткий пересказ видео по данным, которые приложены ниже. "
    "Если данных о речи нет — пересказывай по кадрам, и наоборот. "
    "Не упоминай, что тебе что-то передали: просто пересказывай видео. "
    "Формат: 2–5 предложений связного текста на языке пользователя."
)

PRIVACY_NOTICE = (
    "\n\n_Медиа отправлено на публичный сторонний API — это бесплатный ключ из открытого канала._"
)


def video_merge_message(
    *,
    transcript: str | None,
    frame_notes: list[tuple[float, str]],
    focus: str | None,
) -> str:
    parts = ["Данные о видео:"]
    if transcript:
        parts.append(f"\nРасшифровка речи:\n{transcript}")
    else:
        parts.append("\nРасшифровки речи нет (звука нет или распознавание недоступно).")
    if frame_notes:
        lines = [f"— {moment:.0f} c: {text}" for moment, text in frame_notes]
        parts.append("\nКадры по времени:\n" + "\n".join(lines))
    else:
        parts.append("\nОписаний кадров нет (модель зрения недоступна).")
    if focus:
        parts.append(f"\nОтдельно обрати внимание на: {focus}")
    return "\n".join(parts)


__all__ = [
    "CHAT_SYSTEM",
    "FRAME_PROMPT",
    "FRAME_PROMPT_QUESTION",
    "PRIVACY_NOTICE",
    "VIDEO_MERGE_SYSTEM",
    "video_merge_message",
]
