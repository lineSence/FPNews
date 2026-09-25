"""Отложенная отдача: то, что ждало своей задержки, уходит позже.

Зачем контур. Человек может попросить отдавать не сразу, а через N минут:
первые минуты материал часто правят, и кому-то важнее устоявшийся текст, чем
минута форы. Сторож и рассылка при этом работают как раньше — они просто
пропускают такого получателя `[NEWS-002]`, а забрать пропущенное некому.
Этот контур и забирает.

Он не хранит собственную очередь. Очередь — это сама база: свежие материалы
плюс запись в `deliveries` о том, кому уже отправлено. Отдельная таблица
«ожидающих» рассинхронизировалась бы с рассылкой при первом же перезапуске
`[CORE-025]`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import deliver, store

log = logging.getLogger("fpnews.hold")

# Как часто проверяем. Задержка задаётся в минутах, поэтому чаще минуты
# смотреть незачем, а реже — значит врать человеку про его же настройку.
EVERY = 60.0
# Насколько назад смотрим. Сутки — это верхняя граница разумной задержки
# плюс запас на перезапуск. Дальше материал уже не новость [NEWS-004].
WINDOW_HOURS = 24


def waiting(conn: Any, hours: int = WINDOW_HOURS) -> list[int]:
    """Свежие материалы, которые ещё могут кому-то уйти.

    Возвращаем номера, а не решения: кому именно слать, решает рассылка по
    своим правилам, и дублировать их здесь нельзя — разъедутся.
    """
    rows = conn.execute(
        "SELECT id FROM items WHERE cold = 0 AND "
        "julianday(COALESCE(published_at, listed_at)) >= julianday('now', ?) ORDER BY id",
        ("-{} hours".format(int(hours)),),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def someone_waits(conn: Any) -> bool:
    """Есть ли вообще люди с задержкой. Нет — контур не трогает базу зря."""
    row = conn.execute("SELECT 1 FROM users WHERE delay > 0 LIMIT 1").fetchone()
    return row is not None


async def once(bot: Any, conn: Any) -> int:
    """Один проход: дослать то, чей срок подошёл. Возвращает число отправок."""
    if not someone_waits(conn):
        return 0
    sent = 0
    for item_id in waiting(conn):
        try:
            sent += await deliver.send_item(bot, conn, item_id)
        except Exception as exc:  # noqa: BLE001 — досылка не роняет сбор [CORE-017]
            log.warning("отложенная отдача %s сорвалась: %s", item_id, exc)
    if sent:
        log.info("отложенная отдача: отправлено %s", sent)
    return sent


async def loop(bot: Any, conn: Any, stop: Any, every: float = EVERY) -> int:
    """Контур: раз в минуту забирает то, что дожидалось своей задержки."""
    sent = 0
    while not stop.is_set():
        sent += await once(bot, conn)
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            continue
    return sent


__all__ = ("EVERY", "WINDOW_HOURS", "loop", "once", "someone_waits", "waiting")
