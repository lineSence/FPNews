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
        enriched_at   TEXT,                   -- ушло дополнение
        cold          INTEGER NOT NULL DEFAULT 0, -- подобрано на холодном старте
        dup_of        INTEGER,                    -- id новости, о которой уже писали
        checked_at    TEXT,                       -- когда последний раз перечитывали
        checks        INTEGER NOT NULL DEFAULT 0  -- сколько раз перечитали
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
    """
    CREATE TABLE IF NOT EXISTS vectors (
        item_id    INTEGER NOT NULL,
        model      TEXT NOT NULL,              -- имя модели живёт рядом [LLM-011]
        vec        BLOB NOT NULL,              -- нормированные float32
        created_at TEXT NOT NULL,
        UNIQUE(item_id, model)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS enrichments (
        item_id    INTEGER NOT NULL,
        kind       TEXT NOT NULL,              -- выжимка | цитата | оценка
        text       TEXT NOT NULL,
        model      TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        UNIQUE(item_id, kind)                  -- второе нажатие бесплатно
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


# Колонки, добавленные после того, как база уже работала на сервере.
# `CREATE TABLE IF NOT EXISTS` их не добавит — нужен явный ALTER.
LATE_COLUMNS = (
    ("items", "checked_at", "TEXT"),
    ("items", "checks", "INTEGER NOT NULL DEFAULT 0"),
)


def ensure(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA:
        conn.execute(statement)
    for table, column, kind in LATE_COLUMNS:
        have = {row["name"] for row in conn.execute("PRAGMA table_info({})".format(table))}
        if column not in have:
            conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, column, kind))
    conn.commit()


def remember(conn: sqlite3.Connection, source: str, url: str, title: str, listed_at: str,
             published_at: str = "", cold: bool = False) -> tuple[int, bool]:
    """Записывает найденный адрес. Возвращает (id, новая ли).

    Момент обнаружения (`listed_at`) ставится один раз и больше не трогается:
    он и есть точка отсчёта нашей задержки. Заголовок из ленты может позже
    уточниться разбором страницы, время обнаружения — нет.

    `cold` — новость подобрана на первом заходе, то есть лежала в ленте ещё до
    запуска. Её «редакционная задержка» равна возрасту ленты, а не скорости
    издания, и в статистику такие не идут: иначе первый же старт показал бы
    шесть часов и обесценил все остальные цифры [NEWS-001]. Рассылать их тоже
    нельзя — пользователь получил бы пачку вчерашнего.
    """
    row = conn.execute("SELECT id FROM items WHERE url = ?", (url,)).fetchone()
    if row is not None:
        return int(row["id"]), False
    cursor = conn.execute(
        "INSERT INTO items(url, source, title, published_at, listed_at, cold) "
        "VALUES(?,?,?,?,?,?)",
        (url, source, title, published_at or None, listed_at, 1 if cold else 0),
    )
    conn.commit()
    return int(cursor.lastrowid or 0), True


def fill(conn: sqlite3.Connection, item_id: int, lead: str, body: str, fetched_at: str) -> None:
    """Текст материала и метка разбора. Пустым текстом ничего не затираем."""
    if not (lead or body):
        return
    conn.execute(
        "UPDATE items SET lead = ?, body = ?, fetched_at = ? WHERE id = ?",
        (lead, body, fetched_at, item_id),
    )
    conn.commit()


def mark_dup(conn: sqlite3.Connection, item_id: int, original_id: int) -> None:
    """Пометить новость перепечаткой. Цепочки не строим: только на оригинал."""
    conn.execute("UPDATE items SET dup_of = ? WHERE id = ?", (original_id, item_id))
    conn.commit()


def set_fingerprint(conn: sqlite3.Connection, item_id: int, mark: str) -> None:
    conn.execute("UPDATE items SET simhash = ? WHERE id = ?", (mark, item_id))
    conn.commit()


def now() -> str:
    """Единый вид времени в базе: UTC по ISO, с точностью до микросекунд.

    Точность важнее красоты: задержки здесь меряются секундами, и округление
    до секунды съело бы половину измеряемой величины [NEWS-001].
    """
    from datetime import datetime, timezone  # noqa: PLC0415 — нужен только здесь

    return datetime.now(timezone.utc).isoformat()


def published(raw: str) -> str:
    """Время публикации из ленты в наш вид. Непонятное — пустая строка.

    В RSS оно приходит по RFC 2822 («Thu, 24 Sep 2026 22:34:08 +0300»), и без
    разбора редакционную задержку посчитать не из чего.
    """
    from email.utils import parsedate_to_datetime  # noqa: PLC0415

    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return parsedate_to_datetime(text).isoformat()
    except (TypeError, ValueError):
        try:
            from datetime import datetime  # noqa: PLC0415

            return datetime.fromisoformat(text).isoformat()
        except ValueError:
            return ""


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
        "FROM items WHERE listed_at IS NOT NULL AND cold = 0 ORDER BY listed_at DESC LIMIT ?",
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


PAIRS = {
    "редакционная": ("published_at", "listed_at"),
    "до_отправки": ("listed_at", "sent_at"),
    "до_полного": ("sent_at", "enriched_at"),
}


def latency_of(conn: sqlite3.Connection, item_id: int, kind: str) -> float | None:
    """Одна задержка одной новости в секундах. Нет метки — None, не ноль.

    Ноль вместо «не знаем» испортил бы статистику молча, а это худший вид
    ошибки в измерениях [NEWS-001].
    """
    first, second = PAIRS[kind]
    row = conn.execute(
        "SELECT {}, {} FROM items WHERE id = ?".format(first, second), (item_id,)
    ).fetchone()
    return _delta(row[0], row[1]) if row is not None else None


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


def enrichment(conn: sqlite3.Connection, item_id: int, kind: str) -> dict[str, Any] | None:
    """Готовый ответ модели по этой новости, если он уже есть.

    Кэш здесь не оптимизация, а обязанность: десять человек нажимают ту же
    кнопку под той же новостью, и десять одинаковых вызовов модели — это
    выброшенная квота [CORE-016].
    """
    row = conn.execute(
        "SELECT text, model, created_at FROM enrichments WHERE item_id = ? AND kind = ?",
        (item_id, kind),
    ).fetchone()
    return dict(row) if row is not None else None


def save_enrichment(conn: sqlite3.Connection, item_id: int, kind: str, text: str,
                    model: str) -> None:
    conn.execute(
        "INSERT INTO enrichments(item_id, kind, text, model, created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(item_id, kind) DO UPDATE SET text = excluded.text, "
        "model = excluded.model, created_at = excluded.created_at",
        (item_id, kind, text, model, now()),
    )
    conn.commit()

def vector_of(conn: sqlite3.Connection, item_id: int, model: str) -> bytes:
    """Вектор новости этой моделью. Чужой моделью — как будто его нет."""
    row = conn.execute(
        "SELECT vec FROM vectors WHERE item_id = ? AND model = ?", (item_id, model)
    ).fetchone()
    return bytes(row["vec"]) if row is not None else b""


def save_vector(conn: sqlite3.Connection, item_id: int, model: str, vec: bytes) -> None:
    conn.execute(
        "INSERT INTO vectors(item_id, model, vec, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(item_id, model) DO UPDATE SET vec = excluded.vec",
        (item_id, model, vec, now()),
    )
    conn.commit()


def neighbours(conn: sqlite3.Connection, item_id: int, model: str,
               hours: int = 12, limit: int = 300) -> list[dict[str, Any]]:
    """Соседи с векторами за окно: кандидаты на «тоже написали».

    Берём только те, что кому-то ушли: у остальных вектора и нет — считать
    его было бы тратой квоты на новость, которой никто не видел [CORE-016].
    """
    rows = conn.execute(
        "SELECT i.id, i.title, i.url, i.source, v.vec FROM items i "
        "JOIN vectors v ON v.item_id = i.id AND v.model = ? "
        "WHERE i.id != ? AND i.dup_of IS NULL AND i.cold = 0 "
        "AND i.listed_at >= datetime('now', ?) ORDER BY i.id DESC LIMIT ?",
        (model, item_id, "-{} hours".format(int(hours)), int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def revise(conn: sqlite3.Connection, item_id: int, title: str, length: int,
           digest: str) -> None:
    """Запись о том, как материал выглядел в этот момент."""
    conn.execute(
        "INSERT INTO item_revisions(item_id, seen_at, title, length, digest) "
        "VALUES(?,?,?,?,?)",
        (item_id, now(), title, int(length), digest),
    )
    conn.commit()


def last_revision(conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT title, length, digest, seen_at FROM item_revisions "
        "WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def mark_checked(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE items SET checked_at = ?, checks = checks + 1 WHERE id = ?",
        (now(), item_id),
    )
    conn.commit()

__all__ = ("CACHE_KB", "DEFAULT_PATH", "LATE_COLUMNS", "SCHEMA", "STAMPS", "connect", "ensure", "fill",
           "enrichment", "last_revision", "latency_of", "latency_rows", "mark_checked", "mark_dup", "neighbours", "now", "published", "remember", "save_enrichment", "revise", "save_vector", "set_fingerprint", "stamp", "vector_of")
