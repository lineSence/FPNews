"""Пробник дверей: замер тем же кодом, каким потом ходит сторож.

Зачем отдельная команда. Скорость — единственная цель проекта, а значит
«сколько стоит опрос» нельзя знать со слов: у Медузы условный запрос отдаёт
304 за сорок миллисекунд, у Фонтанки каждый опрос — полный ответ на три
секунды, и поведение ddos-guard при частом опросе заранее неизвестно. Пробник
меряет это на боевом сервере и повторяет замер в любой момент, потому что
чужой сайт меняется без предупреждения `[NEWS-001]`.

Важно, что меряется не отдельным скриптом с другими заголовками, а `news.fetch`
и `news.sources` — теми же модулями, что работают в проде. Иначе замер
описывал бы несуществующую программу.

    python -m news.probe                       # обе двери, один заход
    python -m news.probe fontanka -n 20 -e 15  # двадцать заходов раз в 15 с
    python -m news.probe meduza --fallback     # проверить запасную дверь
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
from typing import Any

import diag

from . import fetch, sources, store, telegram


async def run_source(
    session: Any, source: sources.Source, times: int, every: float, use_fallback: bool
) -> dict[str, Any]:
    """Серия заходов к одной двери. Возвращает сводку для печати."""
    url = source.fallback if use_fallback else source.door
    door = fetch.Door()
    rounds: list[dict[str, Any]] = []
    for number in range(1, times + 1):
        poll = await fetch.poll(session, url, door)
        found = sources.extract(source, poll.body) if poll.body else []
        fresh = [item for item in found if item.url not in door.seen]
        door.seen.update(item.url for item in found)
        row = {
            "заход": number,
            "код": poll.status,
            "секунд": round(poll.seconds, 3),
            "байт": poll.size,
            "условный": poll.conditional,
            "ссылок": len(found),
            "новых": 0 if number == 1 else len(fresh),
            "ошибка": poll.error,
        }
        rounds.append(row)
        diag.event("замер_двери", площадка=source.code, адрес=url, **row)
        print(
            "  {:>3}  код {:<3} {:>7.3f} с  {:>8} б  {}  ссылок {:>3}  новых {:>2} {}".format(
                number,
                poll.status,
                poll.seconds,
                poll.size,
                "304" if poll.conditional else "   ",
                len(found),
                row["новых"],
                poll.error,
            )
        )
        if poll.refused:
            # Чужая защита сказала «отойди» — ждём дольше [NEWS-006].
            print("     защита ответила {}, интервал ×{}".format(poll.status, door.backoff))
        if number < times:
            await asyncio.sleep(every * door.backoff)
    spent = [row["секунд"] for row in rounds]
    bodies = [row["байт"] for row in rounds]
    return {
        "площадка": source.code,
        "адрес": url,
        "заходов": times,
        "интервал": every,
        "секунд_медиана": round(statistics.median(spent), 3) if spent else None,
        "секунд_максимум": round(max(spent), 3) if spent else None,
        "байт_всего": sum(bodies),
        "условных_ответов": sum(1 for row in rounds if row["условный"]),
        "отказов": sum(1 for row in rounds if row["код"] not in (200, 304)),
        "ссылок_всего_разных": len(door.seen),
        "новых_после_первого": sum(row["новых"] for row in rounds),
        "коды": sorted({row["код"] for row in rounds}),
    }


async def probe(known: dict, codes: list[str], times: int, every: float,
                use_fallback: bool) -> list[dict]:
    out: list[dict[str, Any]] = []
    async with fetch.client() as session:
        for code in codes:
            source = known[code]
            url = source.fallback if use_fallback else source.door
            print("{} — {}".format(source.label, url))
            out.append(await run_source(session, source, times, every, use_fallback))
            print()
    return out


def main(argv: Any = None) -> int:
    parser = argparse.ArgumentParser(description="Замер дверей источников")
    parser.add_argument("source", nargs="*", help="коды источников; по умолчанию все")
    parser.add_argument("-n", "--times", type=int, default=1, help="сколько заходов")
    parser.add_argument("-e", "--every", type=float, default=0.0, help="пауза между заходами, с")
    parser.add_argument("--fallback", action="store_true", help="проверить запасную дверь")
    parser.add_argument("--db", default=str(store.DEFAULT_PATH), help="файл базы")
    parser.add_argument("--diag", action="store_true", help="писать события в data/diag")
    args = parser.parse_args(argv)

    telegram.hush()
    # Мерить можно и добавленные в интерфейсе ленты: они в базе, а не в коде.
    conn = store.connect(args.db)
    known = sources.registry(conn)
    conn.close()
    codes = args.source or sorted(known)
    unknown = [code for code in codes if code not in known]
    if unknown:
        print("не знаю источников: {}".format(", ".join(unknown)))
        return 1
    every = args.every if args.every else max(s.interval for s in sources.ALL)
    if args.diag:
        diag.start("пробник дверей")
    summary = asyncio.run(probe(known, codes, max(1, args.times), every, args.fallback))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.diag:
        path = diag.finish(замеров=len(summary))
        print("след: {}".format(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
