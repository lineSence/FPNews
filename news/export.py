"""Выгрузка: те же строки ленты, но файлом.

Зачем. Архив нужен не только для чтения глазами: сводную таблицу уносят в
Excel, в блокнот следователя, в отдельную заметку. Пока выгрузки нет, люди
копируют строки руками и теряют ссылки на оригиналы — а ссылка и есть
главное, что мы обязаны отдать вместе с фактом [NEWS-007].

Формат по умолчанию — CSV с разделителем «точка с запятой» и BOM: так
русский Excel открывает файл без плясок с кодировками. JSON — для тех, кто
будет считать сам.

Потолок строк тот же, что на странице: выгрузка не должна превращаться в
способ выгрести базу одним запросом на одном ядре [CORE-025].

Про формулы. Excel и LibreOffice считают ячейку, начинающуюся с «=», «+»,
«-» или «@», формулой и выполняют её при открытии файла. Заголовки мы берём
с чужих сайтов, то есть в ячейку попадает строка, которую писали не мы: это
готовая инъекция формулы [CORE-016]. Поэтому такие значения уезжают с
одиночной кавычкой впереди — таблица покажет текст, а не выполнит его.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from . import stream

MAX_ROWS = 2000
# Знаки, с которых таблица начинает считать ячейку формулой. Табуляция и
# возврат каретки в списке потому, что ими можно сдвинуть начало строки.
ФОРМУЛА = ("=", "+", "-", "@", "\t", "\r")
COLUMNS = ("id", "заголовок", "источник", "опубликовано", "замечено", "url",
           "перепечатка_из", "правок", "снято")


def rows(conn: Any, query: dict[str, str], many: Any = None) -> list[dict[str, Any]]:
    """Строки под тот же фильтр, что и на странице ленты."""
    flt = stream.read(query, many)
    out: list[dict[str, Any]] = []
    страница = flt.page
    while len(out) < MAX_ROWS:
        flt.page = страница
        порция = stream.select(conn, flt)
        if not порция:
            break
        out.extend(порция[:stream.PER_PAGE])
        if len(порция) <= stream.PER_PAGE:
            break
        страница += 1
    return out[:MAX_ROWS]


def safe_cell(value: Any) -> str:
    """Значение ячейки, которое таблица не станет считать формулой."""
    текст = "" if value is None else str(value)
    return "'" + текст if текст[:1] in ФОРМУЛА else текст


def as_csv(data: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter=";", extrasaction="ignore",
                            lineterminator="\r\n")
    writer.writeheader()
    for row in data:
        writer.writerow({name: safe_cell(row.get(name, "")) for name in COLUMNS})
    # BOM — чтобы Excel не открыл кириллицу кракозябрами.
    return "\ufeff" + buffer.getvalue()


def as_json(data: list[dict[str, Any]]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=1)


def make(conn: Any, query: dict[str, str], kind: str = "csv",
         many: Any = None) -> tuple[str, str, str]:
    """Тело, тип содержимого и имя файла."""
    data = rows(conn, query, many)
    if str(kind).lower() == "json":
        return as_json(data), "application/json; charset=utf-8", "лента.json"
    return as_csv(data), "text/csv; charset=utf-8", "лента.csv"


DOSSIER_ITEMS = 200


def dossier(conn: Any, query: dict[str, str], many: Any = None) -> tuple[str, str]:
    """Досье одним файлом: заголовок, счёт, хронология, правки, соседи.

    Зачем отдельно от таблицы. Таблица хороша, когда считают; когда пишут
    текст, нужна связная справка: что это, за какой срок, кто писал, что
    менялось. Собираем её из того же архива и обязательно со ссылками на
    оригиналы [NEWS-007].

    Формат — Markdown: его читают глазами, открывают где угодно и вставляют
    в заметку без потери ссылок. Файл не пересказывает материалы и ничего не
    выводит: это перечень наблюдений, а выводы делает человек [NEWS-008].
    """
    from . import store  # noqa: PLC0415 — нужен только здесь

    номер = str(query.get("сущность") or "").strip()
    заголовок, строки, дни, подпись = "", [], [], ""
    if номер.isdigit():
        карточка = store.entity(conn, int(номер))
        if карточка is None:
            return "", ""
        заголовок = str(карточка["name"])
        подпись = "сущность, вид: {}".format(карточка["kind"])
        строки = store.entity_items(conn, int(номер), DOSSIER_ITEMS)
        дни = store.entity_days(conn, int(номер), 30)
    else:
        заголовок = str(query.get("q") or "").strip()
        if not заголовок:
            return "", ""
        подпись = "запрос по словам"
        строки = rows(conn, dict(query, q=заголовок), many)[:DOSSIER_ITEMS]
    издания: dict[str, int] = {}
    for строка in строки:
        код = str(строка.get("source") or строка.get("источник") or "")
        издания[код] = издания.get(код, 0) + 1
    куски = [
        "# Досье: {}".format(заголовок),
        "",
        "{} · собрано {} · материалов в досье: {}".format(подпись, store.now()[:16],
                                                          len(строки)),
        "",
        "Это перечень того, что мы видели своими глазами, а не полная картина: "
        "чего мы не собирали, того здесь нет [NEWS-001].",
        "",
        "## Кто писал",
        "",
    ]
    from .deliver import LABEL  # noqa: PLC0415 — названия изданий живут там

    куски += ["- {}: {}".format(LABEL.get(код, код or "—"), сколько)
              for код, сколько in sorted(издания.items(), key=lambda пара: -пара[1])]
    if дни:
        куски += ["", "## Упоминания по дням", ""]
        куски += ["- {}: {}".format(день["день"], день["сколько"]) for день in дни]
    куски += ["", "## Хронология", ""]
    for строка in строки:
        когда = str(строка.get("published_at") or строка.get("опубликовано")
                    or строка.get("listed_at") or строка.get("замечено") or "")[:16]
        снято = " · СНЯТО С ПУБЛИКАЦИИ" if строка.get("gone_at") or строка.get("снято") else ""
        куски.append("- {когда} · {издание} · [{название}]({ссылка}){снято}".format(
            когда=когда.replace("T", " ") or "время неизвестно",
            издание=LABEL.get(str(строка.get("source") or строка.get("источник") or ""),
                              "—"),
            название=str(строка.get("title") or строка.get("заголовок") or "без заголовка"),
            ссылка=str(строка.get("url") or ""), снято=снято))
    правки = _dossier_changes(conn, [int(строка["id"]) for строка in строки if строка.get("id")])
    if правки:
        куски += ["", "## Правки и снятия", ""] + правки
    куски += ["", "---", "",
              "Досье собрано из архива FPNews. Каждая строка — ссылка на оригинал; "
              "пересказ здесь ничего не заменяет [NEWS-007]."]
    имя = "досье-{}.md".format("".join(
        знак if знак.isalnum() or знак in "-_" else "-" for знак in заголовок.lower())[:60])
    return "\n".join(куски), имя


def _dossier_changes(conn: Any, номера: list[int], limit: int = 50) -> list[str]:
    """Строки о правках и снятиях для досье. Пусто — значит не видели."""
    if not номера:
        return []
    отобранные = номера[:limit]
    места = ",".join("?" for _ in отобранные)
    строки = []
    for row in conn.execute(
        "SELECT r.item_id, r.seen_at, r.title, i.title AS сейчас FROM item_revisions r "
        "JOIN items i ON i.id = r.item_id WHERE r.item_id IN ({}) "
        "ORDER BY r.seen_at DESC".format(места), отобранные,
    ).fetchall():
        было, стало = str(row["title"] or ""), str(row["сейчас"] or "")
        строки.append("- {} · правка №{}: «{}» → «{}»".format(
            str(row["seen_at"])[:16].replace("T", " "), int(row["item_id"]), было, стало))
    for row in conn.execute(
        "SELECT id, title, gone_at, gone_code FROM items WHERE gone_at IS NOT NULL "
        "AND id IN ({}) ORDER BY gone_at DESC".format(места), отобранные,
    ).fetchall():
        строки.append("- {} · снято №{} (ответ {}): «{}»".format(
            str(row["gone_at"])[:16].replace("T", " "), int(row["id"]),
            int(row["gone_code"] or 0), str(row["title"] or "")))
    return строки


__all__ = ("COLUMNS", "DOSSIER_ITEMS", "MAX_ROWS", "ФОРМУЛА", "as_csv", "as_json",
           "dossier", "make", "rows", "safe_cell")
