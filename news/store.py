"""База: SQLite в режиме WAL и пять меток времени на каждой новости.

Почему SQLite, а не «что-нибудь быстрое вроде кэша»: обращение к нему не идёт
через сокет вовсе — это вызов функции внутри нашего процесса. Redis выигрывает,
когда процессов много; у нас один. Разбор вариантов — `docs/news-schema.md`.

Метки времени заводятся до того, как появилась первая новость, и не
выключаются настройкой `[NEWS-001]`: без них разговор о скорости превращается
в ощущения. Из них считаются три разные задержки, и смешивать их нельзя —
редакционную мы не контролируем, свою обязаны сокращать.

Таблиц ровно столько, сколько нужно шагам 1–4 дорожной карты. Кластеры сюжетов
и векторы появятся вместе с обогащением, а не «на всякий случай» `[CORE-025]`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("data/fpnews.sqlite3")
# Кэш страниц SQLite в килобайтах со знаком минус. 20 МБ — осознанный потолок:
# на сервере свободно около 500 МБ, и база не имеет права их съесть.
CACHE_KB = 20_000

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS items (
        id            INTEGER PRIMARY KEY,
        url           TEXT NOT NULL UNIQUE,   -- канонический адрес
        source        TEXT NOT NULL,
        title         TEXT NOT NULL DEFAULT '',
        lead          TEXT NOT NULL DEFAULT '',
        body          TEXT NOT NULL DEFAULT '',
        simhash       TEXT NOT NULL DEFAULT '',
        published_at  TEXT,                   -- время издания
        listed_at     TEXT,                   -- увидели адрес в ленте
        fetched_at    TEXT,                   -- скачали и разобрали текст
        sent_at       TEXT,                   -- ушло сырое сообщение
        enriched_at   TEXT                    -- ушло дополнение
    )
    """,
    "CREATE INDEX IF NOT EXISTS items_listed ON items(listed_at)",
    "CREATE INDEX IF NOT EXISTS items_source ON items(source, listed_at)",
    """
    CREATE TABLE IF NOT EXISTS item_revisions (
        id         INTEGER PRIMARY KEY,
        item_id    INTEGER NOT NULL,
        seen_at    TEXT NOT NULL,
        title      TEXT NOT NULL DEFAULT '',
        length     INTEGER NOT NULL DEFAULT 0,
        digest     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS revisions_item ON item_revisions(item_id, seen_at)",
    """
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY,       -- id пользователя Telegram
        name        TEXT NOT NULL DEFAULT '',
        time_zone   TEXT NOT NULL DEFAULT 'Europe/Moscow',
        quiet_from  TEXT NOT NULL DEFAULT '',
        quiet_to    TEXT NOT NULL DEFAULT '',
        created_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS topics (
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        title      TEXT NOT NULL,
        words      TEXT NOT NULL DEFAULT '',   -- слова через запятую
        wording    TEXT NOT NULL DEFAULT '',   -- описание словами, для модели
        sources    TEXT NOT NULL DEFAULT '',   -- коды источников; пусто — все
        enabled    INTEGER NOT NULL DEFAULT 1,
        created_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS topics_user ON topics(user_id, enabled)",
    """
    CREATE TABLE IF NOT EXISTS deliveries (
        id       INTEGER PRIMARY KEY,
        item_id  INTEGER NOT NULL,
        user_id  INTEGER NOT NULL,
        topic_id INTEGER,
        kind     TEXT NOT NULL,                -- сырое | дополнение | изменение | тоже_написали
        sent_at  TEXT NOT NULL,
        UNIQUE(item_id, user_id, kind)         -- защита от повторной отправки
    )
    """,
)

STAMPS = ("published_at", "listed_at", "fetched_at", "sent_at", "enriched_at")


def connect(path: str | Path = DEFAULT_PATH) -> sqlite3.Connection:
    """Соединение с включённым WAL: читатели не мешают единственному писателю."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-{}".format(CACHE_KB))
    ensure(conn)
    return conn


def ensure(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)
    conn.commit()


def remember(conn: sqlite3.Connection, source: str, url: str, title: str, listed_at: str,
             published_at: str = "") -> tuple[int, bool]:
    """Записывает найденный адрес. Возвращает (id, новая ли).

    Момент обнаружения (`listed_at`) ставится один раз и больше не трогается:
    он и есть точка отсчёта нашей задержки. Заголовок из ленты может позже
    уточниться разбором страницы, время обнаружения — нет.
    """
    row = conn.execute("SELECT id FROM items WHERE url = ?", (url,)).fetchone()
    if row is not None:
        return int(row["id"]), False
    cursor = conn.execute(
        "INSERT INTO items(url, source, title, published_at, listed_at) VALUES(?,?,?,?,?)",
        (url, source, title, published_at or None, listed_at),
    )
    conn.commit()
    return int(cursor.lastrowid or 0), True


def stamp(conn: sqlite3.Connection, item_id: int, field: str, when: str) -> None:
    """Проставляет метку времени. Чужое имя поля не принимается."""
    if field not in STAMPS:
        raise ValueError("нет такой метки: {}".format(field))
    conn.execute("UPDATE items SET {} = ? WHERE id = ?".format(field), (when, item_id))
    conn.commit()


def latency_rows(conn: sqlite3.Connection, limit: int = 500) -> list[dict[str, Any]]:
    """Задержки последних новостей в секундах: редакционная, наша, до полного."""
    rows = conn.execute(
        "SELECT url, source, published_at, listed_at, sent_at, enriched_at "
        "FROM items WHERE listed_at IS NOT NULL ORDER BY listed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "url": row["url"],
                "площадка": row["source"],
                "редакционная": _delta(row["published_at"], row["listed_at"]),
                "до_отправки": _delta(row["listed_at"], row["sent_at"]),
                "до_полного": _delta(row["sent_at"], row["enriched_at"]),
            }
        )
    return out


def _delta(first: Any, second: Any) -> float | None:
    from datetime import datetime  # noqa: PLC0415 — нужен только здесь

    if not first or not second:
        return None
    try:
        start = datetime.fromisoformat(str(first))
        end = datetime.fromisoformat(str(second))
    except ValueError:
        return None
    return round((end - start).total_seconds(), 3)


__all__ = ("CACHE_KB", "DEFAULT_PATH", "SCHEMA", "STAMPS", "connect", "ensure",
           "latency_rows", "remember", "stamp")
