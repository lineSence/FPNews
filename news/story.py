"""Смысловая склейка: одно событие, разные слова.

Точная склейка `dedup.py` ловит перепечатку. Здесь ловится другое: Медуза
пишет «Суд арестовал главу комитета», Фонтанка — «Чиновника отправили в СИЗО».
Общих слов почти нет, симхэш далёк, событие одно.

Порядок проверок задан ценой, а не качеством `[CORE-016]`:

1. точная склейка — бесплатно, уже сделана в горячем пути;
2. смысловая — только если точная промолчала и только для новостей, которые
   кому-то ушли.

Порог близости — единственная настройка, и он намеренно высокий. Ошибка
склейки прячет новость от человека, а это худший исход `[NEWS-004]`; ошибка
в другую сторону стоит одного лишнего сообщения. Значение подбирается по
накопленным парам, а не по первому дню `[CORE-019]`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from . import embed, model

log = logging.getLogger("fpnews.story")

# 0.86 — осторожная оценка «то же событие» для многоязычных моделей
# семейства text-embedding. Подлежит уточнению на своих данных.
DEFAULT_THRESHOLD = 0.86
# Ниже этого — просто похожие темы («ещё одна новость про метро»), склеивать
# нельзя, но в лог такие пары попадают: по ним и настраивается порог.
NOTE_THRESHOLD = 0.78
WINDOW_HOURS = 12


@dataclass(frozen=True)
class Match:
    item_id: int
    score: float
    title: str = ""


def threshold() -> float:
    try:
        return float(os.getenv("FPNEWS_STORY_THRESHOLD") or DEFAULT_THRESHOLD)
    except ValueError:
        return DEFAULT_THRESHOLD


def nearest(conn: Any, item_id: int, vec: bytes) -> Match | None:
    """Ближайшая новость за окно. Считается на месте: 300 × 768 — миллисекунды."""
    if not vec:
        return None
    from . import store  # noqa: PLC0415 — импорт здесь, чтобы не было круга

    best: Match | None = None
    for row in store.neighbours(conn, item_id, embed.name(), WINDOW_HOURS):
        score = embed.similarity(vec, bytes(row["vec"]))
        if best is None or score > best.score:
            best = Match(item_id=int(row["id"]), score=score, title=str(row["title"] or ""))
    return best


async def link(session: Any, conn: Any, item: dict[str, Any],
               budget: model.Budget) -> Match | None:
    """Найти, о чём это уже писали, по смыслу. None — не нашли или не звонили.

    Вектор новости считается здесь же и остаётся в базе: он ещё понадобится,
    когда следующее издание напишет о том же самом.
    """
    vec = await embed.ensure(session, conn, item, budget)
    if not vec:
        return None
    best = nearest(conn, int(item["id"]), vec)
    if best is None:
        return None
    if best.score >= threshold():
        log.info("смысловой дубль %.3f: %s ← %s", best.score,
                 str(item.get("title"))[:60], best.title[:60])
        return best
    if best.score >= NOTE_THRESHOLD:
        # Не склеиваем, но записываем: по этим парам и настраивается порог.
        log.info("близко, но не склеиваем %.3f: %s ← %s", best.score,
                 str(item.get("title"))[:60], best.title[:60])
    return None


__all__ = ("DEFAULT_THRESHOLD", "Match", "NOTE_THRESHOLD", "WINDOW_HOURS", "link",
           "nearest", "threshold")
