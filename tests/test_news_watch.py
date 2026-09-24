"""Сторож: что попадает в базу и какие задержки из этого считаются."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from news import fetch, sources, store, watch

FIXTURES = Path(__file__).parent / "fixtures" / "news"


def _run(handler, source, conn, rounds=1):
    async def go():
        door = fetch.Door()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as session:
            steps = [await watch.once(session, source, conn, door) for _ in range(rounds)]
        return steps

    return asyncio.run(go())


def test_медуза_ложится_в_базу_целиком(tmp_path: Path) -> None:
    """Текст есть в ленте, значит страницу открывать не нужно вовсе."""
    body = (FIXTURES / "meduza-rss.xml").read_text(encoding="utf-8")
    conn = store.connect(tmp_path / "db.sqlite3")
    step = _run(lambda r: httpx.Response(200, text=body), sources.MEDUZA, conn)[0]

    # Первый заход — холодный старт: в базу кладём, но новостью не считаем.
    assert step.found >= 3 and step.cold == step.found
    row = conn.execute("SELECT * FROM items ORDER BY id LIMIT 1").fetchone()
    assert row["body"], "текст взят из ленты"
    assert row["fetched_at"] == row["listed_at"], "разбор не стоил лишнего захода"
    assert row["published_at"], "время издания разобрано из pubDate"


def test_повторный_заход_не_плодит_новостей(tmp_path: Path) -> None:
    body = (FIXTURES / "meduza-rss.xml").read_text(encoding="utf-8")
    conn = store.connect(tmp_path / "db.sqlite3")
    first, second = _run(lambda r: httpx.Response(200, text=body), sources.MEDUZA, conn, rounds=2)
    assert first.cold > 0 and second.fresh == 0 and second.cold == 0
    count = conn.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
    assert count == first.cold


def test_новизна_не_зависит_от_порядка(tmp_path: Path) -> None:
    """Лента Фонтанки не отсортирована по времени — верим только адресам."""
    body = (FIXTURES / "fontanka-24hours.html").read_text(encoding="utf-8")
    shuffled = "<html><body>" + "".join(reversed(body.split("<a"))) + "</body></html>"
    conn = store.connect(tmp_path / "db.sqlite3")
    answers = [body, shuffled]
    first, second = _run(
        lambda r: httpx.Response(200, text=answers.pop(0)), sources.FONTANKA, conn, rounds=2
    )
    assert first.cold >= 10
    assert second.fresh == 0, "перемешанный порядок — не повод объявлять всё новым"


def test_редакционная_задержка_считается_сразу(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    item, _ = store.remember(
        conn, "meduza", "https://meduza.io/news/9", "Заголовок",
        "2026-09-24T19:00:40+00:00", published_at=store.published("Thu, 24 Sep 2026 22:00:10 +0300"),
    )
    assert store.latency_of(conn, item, "редакционная") == 30.0
    assert store.latency_of(conn, item, "до_отправки") is None


def test_ответ_304_ничего_не_меняет(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    step = _run(lambda r: httpx.Response(304), sources.MEDUZA, conn)[0]
    assert step.conditional and step.found == 0 and step.fresh == 0


def test_отказ_защиты_не_роняет_сторожа(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    step = _run(lambda r: httpx.Response(429, text="no"), sources.FONTANKA, conn)[0]
    assert step.status == 429 and step.fresh == 0


def test_холодный_старт_не_считается_измерением(tmp_path: Path) -> None:
    """Первый заход видит ленту за полдня — это возраст ленты, а не скорость."""
    body = (FIXTURES / "meduza-rss.xml").read_text(encoding="utf-8")
    extra = body.replace("</channel>", "<item><title>Свежая</title>"
                         "<link>https://meduza.io/news/2026/09/24/svezhaya</link>"
                         "<pubDate>Thu, 24 Sep 2026 22:34:08 +0300</pubDate></item></channel>")
    answers = [body, extra]
    conn = store.connect(tmp_path / "db.sqlite3")
    first, second = _run(
        lambda r: httpx.Response(200, text=answers.pop(0)), sources.MEDUZA, conn, rounds=2
    )
    assert first.cold > 0 and first.fresh == 0, "старое не объявляется новостью"
    assert second.fresh == 1 and second.cold == 0
    # В статистику попадает только то, что мы застали живьём.
    assert len(store.latency_rows(conn)) == 1
