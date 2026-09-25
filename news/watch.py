"""Сторож: опросить дверь, отделить новое, записать. Больше ничего.

Это самый горячий кусок программы, и его ценность — в том, чего здесь нет.
Нет отбора по темам, нет модели, нет сети к кому-либо кроме самой двери
`[NEWS-002]`. Чем тупее сторож, тем меньше между появлением новости и записью
о ней.

Новизна определяется **множеством известных адресов**, а не позицией в ленте:
у Фонтанки лента не отсортирована по времени, а у Медузы CDN иногда меняет
`Last-Modified` без единой новой ссылки. Верить порядку и кодам ответа нельзя,
верить можно только сравнению адресов.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import diag

from . import bridge, fetch, sources, store

log = logging.getLogger("fpnews.watch")


@dataclass
class Step:
    """Итог одного захода — для диагностики и для тестов."""

    status: int
    seconds: float
    size: int
    found: int = 0
    fresh: int = 0
    cold: int = 0
    conditional: bool = False
    error: str = ""
    items: list[int] = field(default_factory=list)


async def once(
    session: Any,
    source: sources.Source,
    conn: sqlite3.Connection,
    door: fetch.Door,
    queue: "asyncio.Queue[int] | None" = None,
) -> Step:
    """Один заход к двери. Возвращает, что нашлось и что оказалось новым."""
    bridge.у_двери(source.code, True)
    try:
        poll = await fetch.poll(session, source.door, door)
    finally:
        bridge.у_двери(source.code, False)
    step = Step(
        status=poll.status,
        seconds=poll.seconds,
        size=poll.size,
        conditional=poll.conditional,
        error=poll.error,
    )
    if poll.conditional or not poll.body:
        if poll.refused:
            log.warning("%s: защита ответила %s, пауза ×%s", source.label, poll.status, door.backoff)
        diag.event("опрос", площадка=source.code, код=poll.status, секунд=round(poll.seconds, 3),
                   байт=poll.size, условный=poll.conditional, ошибка=poll.error)
        bridge.заход(source.code, код=poll.status, найдено=0, новых=0, ошибка=poll.error)
        return step

    found = sources.extract(source, poll.body)
    step.found = len(found)
    listed_at = store.now()
    # Первый заход видит всю ленту сразу — это не новости, а её содержимое на
    # момент запуска. Запоминаем, чтобы не считать и не рассылать [NEWS-001].
    cold = not door.seen
    for item in found:
        if item.url in door.seen:
            continue
        door.seen.add(item.url)
        item_id, is_new = store.remember(
            conn,
            source.code,
            item.url,
            item.title,
            listed_at,
            published_at=store.published(item.published_at),
            cold=cold,
        )
        if not is_new:
            continue
        if item.whole:
            # Текст пришёл вместе со списком: разбор страницы не нужен, и
            # метка `fetched_at` ставится тем же мгновением [NEWS-003].
            store.fill(conn, item_id, item.lead, item.body, listed_at)
        if cold:
            step.cold += 1
            continue
        step.fresh += 1
        step.items.append(item_id)
        diag.event(
            "новость",
            площадка=source.code,
            адрес=item.url,
            заголовок=item.title[:120],
            целиком=item.whole,
            символов=len(item.body),
            редакционная=store.latency_of(conn, item_id, "редакционная"),
        )
        if queue is not None:
            queue.put_nowait(item_id)
    diag.event("опрос", площадка=source.code, код=poll.status, секунд=round(poll.seconds, 3),
               байт=poll.size, ссылок=step.found, новых=step.fresh, холодных=step.cold)
    bridge.заход(source.code, код=poll.status, найдено=step.found, новых=step.fresh)
    if step.cold:
        log.info("%s: холодный старт, подобрано %s — не считаем и не шлём",
                 source.label, step.cold)
    if step.fresh:
        log.info("%s: новых %s из %s", source.label, step.fresh, step.found)
    return step


async def loop(
    session: Any,
    source: sources.Source,
    conn: sqlite3.Connection,
    stop: asyncio.Event,
    queue: "asyncio.Queue[int] | None" = None,
    limit: int = 0,
) -> int:
    """Бесконечный обход двери до сигнала остановки.

    Пауза — собственный интервал источника, умноженный на текущий откат: чужая
    защита не долбится `[NEWS-006]`. Первый заход делается сразу: ждать
    интервал на старте незачем.
    """
    door = fetch.Door()
    rounds = 0
    просьба = bridge.подписаться(source.code)
    try:
        return await _обход(session, source, conn, stop, queue, limit, door, просьба)
    finally:
        bridge.отписаться(source.code)


async def _обход(
    session: Any,
    source: sources.Source,
    conn: sqlite3.Connection,
    stop: asyncio.Event,
    queue: "asyncio.Queue[int] | None",
    limit: int,
    door: fetch.Door,
    просьба: asyncio.Event,
) -> int:
    """Сам круг заходов. Вынесен, чтобы подписка на просьбы снималась всегда."""
    rounds = 0
    while not stop.is_set():
        await once(session, source, conn, door, queue)
        rounds += 1
        if limit and rounds >= limit:
            break
        # Интервал можно переопределить в интерфейсе: у разных лент разный темп,
        # а править код ради этого не должно быть нужно.
        every = store.source_every(conn, source.code) or source.interval
        # Просьба со страницы «Опросить сейчас» будит раньше срока. Откат от
        # чужой защиты она не отменяет: пауза считается тем же способом, а
        # разбуженный сторож просто идёт к двери на круг раньше [NEWS-006].
        просьба.clear()
        ждём = [asyncio.create_task(stop.wait()), asyncio.create_task(просьба.wait())]
        try:
            await asyncio.wait(ждём, timeout=every * door.backoff,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            for задача in ждём:
                задача.cancel()
        просьба.clear()
    return rounds


__all__ = ("Step", "loop", "once")
