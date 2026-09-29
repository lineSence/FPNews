"""Поиск по архиву, карточка материала и сборка сюжета.

Индекс — FTS5 во внешнем содержимом поверх `items`: тексты не
дублируются, иначе база бы выросла вдвое на ровном месте [CORE-025].
Создаётся лениво: база на сервере уже работает, и `CREATE TABLE IF NOT
EXISTS` в `store.SCHEMA` её не догонит — та же логика, что у `LATE_COLUMNS`.

Разделение наблюдения и оценки сохраняется [NEWS-008]: отсюда возвращаются
только зафиксированные факты (кто, когда, сколько правок), без выводов
модели. Отсутствующее время — `None`, а не ноль [NEWS-001].
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from . import scope

# Потолок строк на один запрос: одно ядро и 500 МБ не терпят выборки
# «всё, что нашлось» [CORE-025].
MAX_LIMIT = 200
# С этой длины слово ищется по префиксу: русские окончания иначе мешают,
# а полноценный стеммер тянет за собой зависимость.
PREFIX_FROM = 4

UPDATE_OF = re.compile(r"AFTER\s+UPDATE\s+OF\b", re.I)

INDEX_SQL = (
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
        title, lead, body,
        content='items', content_rowid='id',
        tokenize="unicode61 remove_diacritics 2"
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_fts_ai AFTER INSERT ON items BEGIN
        INSERT INTO items_fts(rowid, title, lead, body)
        VALUES (new.id, new.title, new.lead, new.body);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_fts_ad AFTER DELETE ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, lead, body)
        VALUES ('delete', old.id, old.title, old.lead, old.body);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS items_fts_au AFTER UPDATE OF title, lead, body ON items BEGIN
        INSERT INTO items_fts(items_fts, rowid, title, lead, body)
        VALUES ('delete', old.id, old.title, old.lead, old.body);
        INSERT INTO items_fts(rowid, title, lead, body)
        VALUES (new.id, new.title, new.lead, new.body);
    END
    """,
)


def ensure_index(conn: sqlite3.Connection) -> None:
    """Создаёт индекс и триггеры, если их ещё нет.

    Первое создание сопровождается `rebuild`: иначе всё, что накопилось до
    появления поиска, молча осталось бы ненаходимым.

    Триггер обновления висит только на `title, lead, body`: отметки
    `sent_at`, `checks`, `checked_at` и прочие служебные поля меняются
    постоянно и не должны переписывать индекс. Старый триггер «на любое
    обновление» из уже созданных баз заменяется здесь же.

    Вызывается на каждом поиске, поэтому в обычном случае это одно чтение
    `sqlite_master` без записи и без `commit`.
    """
    have = {
        str(row[0]): str(row[1] or "")
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name IN "
            "('items_fts', 'items_fts_ai', 'items_fts_ad', 'items_fts_au')"
        )
    }
    if "items_fts_au" in have and not UPDATE_OF.search(have["items_fts_au"]):
        conn.execute("DROP TRIGGER items_fts_au")
        del have["items_fts_au"]
    if len(have) == 4:
        return
    for statement in INDEX_SQL:
        conn.execute(statement)
    if "items_fts" not in have:
        conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")
    conn.commit()


def optimize(conn: sqlite3.Connection) -> None:
    """Сжать индекс. Место вызова — суточное обслуживание, не запрос."""
    ensure_index(conn)
    conn.execute("INSERT INTO items_fts(items_fts) VALUES('optimize')")
    conn.commit()


def prepare(text: str) -> str:
    """Запрос человека — в безопасное выражение FTS5.

    Кавычки, звёздочки и `NEAR` из пользовательской строки — не синтаксис, а
    текст: иначе поиск падает на чужом вводе. Пустой результат означает
    «искать нечего» и наверх не превращается в выборку всего архива.
    """
    parts: list[str] = []
    for raw in re.split(r"[^\w-]+", text or "", flags=re.UNICODE):
        word = raw.strip("-")
        if len(word) < 2:
            continue
        quoted = '"{}"'.format(word)
        parts.append(quoted + "*" if len(word) >= PREFIX_FROM else quoted)
    return " AND ".join(parts)


def _any_of(words: str) -> str:
    parts = [prepare(word) for word in (words or "").split(",")]
    parts = [part for part in parts if part]
    return "({})".format(" OR ".join(parts)) if parts else ""


def search(conn: sqlite3.Connection, query: str, *, source: str = "",
           since: str = "", until: str = "", topic_id: int | None = None,
           only_original: bool = False, only_revised: bool = False,
           limit: int = 50, offset: int = 0,
           user_id: int = 0) -> list[dict[str, Any]]:
    """Материалы по запросу, свежие и точные сверху.

    Заголовок весит больше лида, лид — больше тела: совпадение в теле
    часто случайное. Даты сравниваются по времени публикации, а если его
    нет — по моменту обнаружения.
    """
    ensure_index(conn)
    expression = prepare(query)
    if not expression:
        return []
    params: list[Any] = []
    where = ["items_fts MATCH ?"]

    codes: list[str] = []
    if topic_id is not None:
        # Чужая тема — не наша тема: подборка слов принадлежит одному человеку,
        # и адрес «?тема=N» не даёт права подглядывать чужой профиль.
        свой = user_id == 0 or scope.тему_можно(conn, user_id, int(topic_id))
        topic = None
        if свой:
            topic = conn.execute(
                "SELECT words, sources FROM topics WHERE id = ?", (topic_id,)
            ).fetchone()
        if topic is None:
            return []
        group = _any_of(topic["words"])
        if group:
            expression = "{} AND {}".format(expression, group)
        codes = [code.strip() for code in (topic["sources"] or "").split(",") if code.strip()]
        if codes:
            where.append("i.source IN ({})".format(",".join("?" * len(codes))))
    params.append(expression)
    if topic_id is not None and codes:
        params.extend(codes)

    if source:
        where.append("i.source = ?")
        params.append(source)
    if since:
        where.append("date(COALESCE(i.published_at, i.listed_at)) >= date(?)")
        params.append(since)
    if until:
        where.append("date(COALESCE(i.published_at, i.listed_at)) <= date(?)")
        params.append(until)
    if only_original:
        where.append("i.dup_of IS NULL")
    if only_revised:
        where.append("EXISTS (SELECT 1 FROM item_revisions r WHERE r.item_id = i.id)")
    # Область видимости добавляется последней: её параметры обязаны идти
    # в общем списке после всех слов темы и изданий, а перед LIMIT/OFFSET —
    # иначе порядок «?» и значений разъедется, и фильтр сломается тихо.
    хвост, параметры_области = scope.условие(conn, user_id)
    if хвост:
        where.append(хвост)

    sql = (
        "SELECT i.id, i.url, i.source, i.title, i.lead, i.published_at, i.listed_at, "
        "i.dup_of, (SELECT COUNT(*) FROM item_revisions r WHERE r.item_id = i.id) AS revisions "
        "FROM items_fts JOIN items i ON i.id = items_fts.rowid WHERE {} "
        "ORDER BY bm25(items_fts, 4.0, 2.0, 1.0), COALESCE(i.published_at, i.listed_at) DESC "
        "LIMIT ? OFFSET ?".format(" AND ".join(where))
    )
    params.extend(параметры_области)
    params.append(max(1, min(int(limit), MAX_LIMIT)))
    params.append(max(0, int(offset)))
    rows = conn.execute(sql, params).fetchall()
    return [_row(row) for row in rows]


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "url": row["url"],
        "источник": row["source"],
        "заголовок": row["title"],
        "лид": row["lead"],
        "опубликовано": row["published_at"],
        "замечено": row["listed_at"],
        "перепечатка_из": row["dup_of"],
        "правок": int(row["revisions"]),
    }


def item(conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
    """Материал, его правки и сюжет, в который он входит.

    С шага 10 в каждой правке лежит и текст той редакции, поэтому страница
    показывает не только «когда и какой длины», но и что именно убрали
    [NEWS-008]. Снятие с публикации — отдельные поля: это наблюдение, а не
    отсутствие данных [NEWS-001].
    """
    row = conn.execute(
        "SELECT id, url, source, title, lead, body, published_at, listed_at, "
        "fetched_at, sent_at, dup_of, checks, gone_at, gone_code FROM items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        return None
    revisions = conn.execute(
        "SELECT id, seen_at, title, length, text FROM item_revisions WHERE item_id = ? "
        "ORDER BY seen_at",
        (item_id,),
    ).fetchall()
    copies = conn.execute(
        "SELECT id, taken_at, sha256, size FROM snapshots WHERE item_id = ? ORDER BY taken_at",
        (item_id,),
    ).fetchall()
    return {
        "id": int(row["id"]),
        "url": row["url"],
        "источник": row["source"],
        "заголовок": row["title"],
        "лид": row["lead"],
        "текст": row["body"],
        "опубликовано": row["published_at"],
        "замечено": row["listed_at"],
        "разобрано": row["fetched_at"],
        "отправлено": row["sent_at"],
        "перепечатка_из": row["dup_of"],
        "перечитаний": int(row["checks"]),
        "снято": row["gone_at"],
        "код_снятия": row["gone_code"],
        "правки": [
            {"id": int(rev["id"]), "когда": rev["seen_at"], "заголовок": rev["title"],
             "длина": int(rev["length"]), "текст": rev["text"] or ""}
            for rev in revisions
        ],
        "копии": [
            {"id": int(copy["id"]), "когда": copy["taken_at"], "отпечаток": copy["sha256"],
             "размер": int(copy["size"])}
            for copy in copies
        ],
        "сюжет": story(conn, item_id),
    }


def story(conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
    """Кто написал первым и насколько отстали остальные.

    Точка отсчёта — время публикации первого материала; если издание его
    не сообщило, берётся момент обнаружения, и это отмечено в строке.
    Неизвестное отставание — `None`, не ноль [NEWS-001].
    """
    row = conn.execute("SELECT id, dup_of FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    root = int(row["dup_of"] or row["id"])
    rows = conn.execute(
        "SELECT id, url, source, title, published_at, listed_at, dup_of FROM items "
        "WHERE id = ? OR dup_of = ? ORDER BY COALESCE(published_at, listed_at)",
        (root, root),
    ).fetchall()
    if len(rows) < 2:
        return None
    first = rows[0]
    base = first["published_at"] or first["listed_at"]
    members = []
    for member in rows:
        when = member["published_at"] or member["listed_at"]
        members.append(
            {
                "id": int(member["id"]),
                "url": member["url"],
                "источник": member["source"],
                "заголовок": member["title"],
                "когда": when,
                "по_обнаружению": not member["published_at"],
                "отставание": _seconds(base, when),
                "перепечатка": member["dup_of"] is not None,
            }
        )
    return {"оригинал": root, "первый": first["source"], "участники": members}


def _seconds(first: Any, second: Any) -> float | None:
    from datetime import datetime  # noqa: PLC0415 — нужен только здесь

    if not first or not second:
        return None
    try:
        start = datetime.fromisoformat(str(first))
        end = datetime.fromisoformat(str(second))
    except ValueError:
        return None
    return round((end - start).total_seconds(), 3)


__all__ = ("MAX_LIMIT", "ensure_index", "item", "optimize", "prepare", "search", "story")
