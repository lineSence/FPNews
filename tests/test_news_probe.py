"""Пробник и поход к двери: без сети, на поддельном ответе.

Проверяется то, ради чего модуль написан: условный запрос экономит трафик,
чужая защита увеличивает паузу, а новизна считается по множеству адресов, а не
по порядку элементов в ленте.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from news import fetch, probe, sources

FIXTURES = Path(__file__).parent / "fixtures" / "news"


def _session(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_условный_запрос_экономит_всё() -> None:
    body = (FIXTURES / "meduza-rss.xml").read_text(encoding="utf-8")
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if "if-modified-since" in request.headers:
            return httpx.Response(304)
        return httpx.Response(200, text=body, headers={"Last-Modified": "Thu, 24 Sep 2026 19:00:00 GMT"})

    async def run():
        door = fetch.Door()
        async with _session(handler) as session:
            first = await fetch.poll(session, sources.MEDUZA.door, door)
            second = await fetch.poll(session, sources.MEDUZA.door, door)
            return first, second

    first, second = asyncio.run(run())
    assert first.status == 200 and first.size > 0
    assert second.conditional and second.size == 0 and second.body == ""
    assert second.ok, "«ничего нового» — это успех, а не сбой"
    assert "if-modified-since" in calls[1]


def test_защита_увеличивает_паузу() -> None:
    """[NEWS-006]: в 429 нельзя долбиться с прежней частотой."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    async def run():
        door = fetch.Door()
        async with _session(handler) as session:
            for _ in range(3):
                poll = await fetch.poll(session, sources.FONTANKA.door, door)
            return door, poll

    door, poll = asyncio.run(run())
    assert poll.refused and not poll.ok
    assert door.backoff == 8


def test_обрыв_сети_не_роняет_сторожа() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет связи")

    async def run():
        async with _session(handler) as session:
            return await fetch.poll(session, sources.MEDUZA.door, fetch.Door())

    poll = asyncio.run(run())
    assert poll.status == 0 and poll.error and not poll.ok


def test_пробник_считает_новые_ссылки() -> None:
    """Второй заход по той же ленте не должен объявлять всё новым."""
    body = (FIXTURES / "fontanka-24hours.html").read_text(encoding="utf-8")
    extra = body.replace(
        "</body>",
        '<a href="https://www.fontanka.ru/2026/09/24/99999999/">Свежая</a></body>',
    )
    answers = [body, extra]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=answers.pop(0))

    async def run():
        async with _session(handler) as session:
            return await probe.run_source(session, sources.FONTANKA, 2, 0.0, False)

    summary = asyncio.run(run())
    assert summary["новых_после_первого"] == 1
    assert summary["отказов"] == 0
    assert summary["условных_ответов"] == 0
