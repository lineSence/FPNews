"""Досье: справка по теме или сущности одним файлом.

Проверяем, что в файле есть ссылки на оригиналы, что правки и снятия не
теряются и что из пустого запроса досье не собирается [NEWS-007].
"""

from __future__ import annotations

from pathlib import Path

from news import bot as bot_module
from news import entities, export, store, web


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    номер, _ = store.remember(conn, "fontanka", "https://f/1",
                              "«Метрострой» получил контракт", store.now(),
                              published_at=store.now())
    store.fill(conn, номер, "", "Компания «Метрострой» получила контракт на мост.",
               store.now())
    entities.save(conn, номер, "«Метрострой» получил контракт",
                  "Компания «Метрострой» получила контракт на мост.")
    store.revise(conn, номер, "«Метрострой» получил контракт", 40, "о", "старый текст")
    store.mark_gone(conn, номер, 404)
    return conn


def test_досье_по_сущности_со_ссылками(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    сущность = conn.execute(
        "SELECT id FROM entities WHERE name LIKE '%етрострой%'").fetchone()
    тело, имя = export.dossier(conn, {"сущность": str(сущность["id"])})
    assert имя.startswith("досье-") and имя.endswith(".md")
    assert "# Досье:" in тело and "https://f/1" in тело
    assert "## Хронология" in тело and "## Правки и снятия" in тело
    assert "СНЯТО С ПУБЛИКАЦИИ" in тело
    assert "не полная картина" in тело, "досье честно говорит о своих границах"


def test_досье_по_словам(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    тело, имя = export.dossier(conn, {"q": "контракт"})
    assert "# Досье: контракт" in тело and имя == "досье-контракт.md"


def test_из_пустого_запроса_досье_нет(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert export.dossier(conn, {}) == ("", "")
    assert export.dossier(conn, {"сущность": "999"}) == ("", "")


def test_досье_отдаётся_файлом(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    head = ("GET /досье?q=%D0%BA%D0%BE%D0%BD%D1%82%D1%80%D0%B0%D0%BA%D1%82 HTTP/1.1\r\n"
            "Host: localhost\r\nCookie: {}={}".format(web.COOKIE, token))
    ответ = web.route(conn, web.parse(head, ""))
    assert ответ.status.startswith("200") and ответ.filename.endswith(".md")
    assert "# Досье:" in ответ.body
    пусто = web.route(conn, web.parse(
        "GET /досье HTTP/1.1\r\nHost: localhost\r\nCookie: {}={}".format(
            web.COOKIE, token), ""))
    assert пусто.status.startswith("404")
