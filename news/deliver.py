"""Рассылка: кому ушла новость, в каком виде и когда.

Здесь заканчивается горячий путь. Всё, что дальше (выжимка, оценка, склейка
сюжетов), — контур обогащения, и оно не имеет права задерживать это место
`[NEWS-002]`, `[NEWS-003]`.

Повторная отправка невозможна на уровне базы: пара «новость + человек + вид»
уникальна. Это важнее аккуратности кода — при перезапуске посреди рассылки
человек не должен получить всё заново.
"""

from __future__ import annotations

import html
import logging
import sqlite3
from typing import Any

from . import enrich, store, topics

log = logging.getLogger("fpnews.deliver")

LABEL = {"meduza": "Медуза", "fontanka": "Фонтанка"}


def subscribers(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.id, t.user_id, t.title, t.words, t.sources, t.enabled "
        "FROM topics t JOIN users u ON u.id = t.user_id WHERE t.enabled = 1"
    ).fetchall()
    return [dict(row) for row in rows]


def message(item: dict[str, Any], hit: topics.Hit) -> str:
    """Сырое сообщение: заголовок, где сработала тема, ссылка.

    Ссылка на оригинал обязательна `[NEWS-007]`: мы показываем, что вышло, а
    не пересказываем издание вместо него.
    """
    source = LABEL.get(str(item.get("source")), str(item.get("source")))
    title = html.escape(str(item.get("title") or "без заголовка"))
    words = ", ".join(hit.words[:4])
    return (
        "<b>{title}</b>\n"
        "{source} · тема «{topic}» — {where}: {words}\n"
        "{url}"
    ).format(
        title=title,
        source=source,
        topic=html.escape(hit.title),
        where=hit.strength,
        words=html.escape(words),
        url=item.get("url"),
    )


def also_message(item: dict[str, Any], original: dict[str, Any]) -> str:
    """Второе сообщение по тому же событию: коротко и со ссылкой."""
    source = LABEL.get(str(item.get("source")), str(item.get("source")))
    return (
        "<i>Тоже написали</i> — {source}\n"
        "<b>{title}</b>\n{url}\n\n"
        "Об этом же: {first}"
    ).format(
        source=source,
        title=html.escape(str(item.get("title") or "")),
        url=item.get("url"),
        first=original.get("url"),
    )


async def send_also(bot: Any, conn: sqlite3.Connection, item_id: int, original_id: int) -> int:
    """Перепечатка уходит только тем, кто уже получил оригинал.

    Кто оригинал не получал (тема не сработала или он пришёл до подписки),
    получает обычное сообщение: для него это не повтор, а новость.
    """
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    first = conn.execute("SELECT * FROM items WHERE id = ?", (original_id,)).fetchone()
    if row is None or first is None or row["cold"]:
        return 0
    item, original = dict(row), dict(first)
    got = {
        int(line["user_id"])
        for line in conn.execute(
            "SELECT user_id FROM deliveries WHERE item_id = ?", (original_id,)
        ).fetchall()
    }
    sent = 0
    for hit in topics.pick(subscribers(conn), item.get("title") or "", item.get("body") or "",
                           str(item.get("source") or "")):
        if hit.user_id not in got:
            continue  # обычную отправку сделает send_item
        if already(conn, item_id, hit.user_id, "тоже_написали"):
            continue
        if not await bot.send(hit.user_id, also_message(item, original), preview=False,
                              keyboard=enrich.keyboard(item_id)):
            continue
        record(conn, item_id, hit.user_id, hit.topic_id, "тоже_написали")
        sent += 1
    return sent


async def send_item(bot: Any, conn: sqlite3.Connection, item_id: int) -> int:
    """Разослать одну новость всем, чьи темы сработали. Вернуть число отправок."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None or row["cold"]:
        # Подобранное на холодном старте не рассылается: человеку не нужна
        # пачка вчерашнего при каждом перезапуске.
        return 0
    item = dict(row)
    hits = topics.pick(subscribers(conn), item.get("title") or "", item.get("body") or "",
                       str(item.get("source") or ""))
    knew = _already_knows(conn, item.get("dup_of"))
    sent = 0
    for hit in hits:
        if hit.user_id in knew:
            # Ему уже приходил оригинал: перепечатка уйдёт как «тоже написали».
            continue
        if already(conn, item_id, hit.user_id, "сырое"):
            continue
        if not await bot.send(hit.user_id, message(item, hit),
                              keyboard=enrich.keyboard(item_id)):
            continue
        record(conn, item_id, hit.user_id, hit.topic_id, "сырое")
        if not sent and not item.get("sent_at"):
            # Метка ставится по первому получателю: наша задержка кончается
            # здесь, а не на последнем человеке в списке [NEWS-001].
            store.stamp(conn, item_id, "sent_at", store.now())
        sent += 1
    if sent:
        log.info("разослано %s: %s", sent, str(item.get("title"))[:80])
    return sent


def _already_knows(conn: sqlite3.Connection, original_id: Any) -> set[int]:
    if not original_id:
        return set()
    rows = conn.execute(
        "SELECT user_id FROM deliveries WHERE item_id = ?", (int(original_id),)
    ).fetchall()
    return {int(row["user_id"]) for row in rows}


def already(conn: sqlite3.Connection, item_id: int, user_id: int, kind: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM deliveries WHERE item_id = ? AND user_id = ? AND kind = ?",
        (item_id, user_id, kind),
    ).fetchone()
    return row is not None


def record(conn: sqlite3.Connection, item_id: int, user_id: int, topic_id: int, kind: str) -> None:
    try:
        conn.execute(
            "INSERT INTO deliveries(item_id, user_id, topic_id, kind, sent_at) VALUES(?,?,?,?,?)",
            (item_id, user_id, topic_id, kind, store.now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # Гонка двух контуров: значит уже отправлено, и это нормально.
        pass


__all__ = ("LABEL", "already", "also_message", "message", "record", "send_also",
           "send_item", "subscribers")
