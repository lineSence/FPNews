"""Запуск: сторожа, база, живая сводка задержек.

Один процесс на asyncio. Потоков на источник не будет: ядро одно, и потоки на
нём только мешали бы друг другу, а работа здесь почти вся — ожидание сети.

    python -m news.run                    # все источники, до Ctrl+C
    python -m news.run meduza -n 5        # пять заходов и выйти
    python -m news.run --diag             # со следом в data/diag
    python -m news.run --latency          # показать задержки и выйти
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import statistics
from typing import Any

import diag

from . import article, dedup, deliver, fetch, sources, store, telegram

log = logging.getLogger("fpnews")


def report(conn: Any, limit: int = 200) -> dict[str, Any]:
    """Сводка задержек: медиана и девяностый процентиль, а не среднее.

    Среднее портится одной залипшей новостью, а нам нужно понимать типичный
    случай и хвост отдельно [CORE-019].
    """
    rows = store.latency_rows(conn, limit)
    out: dict[str, Any] = {"новостей": len(rows)}
    for kind in ("редакционная", "до_отправки", "до_полного"):
        values = sorted(row[kind] for row in rows if row[kind] is not None)
        if not values:
            out[kind] = None
            continue
        index = min(len(values) - 1, int(len(values) * 0.9))
        out[kind] = {
            "штук": len(values),
            "медиана": round(statistics.median(values), 2),
            "девяностый": round(values[index], 2),
            "максимум": round(values[-1], 2),
        }
    return out


async def handle(bot: Any, session: Any, conn: Any, item_id: int) -> int:
    """Путь одной новости после сторожа.

    Порядок здесь и есть главное решение проекта: сырое сообщение уходит по
    заголовку, и только потом мы идём за текстом, склеиваем дубли и досылаем
    тем, у кого тема нашлась в тексте [NEWS-003].
    """
    sent = await deliver.send_item(bot, conn, item_id)
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None or row["cold"]:
        return sent
    item = dict(row)
    if not item.get("body"):
        parsed = await article.load(session, str(item["url"]))
        if not parsed.empty:
            store.fill(conn, item_id, parsed.lead, parsed.body, store.now())
            if parsed.published_at and not item.get("published_at"):
                store.stamp(conn, item_id, "published_at", store.published(parsed.published_at))
            item["body"] = parsed.body
    store.set_fingerprint(conn, item_id, dedup.fingerprint(item.get("title") or "",
                                                           item.get("body") or ""))
    item = dict(conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())
    original = dedup.find(conn, item)
    if original:
        store.mark_dup(conn, item_id, original)
        sent += await deliver.send_also(bot, conn, item_id, original)
    # Текст приехал — тема могла найтись в нём, а не в заголовке.
    added = await deliver.send_item(bot, conn, item_id)
    if added or sent:
        store.stamp(conn, item_id, "enriched_at", store.now())
    return sent + added


async def dispatch(bot: Any, conn: Any, queue: "asyncio.Queue[int]", stop: Any, done: Any,
                   session: Any = None) -> int:
    """Контур рассылки: берёт новость из очереди и отдаёт подписчикам.

    Отдельная задача, а не часть сторожа: медленный Telegram не имеет права
    задерживать следующий опрос ленты [NEWS-002].
    """
    sent = 0
    while True:
        try:
            item_id = await asyncio.wait_for(queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            if done.is_set() or stop.is_set():
                return sent
            continue
        try:
            sent += await handle(bot, session, conn, item_id)
        except Exception as exc:  # noqa: BLE001 — рассылка не роняет сбор [CORE-017]
            log.warning("рассылка новости %s сорвалась: %s", item_id, exc)


async def serve(codes: list[str], rounds: int, path: str) -> dict[str, Any]:
    from . import bot as bot_module  # noqa: PLC0415
    from . import watch  # noqa: PLC0415 — импорт здесь держит модуль запуска лёгким

    conn = store.connect(path)
    stop = asyncio.Event()
    queue: asyncio.Queue[int] = asyncio.Queue()
    events = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                events.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover — Windows
                pass
    done = asyncio.Event()
    async with fetch.client() as session:
        bot = telegram.Bot(session)
        if not bot.ready:
            log.warning("TELEGRAM_BOT_TOKEN не задан: новости будут копиться в базе без рассылки")
        watchers = [
            asyncio.create_task(
                watch.loop(session, sources.BY_CODE[code], conn, stop, queue, rounds),
                name="сторож-{}".format(code),
            )
            for code in codes
        ]
        sender = asyncio.create_task(
            dispatch(bot, conn, queue, stop, done, session), name="рассылка"
        )
        talker = (
            asyncio.create_task(bot_module.serve(bot, conn, stop), name="бот")
            if bot.ready
            else None
        )
        try:
            await asyncio.gather(*watchers)
        except asyncio.CancelledError:  # pragma: no cover — снаружи
            stop.set()
        done.set()
        sent = await sender
        if talker is not None:
            stop.set()
            talker.cancel()
    summary = report(conn)
    summary["разослано"] = sent
    summary["в_очереди_на_обработку"] = queue.qsize()
    conn.close()
    return summary


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Новостные сторожа")
    parser.add_argument("source", nargs="*", help="коды источников; по умолчанию все")
    parser.add_argument("-n", "--rounds", type=int, default=0, help="заходов на источник (0 — без конца)")
    parser.add_argument("--db", default=str(store.DEFAULT_PATH), help="файл базы")
    parser.add_argument("--diag", action="store_true", help="писать след в data/diag")
    parser.add_argument("--latency", action="store_true", help="только показать задержки")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.latency:
        conn = store.connect(args.db)
        print(_pretty(report(conn)))
        conn.close()
        return 0

    codes = args.source or [source.code for source in sources.ALL]
    unknown = [code for code in codes if code not in sources.BY_CODE]
    if unknown:
        print("не знаю источников: {}".format(", ".join(unknown)))
        return 1
    if args.diag:
        diag.start("новостной прогон")
    summary = asyncio.run(serve(codes, args.rounds, args.db))
    print(_pretty(summary))
    if args.diag:
        print("след: {}".format(diag.finish(**{"новостей": summary.get("новостей", 0)})))
    return 0


def _pretty(data: dict[str, Any]) -> str:
    import json  # noqa: PLC0415

    return json.dumps(data, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
