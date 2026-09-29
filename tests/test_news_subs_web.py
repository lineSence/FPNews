"""Веб: подписки на издания целиком и профили тем — страницы и формы."""

from __future__ import annotations

from pathlib import Path

from news import access
from news import bot as bot_module
from news import store, web


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    access.назначить(conn, 7, access.ВЛАДЕЛЕЦ)
    # Читатель: подписки и темы доступны обеим ролям
    bot_module.ensure_user(conn, 8, "Читатель")
    access.назначить(conn, 8, access.ЧИТАТЕЛЬ)
    return conn


def _get(conn, path: str, token: str = ""):
    head = "GET {} HTTP/1.1\r\nHost: localhost".format(path)
    if token:
        head += "\r\nCookie: {}={}".format(web.COOKIE, token)
    return web.route(conn, web.parse(head, ""))


def _post(conn, path: str, token: str, body: str):
    head = ("POST {} HTTP/1.1\r\nHost: localhost\r\nCookie: {}={}"
            "\r\nContent-Type: application/x-www-form-urlencoded").format(path, web.COOKIE, token)
    return web.route(conn, web.parse(head, body))


def _mark(token: str) -> str:
    return "метка=" + web.csrf(token)


def test_страница_подписок_и_кнопки(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    ответ = _get(conn, "/подписки", token)
    assert ответ.status.startswith("200")
    assert "Мои издания" in ответ.body and "meduza" in ответ.body
    тело = _mark(token) + "&код=meduza"
    assert _post(conn, "/подписки/добавить", token, тело).status.startswith("303")
    assert {строка["code"] for строка in store.feed_subs_of(conn, 8)} == {"meduza"}
    assert _post(conn, "/подписки/убрать", token, тело).status.startswith("303")
    assert store.feed_subs_of(conn, 8) == []


def test_подписка_на_всё_с_страницы(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    тело = _mark(token) + "&код="
    _post(conn, "/подписки/добавить", token, тело)
    assert {строка["code"] for строка in store.feed_subs_of(conn, 7)} == {""}


def test_страница_источников_показывает_подписку(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    ответ = _get(conn, "/источники", token)
    assert ответ.status.startswith("200") and "целиком" in ответ.body


def test_тема_создаётся_со_стоп_словами_и_порогом(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    тело = _mark(token) + "&слова=процесс&стоп=процессор&порог=2"
    assert _post(conn, "/темы/добавить", token, тело).status.startswith("303")
    row = conn.execute("SELECT * FROM topics WHERE user_id = 8").fetchone()
    assert row["words"] == "процесс" and row["stopwords"] == "процессор"
    assert float(row["threshold"]) == 2.0


def test_формы_профиля_темы_меняют_слова_стоп_слова_порог(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    bot_module.add_topic(conn, 7, "дрон")
    номер = conn.execute("SELECT id FROM topics WHERE user_id = 7").fetchone()["id"]
    assert _post(conn, "/темы/стоп", token,
                 _mark(token) + "&номер={}&стоп=дрон-шоу".format(номер)
                 ).status.startswith("303")
    assert _post(conn, "/темы/порог", token,
                 _mark(token) + "&номер={}&порог=1,5".format(номер)
                 ).status.startswith("303")
    assert _post(conn, "/темы/слово", token,
                 _mark(token) + "&номер={}&слово=бпла".format(номер)
                 ).status.startswith("303")
    row = conn.execute("SELECT * FROM topics WHERE id = ?", (номер,)).fetchone()
    assert row["stopwords"] == "дрон-шоу" and float(row["threshold"]) == 1.5
    assert row["words"] == "дрон,бпла"


def test_чужая_тема_не_редактируется(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    bot_module.add_topic(conn, 7, "дрон")
    номер = conn.execute("SELECT id FROM topics WHERE user_id = 7").fetchone()["id"]
    token = web.new_session(conn, 8)
    assert _post(conn, "/темы/стоп", token,
                 _mark(token) + "&номер={}&стоп=кража".format(номер)
                 ).status.startswith("303")
    row = conn.execute("SELECT stopwords FROM topics WHERE id = ?", (номер,)).fetchone()
    assert row["stopwords"] == ""