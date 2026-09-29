"""Сводка: одно сообщение вместо потока.

Зачем. Подписка на тему хороша, пока тем две. На шести темах и семи
изданиях бот превращается в ленту, которую перестают читать, — и смысл
затеи теряется. Сводка отвечает на вопрос «что я пропустил», а не «что
случилось секунду назад»: горячий путь остаётся у сырых сообщений
[NEWS-003].

Что внутри: сколько всего материалов мы видели за окно, что пришло по
вашим темам, какие правки и снятия мы зафиксировали, что нашли
сохранённые запросы и о ком вдруг стали писать чаще обычного.

Отдельно про числа. «За сутки 40 материалов» — это сорок материалов,
которые видели мы, а не всё, что вышло в городе [NEWS-001]. Формулировки в
сводке подобраны так, чтобы не выдавать наше наблюдение за полноту.

Время отправки хранится в `users.digest_at` («09:00», пусто — выключено), а
дата последней отправки — в `users.digest_on`. Поэтому перезапуск процесса
не присылает сводку второй раз, а простой дольше суток не рассылает пачку
за все пропущенные дни.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import store

log = logging.getLogger("fpnews.digest")

# Сколько строк каждого вида показываем. Сводка длиннее экрана — снова лента.
LINES = 6
DEFAULT_HOURS = 24


def hours_back(hours: int = DEFAULT_HOURS) -> str:
    return "-{} hours".format(int(hours))


def collect(conn: Any, user_id: int, hours: int = DEFAULT_HOURS) -> dict[str, Any]:
    """Что произошло за окно с точки зрения одного человека."""
    окно = hours_back(hours)
    всего = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE "
        "julianday(COALESCE(published_at, listed_at)) >= julianday('now', ?)", (окно,),
    ).fetchone()["n"]
    свои = conn.execute(
        "SELECT i.id, i.title, i.url, i.source, d.kind FROM deliveries d "
        "JOIN items i ON i.id = d.item_id WHERE d.user_id = ? AND "
        "julianday(d.sent_at) >= julianday('now', ?) AND d.kind IN ('сырое', 'запрос') ORDER BY d.id DESC LIMIT ?",
        (int(user_id), окно, LINES),
    ).fetchall()
    # Сводка описывает окно с точки зрения одного человека, поэтому правки,
    # снятия и всплески тоже проходят через его область видимости: чужие
    # материалы в «вашей» сводке — это не сводка, а подглядывание [CORE-016].
    from . import scope  # noqa: PLC0415 — нужен только здесь

    хвост, параметры = scope.условие(conn, user_id)
    чужие = (" AND " + хвост) if хвост else ""
    правки = conn.execute(
        "SELECT i.id, i.title, r.seen_at FROM item_revisions r JOIN items i ON i.id = r.item_id "
        "WHERE julianday(r.seen_at) >= julianday('now', ?)" + чужие + " GROUP BY i.id "
        "ORDER BY r.seen_at DESC LIMIT ?",
        (окно, *параметры, LINES),
    ).fetchall()
    снятия = conn.execute(
        "SELECT i.id, i.title, i.gone_code FROM items i WHERE i.gone_at IS NOT NULL "
        "AND julianday(i.gone_at) >= julianday('now', ?)" + чужие + " "
        "ORDER BY i.gone_at DESC LIMIT ?", (окно, *параметры, LINES),
    ).fetchall()
    return {
        "часов": int(hours),
        "всего": int(всего or 0),
        "ваше": [dict(row) for row in свои],
        "правки": [dict(row) for row in правки],
        "снятия": [dict(row) for row in снятия],
        "всплески": [row for row in store.bursts(conn, window=1, limit=LINES, user_id=user_id)
                     if not row["новое"] or row["сейчас"] >= 2][:LINES],
    }


def empty(data: dict[str, Any]) -> bool:
    """Пустая сводка. Такую не шлём: «ничего» человек и так видит."""
    return not (data["ваше"] or data["правки"] or data["снятия"] or data["всплески"])


def _норма_словами(row: dict[str, Any]) -> str:
    """Хвост строки всплеска. Множитель — только когда есть на что делить.

    При нулевой медиане фона (о ком-то пишут реже чем через день) «во
    сколько раз» не определено: store отдаёт None, и печатать его как «×None»
    нельзя. Пишем словами, что обычно — ноль [NEWS-001].
    """
    if row.get("новое"):
        return " (впервые)"
    раз = row.get("во_сколько_раз")
    if раз:
        return " (×{} к норме)".format(раз)
    return " (обычно — ноль в день)"


def text(data: dict[str, Any], base_url: str = "") -> str:
    """Сводка словами. HTML для Telegram, ссылки на оригиналы обязательны."""
    куски = ["<b>Сводка за {} ч</b>".format(int(data["часов"])),
             "Мы видели {} материалов.".format(int(data["всего"]))]
    if data["ваше"]:
        куски.append("\n<b>По вашим темам и запросам</b>")
        куски.extend(
            "• <a href=\"{url}\">{title}</a> — {source}".format(
                url=row["url"], title=html.escape(str(row["title"] or "без заголовка")),
                source=str(row["source"]))
            for row in data["ваше"]
        )
    if data["правки"]:
        куски.append("\n<b>Правили</b>")
        куски.extend("• {}".format(html.escape(str(row["title"] or "")[:90]))
                     for row in data["правки"])
    if data["снятия"]:
        куски.append("\n<b>Сняли с публикации</b>")
        куски.extend("• {} ({})".format(html.escape(str(row["title"] or "")[:90]),
                                        row["gone_code"] or "?")
                     for row in data["снятия"])
    if data["всплески"]:
        куски.append("\n<b>Стали писать чаще</b>")
        куски.extend(
            "• {name} — {сейчас} за сутки{хвост}".format(
                name=html.escape(str(row["имя"])), сейчас=row["сейчас"],
                хвост=_норма_словами(row))
            for row in data["всплески"]
        )
    if base_url:
        куски.append("\n{}/лента".format(base_url.rstrip("/")))
    куски.append("\n<i>Это то, что видели мы, а не всё, что вышло.</i>")
    return "\n".join(куски)


def now_local(zone: str) -> datetime:
    """Текущее время в поясе человека. Неизвестный пояс — Москва."""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return datetime.now(ZoneInfo(zone or "Europe/Moscow"))
    except Exception:  # noqa: BLE001 — кривой пояс не повод падать [CORE-017]
        return datetime.now(timezone(timedelta(hours=3)))


def due(row: dict[str, Any], moment: datetime) -> bool:
    """Пора ли слать этому человеку.

    Условие нарочно простое: настал его час и сегодня ещё не слали. Опоздание
    на полчаса лучше, чем две сводки подряд после перезапуска.
    """
    когда = str(row.get("digest_at") or "").strip()
    if not когда or ":" not in когда:
        return False
    try:
        час, минута = (int(part) for part in когда.split(":")[:2])
    except ValueError:
        return False
    if str(row.get("digest_on") or "") == moment.date().isoformat():
        return False
    return (moment.hour, moment.minute) >= (час, минута)


async def once(bot: Any, conn: Any, base_url: str = "") -> int:
    """Один обход: разослать сводки тем, кому пора. Вернуть число отправок."""
    rows = conn.execute(
        "SELECT id, time_zone, digest_at, digest_on FROM users WHERE digest_at != ''"
    ).fetchall()
    sent = 0
    for row in rows:
        человек = dict(row)
        if not due(человек, now_local(str(человек.get("time_zone") or ""))):
            continue
        data = collect(conn, int(человек["id"]))
        сегодня = now_local(str(человек.get("time_zone") or "")).date().isoformat()
        if empty(data):
            # Отметку всё равно ставим: иначе пустая сводка будет проверяться
            # каждые десять минут до конца суток.
            store.mark_digest(conn, int(человек["id"]), сегодня)
            continue
        if not await bot.send(int(человек["id"]), text(data, base_url), preview=False):
            continue
        store.mark_digest(conn, int(человек["id"]), сегодня)
        sent += 1
    if sent:
        log.info("сводок отправлено: %s", sent)
    return sent


async def loop(bot: Any, conn: Any, stop: Any, every: float = 600.0, rounds: int = 0) -> int:
    """Фоновый контур. Раз в десять минут — этого хватает для часа с точностью."""
    import asyncio  # noqa: PLC0415

    from . import web  # noqa: PLC0415

    sent = 0
    while not stop.is_set():
        try:
            sent += await once(bot, conn, web.base_url())
        except Exception as exc:  # noqa: BLE001 — сводка не роняет сбор [CORE-017]
            log.warning("обход сводок сорвался: %s", exc)
        rounds -= 1
        if rounds == 0:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            continue
    return sent


__all__ = ("DEFAULT_HOURS", "LINES", "collect", "due", "empty", "hours_back", "loop",
           "now_local", "once", "text")
