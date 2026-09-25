"""Обогащение по кнопке: выжимка, цитата, оценка.

Модель здесь не решает, что показать человеку, — она отвечает на уже
показанное. Сообщение ушло раньше по заголовку `[NEWS-003]`, кнопки висят под
ним, и пока никто не нажал, ни один вызов не сделан `[NEWS-002]`.

Почему именно три кнопки и почему разные.

- **Выжимка** — три пункта по тексту. Нужна тем, кто читает ленту с телефона
  и решает, открывать ли оригинал.
- **Цитата** — дословно из материала, без пересказа. Это единственная кнопка,
  чей результат можно проверить глазами: он либо есть в тексте, либо нет.
- **Оценка** — что в новости нового и чего в ней не сказано. Самое полезное и
  самое ненадёжное, поэтому подписано как мнение модели `[NEWS-008]`.

Ответ подписан именем модели и ссылкой на оригинал: пересказ не заменяет
издание `[NEWS-007]`.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from . import article, model, store

log = logging.getLogger("fpnews.enrich")

# Коды в кнопках короткие: телеграм даёт под callback_data 64 байта, а
# кириллица в utf-8 занимает по два.
KINDS = {
    "s": "выжимка",
    "q": "цитата",
    "w": "оценка",
}
CODES = {name: code for code, name in KINDS.items()}
BUTTON = {"выжимка": "Выжимка", "цитата": "Цитата", "оценка": "Оценка"}

PROMPTS = {
    "выжимка": (
        "Ты сжимаешь новость до трёх пунктов. Пиши по-русски, по существу, "
        "каждый пункт с новой строки и начинается с «— ». Только то, что есть "
        "в тексте: ни выводов, ни фона, ни оценок. Если факта нет — не пиши его."
    ),
    "цитата": (
        "Найди в тексте одну самую содержательную прямую цитату и выпиши её "
        "дословно, без изменений, вместе с тем, кто её произнёс. Ничего не "
        "пересказывай. Если прямой речи в тексте нет, ответь ровно: "
        "«Прямой речи в материале нет»."
    ),
    "оценка": (
        "Ответь тремя короткими абзацами по-русски: что здесь нового, кого это "
        "касается, чего в тексте не сказано. Не выдумывай фактов, которых нет в "
        "материале; про недостающее пиши как про недостающее."
    ),
}
# Тексту Фонтанки хватает с запасом, а длинный материал стоит денег и секунд.
MAX_INPUT = 6000
MARK = {
    "выжимка": "🤖 Выжимка",
    "цитата": "❝ Цитата",
    "оценка": "🤖 Оценка",
}


def keyboard(item_id: int) -> dict[str, Any]:
    """Три кнопки под сырым сообщением. Нажатие — единственный вход в модель."""
    return {
        "inline_keyboard": [
            [
                {"text": BUTTON[KINDS[code]], "callback_data": "e:{}:{}".format(code, item_id)}
                for code in ("s", "q", "w")
            ]
        ]
    }


def parse(data: str) -> tuple[str, int] | None:
    """«e:s:42» → («выжимка», 42). Чужое — None, а не исключение."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "e" or parts[1] not in KINDS:
        return None
    try:
        return KINDS[parts[1]], int(parts[2])
    except ValueError:
        return None


async def text_of(session: Any, conn: Any, item: dict[str, Any]) -> str:
    """Текст материала: из базы, а если его там нет — со страницы.

    Такое бывает у совсем свежей новости: кнопку нажали раньше, чем контур
    обогащения успел сходить за текстом.
    """
    body = str(item.get("body") or "")
    if body or session is None:
        return body
    parsed = await article.load(session, str(item.get("url") or ""))
    if parsed.empty:
        return ""
    store.fill(conn, int(item["id"]), parsed.lead, parsed.body, store.now())
    return parsed.body


def render(kind: str, body: str, model_name: str, url: str, cached: bool = False) -> str:
    """Готовое сообщение: пометка машины, текст, ссылка на оригинал."""
    note = MARK.get(kind, kind)
    if kind == "оценка":
        note += " — это суждение модели, а не издания"
    tail = "\n\n{}\n<i>{}{}</i>".format(
        url, model_name or "модель", ", из памяти" if cached else ""
    )
    return "<b>{}</b>\n\n{}{}".format(note, html.escape(body.strip())[:3000], tail)


async def make(session: Any, conn: Any, item_id: int, kind: str,
               budget: model.Budget) -> str:
    """Весь путь одной кнопки. Ошибку возвращает текстом, наружу не бросает."""
    if kind not in PROMPTS:
        return "Такой кнопки нет."
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return "Новость потерялась."
    item = dict(row)
    ready = store.enrichment(conn, item_id, kind)
    if ready:
        budget.cached += 1
        return render(kind, ready["text"], str(ready["model"]), str(item["url"]), cached=True)
    body = await text_of(session, conn, item)
    if not body:
        return "Текст материала не открылся — остаётся оригинал: {}".format(item["url"])
    source = "{}\n\n{}".format(item.get("title") or "", body)[:MAX_INPUT]
    answer = await model.ask(session, PROMPTS[kind], source, budget)
    if not answer.ok:
        log.info("кнопка «%s» для %s не сработала: %s", kind, item_id, answer.error)
        return "Не получилось: {}. Оригинал: {}".format(answer.error, item["url"])
    store.save_enrichment(conn, item_id, kind, answer.text, answer.model)
    return render(kind, answer.text, answer.model, str(item["url"]))


__all__ = ("BUTTON", "CODES", "KINDS", "MARK", "MAX_INPUT", "PROMPTS", "keyboard",
           "make", "parse", "render", "text_of")
