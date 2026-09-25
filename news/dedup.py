"""Точная склейка дублей: без сети, без модели, в горячем пути.

Два издания пишут об одном событии, и второе сообщение об этом же — шум.
Но и потерять новость нельзя `[NEWS-004]`, поэтому склейка здесь нарочно
консервативная: совпало наверняка — склеиваем, есть сомнения — считаем
разными новостями, а смысловую близость ищет уже контур обогащения.

Три признака, все считаются локально за микросекунды:

1. канонический адрес — один и тот же материал, пришедший из разных лент;
2. нормализованный заголовок — перепечатка слово в слово;
3. симхэш первых абзацев — перепечатка с мелкой правкой.

Симхэш, а не обычный хэш: обычный ломается от одной запятой, а этот меняется
пропорционально изменению текста, и близость меряется расстоянием Хэмминга.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

WORD = re.compile(r"[0-9a-zа-я]+")
BITS = 64
# Порог близости подобран осторожно: чем он выше, тем больше риск склеить две
# разные новости, а это прячет новость от человека — худший исход [NEWS-004].
MAX_DISTANCE = 3
# Сколько символов текста берём: начало материала повторяется у перепечаток,
# концовка чаще своя (реклама, «читайте также»).
HEAD_CHARS = 600


def normalize_title(title: str) -> str:
    """Заголовок без регистра, ё и пунктуации — для точного сравнения."""
    low = str(title or "").lower().replace("ё", "е")
    return " ".join(WORD.findall(low))


def simhash(text: str) -> str:
    """64-битный симхэш по словам. Пустой текст — пустая строка."""
    words = WORD.findall(str(text or "").lower().replace("ё", "е"))[:400]
    if not words:
        return ""
    vector = [0] * BITS
    for word in words:
        digest = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
        for bit in range(BITS):
            vector[bit] += 1 if digest >> bit & 1 else -1
    value = 0
    for bit in range(BITS):
        if vector[bit] > 0:
            value |= 1 << bit
    return "{:016x}".format(value)


def distance(first: str, second: str) -> int:
    """Расстояние Хэмминга. Несравнимое — максимальное расстояние."""
    if not first or not second:
        return BITS
    return bin(int(first, 16) ^ int(second, 16)).count("1")


def fingerprint(title: str, body: str) -> str:
    return simhash((title or "") + " " + (body or "")[:HEAD_CHARS])


def find(conn: sqlite3.Connection, item: dict[str, Any], hours: int = 12) -> int | None:
    """Ищет, о чём это уже писали. Возвращает id оригинала или None.

    Окно в часах, а не «вся база»: одинаковые заголовки через месяц — это
    разные события («Вечерний Петербург», «Курс доллара вырос»).
    """
    title = normalize_title(item.get("title") or "")
    mark = str(item.get("simhash") or "")
    rows = conn.execute(
        "SELECT id, title, simhash FROM items "
        "WHERE id != ? AND dup_of IS NULL AND listed_at >= datetime('now', ?) "
        "ORDER BY id DESC LIMIT 400",
        (int(item.get("id") or 0), "-{} hours".format(int(hours))),
    ).fetchall()
    for row in rows:
        if title and normalize_title(row["title"]) == title:
            return int(row["id"])
        if mark and distance(mark, str(row["simhash"] or "")) <= MAX_DISTANCE:
            return int(row["id"])
    return None


__all__ = ("BITS", "HEAD_CHARS", "MAX_DISTANCE", "distance", "find", "fingerprint",
           "normalize_title", "simhash")
