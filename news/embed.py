"""Векторы: смысловая близость двух новостей одним числом.

Точная склейка (`dedup.py`) ловит перепечатку слово в слово и стоит
микросекунды. Она не ловит главное: Медуза и Фонтанка пишут об одном событии
разными словами, и симхэш этого не видит. Видит вектор.

Четыре решения.

1. **Модель пиннится на всю жизнь базы `[LLM-011]`.** Векторы разных моделей
   несравнимы, и «запасная модель» здесь означает не устойчивость, а молча
   испорченную склейку: расстояния поедут, а ошибка вылезет через неделю. Имя
   модели лежит рядом с каждым вектором, и чужие в сравнение не берутся.
2. **Вектор считается только для разосланных новостей.** Фонтанка даёт сотни
   материалов в сутки, из них человеку ушли единицы. Склеивать то, чего никто
   не видел, незачем — это чистая трата квоты `[CORE-016]`.
3. **Вектор нормирован при записи.** Тогда косинус — это скалярное
   произведение, и сравнение четырёхсот соседей стоит миллисекунды на нашем
   единственном ядре.
4. **Ошибка не ломает ничего.** Нет вектора — работает точная склейка, как
   работала до шага 6 `[CORE-017]`.
"""

from __future__ import annotations

import array
import logging
import math
import os
from typing import Any, Sequence

from . import model, store

log = logging.getLogger("fpnews.embed")

DEFAULT_MODEL = "text-embedding-004"
# Заголовок плюс начало текста: конец материала у разных изданий свой.
MAX_CHARS = 1200


def name() -> str:
    """Имя модели векторов. Меняется только вместе с очисткой таблицы."""
    return (os.getenv("FPNEWS_EMBED_MODEL") or DEFAULT_MODEL).strip()


def pack(values: Sequence[float]) -> bytes:
    """Нормированный вектор в байты: 4 байта на число, 3 КБ на новость."""
    length = math.sqrt(sum(value * value for value in values)) or 1.0
    return array.array("f", [float(value) / length for value in values]).tobytes()


def unpack(blob: bytes) -> array.array:
    out = array.array("f")
    out.frombytes(blob)
    return out


def similarity(first: bytes, second: bytes) -> float:
    """Косинус двух упакованных векторов. Разной длины — ноль, не ошибка."""
    left, right = unpack(first), unpack(second)
    if not left or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))


def text_of(item: dict[str, Any]) -> str:
    return "{}\n{}".format(item.get("title") or "", item.get("body") or "")[:MAX_CHARS].strip()


async def vector(session: Any, text: str, budget: model.Budget) -> bytes:
    """Один вектор через локальный шлюз. Неудача — пустые байты."""
    if not text.strip() or session is None:
        return b""
    if not budget.take_embed():
        log.info("дневной потолок векторов исчерпан")
        return b""
    base = (os.getenv("FPNEWS_LLM_URL") or model.DEFAULT_URL).rstrip("/")
    key = (os.getenv("FPNEWS_LLM_KEY") or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer {}".format(key)
    try:
        response = await session.post(
            base + "/embeddings",
            json={"model": name(), "input": text},
            headers=headers,
            timeout=30.0,
        )
        if response.status_code >= 400:
            log.warning("вектор не получен: %s %s", response.status_code,
                        response.text[:200])
            return b""
        values = response.json()["data"][0]["embedding"]
    except Exception as exc:  # noqa: BLE001 — чужая сеть [CORE-017]
        log.warning("вектор не получен: %s", type(exc).__name__)
        return b""
    if not values:
        return b""
    return pack(values)


async def ensure(session: Any, conn: Any, item: dict[str, Any],
                 budget: model.Budget) -> bytes:
    """Вектор новости: из базы, а если его нет — посчитать и записать."""
    item_id = int(item["id"])
    ready = store.vector_of(conn, item_id, name())
    if ready:
        return ready
    blob = await vector(session, text_of(item), budget)
    if blob:
        store.save_vector(conn, item_id, name(), blob)
    return blob


__all__ = ("DEFAULT_MODEL", "MAX_CHARS", "ensure", "name", "pack", "similarity",
           "text_of", "unpack", "vector")
