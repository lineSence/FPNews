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
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from . import stream

MAX_ROWS = 2000
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


def as_csv(data: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=COLUMNS, delimiter=";", extrasaction="ignore",
                            lineterminator="\r\n")
    writer.writeheader()
    for row in data:
        writer.writerow({name: row.get(name, "") for name in COLUMNS})
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


__all__ = ("COLUMNS", "MAX_ROWS", "as_csv", "as_json", "make", "rows")
