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

from . import fetch, sources, store

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


async def serve(codes: list[str], rounds: int, path: str) -> dict[str, Any]:
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
    async with fetch.client() as session:
        tasks = [
            asyncio.create_task(
                watch.loop(session, sources.BY_CODE[code], conn, stop, queue, rounds),
                name="сторож-{}".format(code),
            )
            for code in codes
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:  # pragma: no cover — снаружи
            stop.set()
    summary = report(conn)
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
