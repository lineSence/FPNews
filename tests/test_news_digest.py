"""Сводка: одно сообщение вместо потока.

Главное, что проверяем: сводка уходит один раз в день, пустую не шлём,
а перезапуск процесса не присылает её второй раз [NEWS-004].
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from news import bot as bot_module
from news import digest, store, web


def _conn(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    return conn


def _add(conn, title: str, source: str = "fontanka") -> int:
    адрес = "https://example.org/{}".format(abs(hash(title)) % 10**9)
    item_id, _ = store.remember(conn, source, адрес, title, store.now(),
                                published_at=store.now())
    return item_id


class _Бот:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.отправлено: list[tuple[int, str]] = []

    async def send(self, user_id, text, **kwargs):  # noqa: ANN001, ANN003
        self.отправлено.append((user_id, text))
        return self.ok


def _момент(час: int, минута: int = 0) -> datetime:
    return datetime(2026, 9, 25, час, минута, tzinfo=timezone(timedelta(hours=3)))


def test_сводка_собирает_своё_и_общее(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    первый = _add(conn, "Мост закрыли")
    второй = _add(conn, "Тариф вырос", "dp")
    conn.execute("INSERT INTO deliveries(item_id, user_id, kind, sent_at) VALUES(?,?,?,?)",
                 (первый, 7, "сырое", store.now()))
    conn.commit()
    store.revise(conn, второй, "Тариф вырос вдвое", 50, "d", "новый текст")
    store.mark_gone(conn, второй, 404)
    data = digest.collect(conn, 7)
    assert data["всего"] == 2 and len(data["ваше"]) == 1
    assert data["правки"] and data["снятия"]
    текст = digest.text(data, "http://localhost:6769")
    assert "Мост закрыли" in текст and "404" in текст
    assert "видели мы, а не всё, что вышло" in текст


def test_пустая_сводка_не_шлётся(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    store.set_digest(conn, 7, "09:00")
    бот = _Бот()
    assert digest.empty(digest.collect(conn, 7))
    assert asyncio.run(digest.once(бот, conn)) == 0
    assert бот.отправлено == []
    строка = conn.execute("SELECT digest_on FROM users WHERE id = 7").fetchone()
    assert строка["digest_on"], "отметка ставится, чтобы не проверять каждые десять минут"


def test_сводка_уходит_один_раз_в_день(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    store.set_digest(conn, 7, "09:00")
    item = _add(conn, "Мост закрыли")
    store.mark_gone(conn, item, 410)
    бот = _Бот()
    assert asyncio.run(digest.once(бот, conn)) == 1
    assert asyncio.run(digest.once(бот, conn)) == 0, "перезапуск не шлёт второй раз"
    assert len(бот.отправлено) == 1


def test_время_соблюдается() -> None:
    человек = {"digest_at": "09:00", "digest_on": ""}
    assert not digest.due(человек, _момент(8, 30))
    assert digest.due(человек, _момент(9, 0))
    assert digest.due(человек, _момент(23, 0)), "опоздание лучше пропуска"
    assert not digest.due({"digest_at": "", "digest_on": ""}, _момент(9, 0))
    assert not digest.due({"digest_at": "ерунда", "digest_on": ""}, _момент(9, 0))
    assert not digest.due({"digest_at": "09:00", "digest_on": "2026-09-25"}, _момент(9, 0))


def test_команда_бота_настраивает_и_показывает(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    ответ = bot_module.answer(conn, 7, "Владелец", "/сводка 08:30")
    assert "08:30" in ответ
    assert conn.execute("SELECT digest_at FROM users WHERE id = 7").fetchone()["digest_at"] \
        == "08:30"
    выкл = bot_module.answer(conn, 7, "Владелец", "/сводка нет")
    assert "выключена" in выкл
    assert conn.execute("SELECT digest_at FROM users WHERE id = 7").fetchone()["digest_at"] == ""
    сейчас = bot_module.answer(conn, 7, "Владелец", "/сводка")
    assert "материалов" in сейчас


def test_страница_сводки_сохраняет_время(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    token = web.new_session(conn, 7)
    head = ("POST /сводка/время HTTP/1.1\r\nHost: x\r\nCookie: {}={}"
            "\r\nContent-Type: application/x-www-form-urlencoded").format(web.COOKIE, token)
    ответ = web.route(conn, web.parse(head, "метка={}&время=07:15".format(web.csrf(token))))
    assert ответ.location == "/сводка"
    assert conn.execute("SELECT digest_at FROM users WHERE id = 7").fetchone()["digest_at"] \
        == "07:15"
    страница = web.route(conn, web.parse(
        "GET /сводка HTTP/1.1\r\nHost: x\r\nCookie: {}={}".format(web.COOKIE, token), ""))
    assert страница.status.startswith("200") and "07:15" in страница.body
