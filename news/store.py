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
    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,           -- случайные 24 байта
        user_id    INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_codes (
        code       TEXT PRIMARY KEY,           -- одноразовый, пять минут
        user_id    INTEGER NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_state (
        code    TEXT PRIMARY KEY,               -- код издания из sources.py
        enabled INTEGER NOT NULL DEFAULT 1,     -- опрашивать ли
        every   INTEGER NOT NULL DEFAULT 0      -- свой интервал в секундах; 0 — как в коде
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        id       INTEGER PRIMARY KEY,
        item_id  INTEGER NOT NULL,
        taken_at TEXT NOT NULL,              -- когда страница была у нас в руках
        sha256   TEXT NOT NULL,              -- отпечаток исходного HTML
        size     INTEGER NOT NULL DEFAULT 0, -- размер до сжатия
        packed   BLOB NOT NULL,              -- сам HTML, gzip
        UNIQUE(item_id, sha256)              -- одинаковая страница хранится один раз
    )
    """,
    "CREATE INDEX IF NOT EXISTS snapshots_item ON snapshots(item_id, taken_at)",
    """
    CREATE TABLE IF NOT EXISTS saved_queries (
        id            INTEGER PRIMARY KEY,
        user_id       INTEGER NOT NULL,
        title         TEXT NOT NULL DEFAULT '',
        query         TEXT NOT NULL,
        source        TEXT NOT NULL DEFAULT '',   -- код издания; пусто — все
        topic_id      INTEGER,
        only_original INTEGER NOT NULL DEFAULT 0,
        only_revised  INTEGER NOT NULL DEFAULT 0,
        notify        INTEGER NOT NULL DEFAULT 1, -- слать ли находки в бот
        last_item_id  INTEGER NOT NULL DEFAULT 0, -- по какой id уже отдано
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS saved_queries_user ON saved_queries(user_id)",
    """
    CREATE TABLE IF NOT EXISTS entities (
        id         INTEGER PRIMARY KEY,
        kind       TEXT NOT NULL,              -- организация | человек | деньги | место
        name       TEXT NOT NULL,              -- как встретилось в первый раз
        norm       TEXT NOT NULL,              -- ключ склейки написаний
        created_at TEXT NOT NULL,
        UNIQUE(kind, norm)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mentions (
        item_id   INTEGER NOT NULL,
        entity_id INTEGER NOT NULL,
        in_title  INTEGER NOT NULL DEFAULT 0,  -- в заголовке вес другой
        times     INTEGER NOT NULL DEFAULT 1,
        UNIQUE(item_id, entity_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS mentions_entity ON mentions(entity_id)",
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
    # Шаг 10: текст ревизии (раньше хранился только отпечаток) и снятие с
    # публикации. Обе колонки нужны на базах, которые уже работают на сервере.
    ("item_revisions", "text", "TEXT NOT NULL DEFAULT ''"),
    ("items", "gone_at", "TEXT"),
    ("items", "gone_code", "INTEGER"),
    # Какие виды сообщений человек согласен получать. Пусто — все.
    ("users", "kinds", "TEXT NOT NULL DEFAULT ''"),
    # Из каких изданий человек согласен получать сообщения. Пусто — из всех.
    ("users", "sources", "TEXT NOT NULL DEFAULT ''"),
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
           digest: str, text: str = "") -> None:
    """Запись о том, как материал выглядел в этот момент.

    С шага 10 храним и сам текст: отпечаток отвечает «изменилось», но не
    отвечает «что именно», а исчезнувшая формулировка и есть наблюдение
    [NEWS-008]. Пустой текст не затирает уже сохранённый.
    """
    conn.execute(
        "INSERT INTO item_revisions(item_id, seen_at, title, length, digest, text) "
        "VALUES(?,?,?,?,?,?)",
        (item_id, now(), title, int(length), digest, text or ""),
    )
    conn.commit()


def revisions(conn: sqlite3.Connection, item_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """История правок материала от старой к новой, вместе с текстами."""
    rows = conn.execute(
        "SELECT id, seen_at, title, length, digest, text FROM item_revisions "
        "WHERE item_id = ? ORDER BY id ASC LIMIT ?",
        (item_id, int(limit)),
    ).fetchall()
    return [dict(row) for row in rows]


def last_revision(conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT title, length, digest, seen_at FROM item_revisions "
        "WHERE item_id = ? ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def save_snapshot(conn: sqlite3.Connection, item_id: int, page: str) -> str:
    """Доказательная копия страницы: gzip плюс sha256. Возвращает отпечаток.

    Пересказ проверить нельзя, а копию — можно: издание снимает материал, а у
    нас остаётся то, что мы видели своими глазами, со временем получения
    [NEWS-007]. Одинаковая страница второй раз не пишется.
    """
    import gzip  # noqa: PLC0415 — нужен только здесь
    import hashlib  # noqa: PLC0415

    raw = (page or "").encode("utf-8", "replace")
    if not raw:
        return ""
    digest = hashlib.sha256(raw).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO snapshots(item_id, taken_at, sha256, size, packed) "
        "VALUES(?,?,?,?,?)",
        (item_id, now(), digest, len(raw), gzip.compress(raw, 6)),
    )
    conn.commit()
    return digest


def snapshots(conn: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
    """Список копий без самих страниц: время, отпечаток, размер."""
    rows = conn.execute(
        "SELECT id, taken_at, sha256, size FROM snapshots WHERE item_id = ? "
        "ORDER BY taken_at ASC",
        (item_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def snapshot_page(conn: sqlite3.Connection, snapshot_id: int) -> str:
    """Сама сохранённая страница. Нет такой — пустая строка, не исключение."""
    import gzip  # noqa: PLC0415

    row = conn.execute(
        "SELECT packed FROM snapshots WHERE id = ?", (snapshot_id,)
    ).fetchone()
    if row is None:
        return ""
    return gzip.decompress(bytes(row["packed"])).decode("utf-8", "replace")


def mark_gone(conn: sqlite3.Connection, item_id: int, code: int) -> None:
    """Материал снят с публикации: код ответа и время, когда мы это увидели.

    Время — наше наблюдение, а не момент снятия: между ними наш интервал
    перечитывания, и выдавать одно за другое нельзя [NEWS-001].
    """
    conn.execute(
        "UPDATE items SET gone_at = COALESCE(gone_at, ?), gone_code = ? WHERE id = ?",
        (now(), int(code), item_id),
    )
    conn.commit()


def revive(conn: sqlite3.Connection, item_id: int) -> None:
    """Страница снова отвечает: отметку о снятии снимаем."""
    conn.execute("UPDATE items SET gone_at = NULL, gone_code = NULL WHERE id = ?", (item_id,))
    conn.commit()


def gone(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    """Снятые с публикации, самые свежие первыми."""
    rows = conn.execute(
        "SELECT id, url, source, title, published_at, listed_at, gone_at, gone_code "
        "FROM items WHERE gone_at IS NOT NULL ORDER BY gone_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def add_query(conn: sqlite3.Connection, user_id: int, query: str, *, title: str = "",
              source: str = "", topic_id: int | None = None, only_original: bool = False,
              only_revised: bool = False, notify: bool = True) -> int:
    """Сохранённый запрос. Точка отсчёта — последний существующий материал.

    Иначе первая же проверка вывалила бы человеку весь архив по слову
    «тариф»: подписка обязана говорить о новом, а не о прошлом [NEWS-004].
    """
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS last FROM items").fetchone()
    cursor = conn.execute(
        "INSERT INTO saved_queries(user_id, title, query, source, topic_id, only_original, "
        "only_revised, notify, last_item_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user_id, title or query, query, source, topic_id, 1 if only_original else 0,
         1 if only_revised else 0, 1 if notify else 0, int(row["last"]), now()),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def queries(conn: sqlite3.Connection, user_id: int | None = None) -> list[dict[str, Any]]:
    """Сохранённые запросы одного человека или все — для фонового обхода."""
    if user_id is None:
        rows = conn.execute("SELECT * FROM saved_queries ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM saved_queries WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def drop_query(conn: sqlite3.Connection, query_id: int, user_id: int) -> bool:
    """Удаляет свой запрос. Чужой не трогает даже по верному номеру."""
    cursor = conn.execute(
        "DELETE FROM saved_queries WHERE id = ? AND user_id = ?", (query_id, user_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def toggle_notify(conn: sqlite3.Connection, query_id: int, user_id: int) -> bool:
    """Переключает уведомления по запросу. Возвращает новое состояние.

    Отдельная функция, а не «поставить значение», потому что кнопка в вебе
    без JavaScript умеет только послать факт нажатия [CORE-025].
    """
    row = conn.execute(
        "SELECT notify FROM saved_queries WHERE id = ? AND user_id = ?", (query_id, user_id)
    ).fetchone()
    if row is None:
        return False
    state = 0 if row["notify"] else 1
    conn.execute("UPDATE saved_queries SET notify = ? WHERE id = ?", (state, query_id))
    conn.commit()
    return bool(state)


def mark_query_seen(conn: sqlite3.Connection, query_id: int, last_item_id: int) -> None:
    """Запоминает, по какой материал запрос уже отдан. Назад не откатываем."""
    conn.execute(
        "UPDATE saved_queries SET last_item_id = MAX(last_item_id, ?) WHERE id = ?",
        (int(last_item_id), query_id),
    )
    conn.commit()


# Виды сообщений, которые можно выключить в интерфейсе. «Изменение» и
# «запрос» намеренно в списке: человек вправе не хотеть досылок.
KINDS = ("сырое", "дополнение", "изменение", "тоже_написали", "запрос")


def source_states(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Состояние источников из базы. Чего нет в базе — включено по умолчанию."""
    rows = conn.execute("SELECT code, enabled, every FROM source_state").fetchall()
    return {
        str(row["code"]): {"включён": bool(row["enabled"]), "интервал": int(row["every"] or 0)}
        for row in rows
    }


def set_source(conn: sqlite3.Connection, code: str, *, enabled: bool | None = None,
               every: int | None = None) -> None:
    """Включить, выключить или задать свой интервал опроса издания."""
    conn.execute("INSERT OR IGNORE INTO source_state(code) VALUES(?)", (code,))
    if enabled is not None:
        conn.execute("UPDATE source_state SET enabled = ? WHERE code = ?",
                     (1 if enabled else 0, code))
    if every is not None:
        conn.execute("UPDATE source_state SET every = ? WHERE code = ?",
                     (max(0, int(every)), code))
    conn.commit()


def source_enabled(conn: sqlite3.Connection, code: str) -> bool:
    row = conn.execute("SELECT enabled FROM source_state WHERE code = ?", (code,)).fetchone()
    return True if row is None else bool(row["enabled"])


def source_every(conn: sqlite3.Connection, code: str) -> int:
    """Свой интервал издания в секундах. 0 — брать тот, что в коде."""
    row = conn.execute("SELECT every FROM source_state WHERE code = ?", (code,)).fetchone()
    return int(row["every"] or 0) if row is not None else 0


def kinds_of(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """Какие виды сообщений человек получает. Пустая настройка — все."""
    row = conn.execute("SELECT kinds FROM users WHERE id = ?", (int(user_id),)).fetchone()
    raw = str(row["kinds"]).strip() if row is not None and row["kinds"] else ""
    if not raw:
        return set(KINDS)
    return {part.strip() for part in raw.split(",") if part.strip()}


def set_kinds(conn: sqlite3.Connection, user_id: int, kinds: Any) -> None:
    """Сохраняет выбор видов. Все виды сразу сохраняются как «пусто»."""
    chosen = [kind for kind in KINDS if kind in set(kinds or ())]
    value = "" if len(chosen) == len(KINDS) else ",".join(chosen)
    conn.execute("UPDATE users SET kinds = ? WHERE id = ?", (value, int(user_id)))
    conn.commit()


def set_quiet(conn: sqlite3.Connection, user_id: int, since: str, until: str) -> None:
    """Тихие часы. Непонятное время не сохраняется, а не ломает настройку."""
    import re  # noqa: PLC0415

    clock = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
    first = (since or "").strip()
    second = (until or "").strip()
    if first and not clock.match(first):
        return
    if second and not clock.match(second):
        return
    conn.execute("UPDATE users SET quiet_from = ?, quiet_to = ? WHERE id = ?",
                 (first, second, int(user_id)))
    conn.commit()


def user_sources(conn: sqlite3.Connection, user_id: int) -> set[str]:
    """Из каких изданий человек получает сообщения. Пустая настройка — из всех.

    Пусто значит «все», а не «ни одного»: у старых записей колонки не было, и
    трактовать её отсутствие как запрет означало бы молча выключить рассылку
    [NEWS-001].
    """
    row = conn.execute("SELECT sources FROM users WHERE id = ?", (int(user_id),)).fetchone()
    raw = str(row["sources"] or "") if row is not None else ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def set_user_sources(conn: sqlite3.Connection, user_id: int, codes: Any) -> None:
    """Сохраняет список изданий для отдачи. Все отмечены — храним пусто."""
    chosen = [str(code).strip() for code in (codes or []) if str(code).strip()]
    conn.execute("UPDATE users SET sources = ? WHERE id = ?",
                 (",".join(sorted(set(chosen))), int(user_id)))
    conn.commit()


def source_allowed(conn: sqlite3.Connection, user_id: int, code: str) -> bool:
    """Согласен ли человек получать сообщения из этого издания."""
    chosen = user_sources(conn, user_id)
    return not chosen or str(code) in chosen


def topics_of(conn: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    """Темы человека вместе с настройками отдачи."""
    rows = conn.execute(
        "SELECT * FROM topics WHERE user_id = ? ORDER BY id", (int(user_id),)
    ).fetchall()
    return [dict(row) for row in rows]


def set_topic_delivery(conn: sqlite3.Connection, topic_id: int, user_id: int, *,
                       enabled: bool, sources: Any = None) -> bool:
    """Отдавать ли тему в бот и из каких изданий. Чужую тему не трогает."""
    codes = None
    if sources is not None:
        codes = ",".join(sorted({str(code).strip() for code in sources if str(code).strip()}))
    if codes is None:
        cursor = conn.execute("UPDATE topics SET enabled = ? WHERE id = ? AND user_id = ?",
                              (1 if enabled else 0, int(topic_id), int(user_id)))
    else:
        cursor = conn.execute(
            "UPDATE topics SET enabled = ?, sources = ? WHERE id = ? AND user_id = ?",
            (1 if enabled else 0, codes, int(topic_id), int(user_id)),
        )
    conn.commit()
    return cursor.rowcount > 0


def entities_top(conn: sqlite3.Connection, *, kind: str = "", query: str = "",
                 days: int = 30, limit: int = 100) -> list[dict[str, Any]]:
    """Кого чаще всего упоминают за окно. Пустой список — мы не видели."""
    where = ["m.item_id = i.id"]
    params: list[Any] = []
    if kind:
        where.append("e.kind = ?")
        params.append(kind)
    if query:
        where.append("e.norm LIKE ?")
        params.append("%{}%".format(str(query).lower().replace("ё", "е")))
    if days:
        where.append("COALESCE(i.published_at, i.listed_at) >= datetime('now', ?)")
        params.append("-{} days".format(int(days)))
    rows = conn.execute(
        "SELECT e.id, e.kind, e.name, COUNT(DISTINCT i.id) AS материалов, "
        "SUM(m.in_title) AS в_заголовках, MAX(COALESCE(i.published_at, i.listed_at)) AS последний "
        "FROM entities e JOIN mentions m ON m.entity_id = e.id JOIN items i "
        "WHERE {} GROUP BY e.id ORDER BY материалов DESC, последний DESC LIMIT ?".format(
            " AND ".join(where)),
        (*params, max(1, min(int(limit), 500))),
    ).fetchall()
    return [dict(row) for row in rows]


def entity(conn: sqlite3.Connection, entity_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM entities WHERE id = ?", (int(entity_id),)).fetchone()
    return dict(row) if row is not None else None


def entity_items(conn: sqlite3.Connection, entity_id: int,
                 limit: int = 50) -> list[dict[str, Any]]:
    """Материалы, где сущность встретилась. Свежие сверху, со ссылкой."""
    rows = conn.execute(
        "SELECT i.id, i.title, i.url, i.source, i.published_at, i.listed_at, i.gone_at, "
        "m.in_title, m.times FROM mentions m JOIN items i ON i.id = m.item_id "
        "WHERE m.entity_id = ? ORDER BY COALESCE(i.published_at, i.listed_at) DESC LIMIT ?",
        (int(entity_id), max(1, min(int(limit), 200))),
    ).fetchall()
    return [dict(row) for row in rows]


def entity_days(conn: sqlite3.Connection, entity_id: int, days: int = 30) -> list[dict[str, Any]]:
    """Упоминания по дням — для полоски всплеска на карточке."""
    rows = conn.execute(
        "SELECT date(COALESCE(i.published_at, i.listed_at)) AS день, COUNT(*) AS сколько "
        "FROM mentions m JOIN items i ON i.id = m.item_id WHERE m.entity_id = ? "
        "AND COALESCE(i.published_at, i.listed_at) >= datetime('now', ?) "
        "GROUP BY день ORDER BY день",
        (int(entity_id), "-{} days".format(int(days))),
    ).fetchall()
    return [dict(row) for row in rows]


def bursts(conn: sqlite3.Connection, *, window: int = 2, background: int = 30,
           limit: int = 30) -> list[dict[str, Any]]:
    """Всплески: о ком вдруг стали писать чаще обычного.

    Считаем просто: упоминания за короткое окно против среднесуточного фона
    за месяц. Это наблюдение, а не объяснение: всплеск говорит «стали писать»,
    а не «что-то случилось» [NEWS-008]. Сущности, которых до этого не было
    вовсе, фоном не считаются нулём — у них фон неизвестен [NEWS-001], и они
    помечаются отдельно.
    """
    rows = conn.execute(
        "SELECT e.id, e.kind, e.name, "
        "SUM(CASE WHEN COALESCE(i.published_at, i.listed_at) >= datetime('now', ?) "
        "THEN 1 ELSE 0 END) AS сейчас, COUNT(*) AS всего "
        "FROM entities e JOIN mentions m ON m.entity_id = e.id "
        "JOIN items i ON i.id = m.item_id "
        "WHERE COALESCE(i.published_at, i.listed_at) >= datetime('now', ?) "
        "GROUP BY e.id HAVING сейчас >= 2 ORDER BY сейчас DESC LIMIT ?",
        ("-{} days".format(int(window)), "-{} days".format(int(background)),
         max(1, min(int(limit), 100))),
    ).fetchall()
    out = []
    for row in rows:
        сейчас = int(row["сейчас"])
        всего = int(row["всего"])
        фон = (всего - сейчас) / max(1, background - window)
        out.append({
            "id": int(row["id"]), "вид": row["kind"], "имя": row["name"],
            "сейчас": сейчас, "за_месяц": всего,
            "фон": round(фон, 2),
            "во_сколько_раз": round(сейчас / window / фон, 1) if фон > 0 else None,
            "новое": фон == 0,
        })
    out.sort(key=lambda item: (item["во_сколько_раз"] or 999, item["сейчас"]), reverse=True)
    return out


def mark_checked(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE items SET checked_at = ?, checks = checks + 1 WHERE id = ?",
        (now(), item_id),
    )
    conn.commit()

__all__ = ("CACHE_KB", "DEFAULT_PATH", "KINDS", "LATE_COLUMNS", "SCHEMA", "STAMPS", "add_query",
           "connect", "drop_query", "ensure", "fill", "enrichment", "gone", "last_revision",
           "latency_of", "latency_rows", "mark_checked", "mark_dup", "mark_gone",
           "mark_query_seen", "neighbours", "now", "published", "queries", "remember",
           "revise", "revisions", "revive", "save_enrichment", "save_snapshot", "save_vector",
           "set_fingerprint", "set_kinds", "set_quiet", "set_source", "snapshot_page", "snapshots",
           "source_enabled", "source_every", "source_states", "kinds_of", "stamp",
           "set_topic_delivery", "set_user_sources", "source_allowed", "topics_of",
           "toggle_notify", "user_sources", "vector_of", "bursts", "entities_top", "entity",
           "entity_days", "entity_items")
