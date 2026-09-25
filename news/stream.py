"""Общая лента: отбор, порядок и раскладка по папкам.

Зачем отдельный модуль. Поиск (`news/search.py`) отвечает на вопрос «где
встречается слово», а лента — на другой: «что вообще вышло за это время».
Это разные запросы к одной базе, и смешивать их в одной функции с десятком
необязательных доводов было бы хуже, чем написать сорок строк SQL рядом.

Папки — это раскладка уже отобранного, а не отдельная сущность в базе.
Правило простое: материал кладётся в первую подходящую папку, остальное
идёт в «Прочее». Никакой модели здесь нет и не будет: раскладка стоит в
пути показа страницы, а туда модель не пускают [NEWS-002]. «Умность» тут
только в том, что папки берутся из тем человека и из склейки перепечаток,
то есть из уже сделанных наблюдений, а не из догадок [NEWS-008].

Отсутствие материалов в папке — это отсутствие наших наблюдений, а не
утверждение, что издание молчало [NEWS-001].
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from . import search, store, topics

# Порядок строк. Названия те же, что видит человек в списке на странице.
SORTS = ("новые сверху", "старые сверху", "по изданию", "по задержке")
# Раскладка по папкам. «По теме» и «по сюжету» — те самые умные папки.
GROUPS = ("без папок", "по дате", "по изданию", "по теме", "по сюжету")
# Период в днях. Ноль — без ограничения.
PERIODS = (("всё время", 0), ("сутки", 1), ("три дня", 3), ("неделя", 7), ("месяц", 30))
PERIOD_DAYS = dict(PERIODS)
# Потолок строк на страницу: одно ядро и человек, который столько не прочтёт.
PER_PAGE = 50
MAX_ROWS = 200
# Потолок номера страницы. Без него «стр=99999999999999999999» из чужой
# ссылки уходит в SQLite числом, которое туда не влезает, и страница падает.
MAX_PAGE = 1000

ORDER = {
    "новые сверху": "COALESCE(i.published_at, i.listed_at) DESC, i.id DESC",
    "старые сверху": "COALESCE(i.published_at, i.listed_at) ASC, i.id ASC",
    "по изданию": "i.source ASC, COALESCE(i.published_at, i.listed_at) DESC",
    # Задержка издания: сколько прошло от публикации до нашего обнаружения.
    # Где времени публикации нет, строка уходит вниз, а не притворяется нулём.
    "по задержке": ("(julianday(i.listed_at) - julianday(i.published_at)) IS NULL, "
                    "(julianday(i.listed_at) - julianday(i.published_at)) DESC"),
}


@dataclass
class Filter:
    """Что человек выбрал на странице ленты."""

    words: str = ""
    sources: tuple[str, ...] = ()
    topic_id: int = 0
    period: str = "неделя"
    since: str = ""
    until: str = ""
    sort: str = "новые сверху"
    group: str = "без папок"
    only_original: bool = False
    page: int = 1
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def offset(self) -> int:
        return (min(MAX_PAGE, max(1, self.page)) - 1) * PER_PAGE


def read(query: dict[str, str], many: Any = None) -> Filter:
    """Фильтр из строки запроса. Всё непонятное — значение по умолчанию.

    Чужая ссылка не должна ни падать, ни превращаться в выборку всего архива:
    неизвестный порядок — «новые сверху», неизвестный период — неделя.
    """
    chosen = tuple(many("издание")) if callable(many) else tuple(
        code for code in str(query.get("издание", "") or "").split(",") if code.strip()
    )
    sort = str(query.get("порядок", "") or "")
    group = str(query.get("папки", "") or "")
    period = str(query.get("период", "") or "")
    try:
        page = min(MAX_PAGE, max(1, int(str(query.get("стр", "1") or "1"))))
    except ValueError:
        page = 1
    try:
        topic_id = max(0, min(2**31, int(str(query.get("тема", "0") or "0"))))
    except ValueError:
        topic_id = 0
    return Filter(
        words=str(query.get("q", "") or "").strip(),
        sources=tuple(dict.fromkeys(code.strip() for code in chosen if code.strip())),
        topic_id=topic_id,
        period=period if period in PERIOD_DAYS else "неделя",
        since=str(query.get("с", "") or "").strip(),
        until=str(query.get("по", "") or "").strip(),
        sort=sort if sort in SORTS else SORTS[0],
        group=group if group in GROUPS else GROUPS[0],
        only_original=bool(query.get("оригиналы")),
        page=page,
    )


def link(flt: Filter, **changes: Any) -> str:
    """Адрес ленты с изменённым одним полем — для ссылок «дальше» и папок."""
    data = {
        "q": flt.words,
        "тема": str(flt.topic_id or ""),
        "период": flt.period,
        "с": flt.since,
        "по": flt.until,
        "порядок": flt.sort,
        "папки": flt.group,
        "оригиналы": "1" if flt.only_original else "",
        "стр": str(flt.page),
    }
    data.update({key: str(value) for key, value in changes.items()})
    pairs = [(key, value) for key, value in data.items() if value not in ("", "0", None)]
    pairs.extend(("издание", code) for code in flt.sources)
    return "/лента?" + urllib.parse.urlencode(pairs)


def _words_of_topic(conn: Any, topic_id: int) -> tuple[str, list[str]]:
    row = conn.execute("SELECT words, sources FROM topics WHERE id = ?", (topic_id,)).fetchone()
    if row is None:
        return "", []
    codes = [code.strip() for code in str(row["sources"] or "").split(",") if code.strip()]
    parts = [search.prepare(word) for word in str(row["words"] or "").split(",")]
    parts = [part for part in parts if part]
    return "({})".format(" OR ".join(parts)) if parts else "", codes


def select(conn: Any, flt: Filter) -> list[dict[str, Any]]:
    """Материалы под фильтр. Пустой список — мы ничего такого не видели."""
    where: list[str] = []
    params: list[Any] = []
    expression = ""
    if flt.words:
        expression = search.prepare(flt.words)
        if not expression:
            return []
    codes = list(flt.sources)
    if flt.topic_id:
        group, topic_codes = _words_of_topic(conn, flt.topic_id)
        if not group:
            return []
        expression = "{} AND {}".format(expression, group) if expression else group
        if topic_codes and not codes:
            codes = topic_codes
    if expression:
        search.ensure_index(conn)
        source = "items_fts JOIN items i ON i.id = items_fts.rowid"
        where.append("items_fts MATCH ?")
        params.append(expression)
    else:
        source = "items i"
    if codes:
        where.append("i.source IN ({})".format(",".join("?" * len(codes))))
        params.extend(codes)
    days = PERIOD_DAYS.get(flt.period, 0)
    if days and not (flt.since or flt.until):
        where.append("COALESCE(i.published_at, i.listed_at) >= datetime('now', ?)")
        params.append("-{} days".format(int(days)))
    if flt.since:
        where.append("date(COALESCE(i.published_at, i.listed_at)) >= date(?)")
        params.append(flt.since)
    if flt.until:
        where.append("date(COALESCE(i.published_at, i.listed_at)) <= date(?)")
        params.append(flt.until)
    if flt.only_original:
        where.append("i.dup_of IS NULL")
    sql = (
        "SELECT i.id, i.url, i.source, i.title, i.lead, i.published_at, i.listed_at, i.dup_of, "
        "(SELECT COUNT(*) FROM item_revisions r WHERE r.item_id = i.id) AS revisions, "
        "i.gone_at FROM {source}{where} ORDER BY {order} LIMIT ? OFFSET ?"
    ).format(
        source=source,
        where=(" WHERE " + " AND ".join(where)) if where else "",
        order=ORDER.get(flt.sort, ORDER["новые сверху"]),
    )
    params.append(min(PER_PAGE + 1, MAX_ROWS))
    params.append(flt.offset)
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "id": int(row["id"]),
            "url": row["url"],
            "источник": row["source"],
            "заголовок": row["title"],
            "лид": row["lead"],
            "опубликовано": row["published_at"],
            "замечено": row["listed_at"],
            "перепечатка_из": row["dup_of"],
            "правок": int(row["revisions"]),
            "снято": row["gone_at"],
        }
        for row in rows
    ]


def folders(conn: Any, rows: list[dict[str, Any]], flt: Filter,
            user_id: int) -> list[tuple[str, list[dict[str, Any]]]]:
    """Раскладка строк по папкам. Порядок папок — по числу материалов."""
    if flt.group == "по дате":
        return _by_key(rows, lambda row: str(row["опубликовано"] or row["замечено"] or "")[:10]
                       or "без даты", sort_by_size=False)
    if flt.group == "по изданию":
        return _by_key(rows, lambda row: str(row["источник"]))
    if flt.group == "по сюжету":
        return _by_story(rows)
    if flt.group == "по теме":
        return _by_topic(conn, rows, user_id)
    return [("", rows)]


def _by_key(rows: list[dict[str, Any]], key: Any, sort_by_size: bool = True
            ) -> list[tuple[str, list[dict[str, Any]]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        out.setdefault(key(row), []).append(row)
    pairs = list(out.items())
    if sort_by_size:
        pairs.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    else:
        pairs.sort(key=lambda pair: pair[0], reverse=True)
    return pairs


def _by_story(rows: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Папка на сюжет: перепечатки ложатся к оригиналу.

    Склейку делает шаг дедупликации, здесь мы её только показываем. Материал
    без перепечаток остаётся папкой из одной строки — прятать его нельзя,
    иначе лента начнёт умалчивать о половине выхода.
    """
    known = {row["id"]: row for row in rows}
    out: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        head = int(row["перепечатка_из"] or row["id"])
        out.setdefault(head, []).append(row)
    pairs = []
    for head, group in out.items():
        first = known.get(head) or group[0]
        pairs.append((str(first["заголовок"] or "без заголовка"), group))
    pairs.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    return pairs


def _by_topic(conn: Any, rows: list[dict[str, Any]],
              user_id: int) -> list[tuple[str, list[dict[str, Any]]]]:
    """Папки по темам человека. Что ни во что не попало — «Прочее».

    Строка кладётся в первую подходящую папку, а не во все сразу: иначе одна
    новость размножилась бы по экрану и сломала счёт материалов.
    """
    mine = [topic for topic in store.topics_of(conn, user_id) if topic.get("enabled", 1)]
    out: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        title = str(row["заголовок"] or "")
        lead = str(row["лид"] or "")
        where = "Прочее"
        for topic in mine:
            words = topics.parse_words(topic.get("words") or "")
            if words and (topics.matched(words, title) or topics.matched(words, lead)):
                where = str(topic.get("title") or "тема")
                break
        out.setdefault(where, []).append(row)
    pairs = [(name, group) for name, group in out.items() if name != "Прочее"]
    pairs.sort(key=lambda pair: (-len(pair[1]), pair[0]))
    if "Прочее" in out:
        pairs.append(("Прочее", out["Прочее"]))
    return pairs


__all__ = ("GROUPS", "MAX_PAGE", "MAX_ROWS", "ORDER", "PERIODS", "PERIOD_DAYS", "PER_PAGE", "SORTS",
           "Filter", "folders", "link", "read", "select")
