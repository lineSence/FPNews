"""Мост веб → сторожа: кнопка будит опрос, проверка связи уходит в бот.

Проверяем поведение, а не разметку: что просьба доходит до сторожа, что без
сторожей страница честно об этом говорит и что веб сам в сеть не ходит
[NEWS-002].
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from news import bot as bot_module
from news import access, bridge, pages, store, web


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    # Опрос и проверка связи — действия владельца [guard]: без роли веб
    # честно отвечает 403, и тесты моста проверяли бы охрану, а не мост.
    access.назначить(conn, 7, "владелец")
    return conn


def _post(conn, path: str, token: str, body: str):
    head = ("POST {} HTTP/1.1\r\nHost: localhost\r\nCookie: {}={}"
            "\r\nContent-Type: application/x-www-form-urlencoded").format(path, web.COOKIE, token)
    return web.route(conn, web.parse(head, body))


def _get(conn, path: str, token: str):
    head = "GET {} HTTP/1.1\r\nHost: localhost\r\nCookie: {}={}".format(path, web.COOKIE, token)
    return web.route(conn, web.parse(head, ""))


def test_просьба_будит_сторожа(tmp_path: Path) -> None:
    bridge.забыть()

    async def сценарий() -> bool:
        событие = bridge.подписаться("fontanka")
        assert bridge.попросить() == 1
        await asyncio.wait_for(событие.wait(), timeout=1)
        bridge.отписаться("fontanka")
        return True

    assert asyncio.run(сценарий())
    assert bridge.сторожей() == 0, "ушедший сторож не остаётся на мосту"


def test_без_сторожей_страница_говорит_прямо(tmp_path: Path) -> None:
    bridge.забыть()
    conn = _db(tmp_path)
    тело = _get(conn, "/", web.new_session(conn, 7)).body
    assert "Сторожа в этом процессе не работают" in тело
    assert "заходов в этом процессе ещё не было" in тело


def test_кнопка_опросить_не_ходит_в_сеть(tmp_path: Path) -> None:
    """Веб только кладёт просьбу: ответ страницы не ждёт чужого сервера."""
    bridge.забыть()
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    ответ = _post(conn, "/опросить", token, "метка=" + web.csrf(token))
    assert ответ.status.startswith("303") and ответ.location == "/"


def test_проверка_связи_уходит_в_бот(tmp_path: Path) -> None:
    bridge.забыть()
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    _post(conn, "/проверка", token, "метка=" + web.csrf(token))
    assert bridge.ТЕСТЫ == [7]

    class Бот:
        def __init__(self) -> None:
            self.письма: list[tuple[int, str]] = []

        async def send(self, chat_id: int, text: str, *args, **kwargs) -> None:
            self.письма.append((chat_id, text))

    бот = Бот()

    async def сценарий() -> int:
        stop = asyncio.Event()
        задача = asyncio.create_task(bridge.loop(бот, conn, stop, пауза=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        return await задача

    assert asyncio.run(сценарий()) == 1
    assert бот.письма and "Проверка связи" in бот.письма[0][1]
    assert bridge.ПРОВЕРКА["ответ"] == "доставлено"


def test_сбой_бота_не_роняет_контур(tmp_path: Path) -> None:
    bridge.забыть()
    conn = _db(tmp_path)
    bridge.проверить(7)

    class Злой:
        async def send(self, *args, **kwargs) -> None:
            raise RuntimeError("телега молчит")

    async def сценарий() -> int:
        stop = asyncio.Event()
        задача = asyncio.create_task(bridge.loop(Злой(), conn, stop, пауза=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        return await задача

    assert asyncio.run(сценарий()) == 0
    assert "не вышло" in bridge.ПРОВЕРКА["ответ"]


def test_строка_состояния_показывает_заход(tmp_path: Path) -> None:
    bridge.забыть()
    conn = _db(tmp_path)

    async def сценарий() -> None:
        bridge.подписаться("fontanka")
        bridge.заход("fontanka", код=200, найдено=30, новых=2)

    asyncio.run(сценарий())
    строка = pages._status_line(conn, "метка")
    assert "Сторожей на посту" in строка and "последний заход" in строка
    bridge.забыть()
