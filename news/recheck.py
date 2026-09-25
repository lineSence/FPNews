"""Перечитывание материалов и досылка «изменение в новости».

Новость на сайте живёт: заголовок правят, текст дописывают, «задержан» через
час становится «арестован». Мы отправили первую версию по горячему пути
`[NEWS-003]` — значит обязаны сказать, когда она перестала быть правдой.

Частота убывающая: первые минуты правят почти всё, через сутки — почти
ничего. Поэтому материал перечитывается через 10 минут, 30 минут, 2 часа и
6 часов после обнаружения, а дальше не трогается вовсе. Пять заходов на
новость — это дёшево; ходить раз в час сутками ради одного исправления в
тысяче — нет `[NEWS-006]`.

Что считается изменением. Заголовок — любое изменение по существу: его читают
в уведомлении, и правка «задержан → арестован» и есть новость. Текст — только
существенное приращение: издания постоянно дописывают строчку про «читайте
также», и досылать такое значит приучить человека не читать досылки
`[NEWS-004]`.
"""

from __future__ import annotations

import html
import logging
from typing import Any

from . import article, dedup, store

log = logging.getLogger("fpnews.recheck")

# Через сколько секунд после обнаружения делается заход номер N.
STEPS = (600, 1800, 7200, 21600)
# Коды, по которым материал считается снятым с публикации. 403 и 429 сюда не
# входят: это защита сайта от нас, а не исчезновение текста [CORE-017].
GONE_CODES = (404, 410)
# Текст вырос настолько — это дописанный материал, а не правка опечатки.
GROWTH = 0.25
MIN_GROWTH_CHARS = 400


def due(conn: Any, limit: int = 20) -> list[dict[str, Any]]:
    """Материалы, которым пора на перечитывание. Самые свежие первыми."""
    rows = conn.execute(
        "SELECT id, url, title, body, checks, listed_at, checked_at, gone_at FROM items "
        "WHERE cold = 0 AND sent_at IS NOT NULL AND checks < ? "
        "AND julianday(listed_at) >= julianday('now', '-1 day') "
        "ORDER BY listed_at DESC LIMIT ?",
        (len(STEPS), int(limit * 4)),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if _age(item.get("listed_at")) >= STEPS[int(item["checks"])]:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _age(when: Any) -> float:
    from datetime import datetime, timezone  # noqa: PLC0415

    try:
        start = datetime.fromisoformat(str(when))
    except (TypeError, ValueError):
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - start).total_seconds()


def changed(old: dict[str, Any], title: str, body: str) -> str:
    """Что изменилось: «заголовок», «дополнен» или пусто."""
    was_title = dedup.normalize_title(str(old.get("title") or ""))
    now_title = dedup.normalize_title(title)
    if now_title and was_title and now_title != was_title:
        return "заголовок"
    was = len(str(old.get("body") or ""))
    became = len(body or "")
    if was and became - was >= max(MIN_GROWTH_CHARS, was * GROWTH):
        return "дополнен"
    return ""


def gone_message(item: dict[str, Any], code: int) -> str:
    """Сообщение о снятии. Говорим ровно то, что видели: код и время."""
    return (
        "<i>Материал снят с публикации</i> — ответ {code}\n\n<b>{title}</b>\n\n"
        "Копия страницы сохранена у нас.\n{url}"
    ).format(code=int(code), title=html.escape(str(item.get("title") or "")),
             url=item.get("url"))


def message(item: dict[str, Any], kind: str, title: str) -> str:
    """Досылка. Коротко: что изменилось, как теперь, ссылка `[NEWS-007]`."""
    if kind == "заголовок":
        head = "Заголовок изменился"
        body = "Было: <s>{}</s>\nСтало: <b>{}</b>".format(
            html.escape(str(item.get("title") or "")), html.escape(title)
        )
    else:
        head = "Материал дополнен"
        body = "<b>{}</b>".format(html.escape(title or str(item.get("title") or "")))
    return "<i>Изменение в новости</i> — {}\n\n{}\n\n{}".format(head, body, item.get("url"))


async def once(bot: Any, session: Any, conn: Any, limit: int = 20) -> int:
    """Один заход по всем назревшим материалам. Возвращает число досылок."""
    from . import deliver  # noqa: PLC0415 — импорт здесь, чтобы не было круга

    sent = 0
    for item in due(conn, limit):
        item_id = int(item["id"])
        store.mark_checked(conn, item_id)
        poll, parsed = await article.load_page(session, str(item["url"]))
        if poll.status in GONE_CODES:
            # Исчезновение — само по себе наблюдение, и оно ценнее правки.
            if not item.get("gone_at"):
                store.mark_gone(conn, item_id, poll.status)
                sent += await deliver.send_change(bot, conn, item_id,
                                                  gone_message(item, poll.status))
            else:
                store.mark_gone(conn, item_id, poll.status)
            continue
        if parsed.empty:
            continue
        if item.get("gone_at"):
            store.revive(conn, item_id)
        if poll.body:
            store.save_snapshot(conn, item_id, poll.body)
        title = parsed.title or str(item.get("title") or "")
        store.revise(conn, item_id, title, len(parsed.body),
                     dedup.simhash(parsed.body), parsed.body)
        kind = changed(item, title, parsed.body)
        if not kind:
            continue
        store.fill(conn, int(item["id"]), parsed.lead, parsed.body, store.now())
        if title != item.get("title"):
            conn.execute("UPDATE items SET title = ? WHERE id = ?", (title, int(item["id"])))
            conn.commit()
        sent += await deliver.send_change(bot, conn, int(item["id"]),
                                          message(item, kind, title))
    if sent:
        log.info("досылок об изменениях: %s", sent)
    return sent


async def loop(bot: Any, session: Any, conn: Any, stop: Any, every: float = 120.0,
               rounds: int = 0) -> int:
    """Фоновый контур. Спит между заходами и слушает остановку."""
    import asyncio  # noqa: PLC0415

    sent = 0
    while not stop.is_set():
        try:
            sent += await once(bot, session, conn)
        except Exception as exc:  # noqa: BLE001 — досылки не роняют сбор [CORE-017]
            log.warning("перечитывание сорвалось: %s", exc)
        rounds -= 1
        if rounds == 0:
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=every)
        except asyncio.TimeoutError:
            continue
    return sent


__all__ = ("GONE_CODES", "GROWTH", "MIN_GROWTH_CHARS", "STEPS", "changed", "due",
           "gone_message", "loop", "message", "once")
