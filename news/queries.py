"""Сохранённые запросы: архив превращается в наблюдение.

Поиск отвечает на вопрос, заданный сейчас. Работа аналитика устроена иначе:
вопрос задаётся один раз («что про этот подряд»), а ответы приходят месяцами.
Без этого человек обязан помнить, что надо зайти и поискать, — и однажды не
зайдёт.

Три решения.

1. **Отсчёт от момента сохранения.** Подписка говорит о новом; выдать весь
   архив в первую же проверку значит приучить не читать уведомления
   `[NEWS-004]`.
2. **Ни одного лишнего запроса к модели.** Проверка — тот же `search` по
   индексу FTS5, то есть доли миллисекунды на одном ядре `[NEWS-002]`.
3. **Отправка идёт через `deliver`,** как всё остальное: одна защита от
   повторов, один вид сообщений, одни тихие часы.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from . import search, store

log = logging.getLogger("fpnews.queries")

# Сколько находок по одному запросу отдаём за раз: остальное подождёт
# следующего захода, лента из тридцати сообщений — не уведомление [NEWS-004].
BATCH = 5


def fresh(conn: Any, saved: dict[str, Any], limit: int = BATCH) -> list[dict[str, Any]]:
    """Находки по запросу, которых человек ещё не видел, от старых к новым."""
    rows = search.search(
        conn,
        str(saved.get("query") or ""),
        source=str(saved.get("source") or ""),
        topic_id=saved.get("topic_id"),
        only_original=bool(saved.get("only_original")),
        only_revised=bool(saved.get("only_revised")),
        limit=search.MAX_LIMIT,
    )
    last = int(saved.get("last_item_id") or 0)
    new = [row for row in rows if int(row.get("id") or 0) > last]
    new.sort(key=lambda row: int(row.get("id") or 0))
    return new[:limit]


def message(saved: dict[str, Any], row: dict[str, Any]) -> str:
    """Уведомление по сохранённому запросу: чей запрос, что нашлось, ссылка."""
    return (
        "<i>По запросу «{query}»</i>\n\n<b>{title}</b>\n{source}\n{url}"
    ).format(
        query=html.escape(str(saved.get("title") or saved.get("query") or "")),
        title=html.escape(str(row.get("заголовок") or "")),
        source=html.escape(str(row.get("источник") or "")),
        url=row.get("url") or "",
    )


async def once(bot: Any, conn: Any) -> int:
    """Один обход всех подписок. Возвращает число отправленных уведомлений."""
    from . import deliver  # noqa: PLC0415 — импорт здесь, чтобы не было круга

    sent = 0
    for saved in store.queries(conn):
        rows = fresh(conn, saved)
        if not rows:
            continue
        top = max(int(row.get("id") or 0) for row in rows)
        if saved.get("notify"):
            for row in rows:
                sent += await deliver.send_to(
                    bot, conn, int(row.get("id") or 0), int(saved["user_id"]),
                    message(saved, row), kind="запрос",
                )
        store.mark_query_seen(conn, int(saved["id"]), top)
    if sent:
        log.info("уведомлений по сохранённым запросам: %s", sent)
    return sent


async def loop(bot: Any, conn: Any, stop: Any, every: float = 300.0, rounds: int = 0) -> int:
    """Фоновый контур. Реже, чем сбор: подписка — не горячий путь [NEWS-003]."""
    import asyncio  # noqa: PLC0415

    sent = 0
    while not stop.is_set():
        try:
            sent += await once(bot, conn)
        except Exception as exc:  # noqa: BLE001 — подписки не роняют сбор [CORE-017]
            log.warning("обход сохранённых запросов сорвался: %s", exc)
        rounds -= 1
        if rounds == 0:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            continue
    return sent


__all__ = ("BATCH", "fresh", "loop", "message", "once")
