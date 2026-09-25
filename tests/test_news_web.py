"""Веб-интерфейс: вход по коду, свои темы, чужое не видно."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from news import bot as bot_module
from news import pages, store, web


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    return conn


def _get(conn, path: str, token: str = "", query: str = ""):
    head = "GET {}{} HTTP/1.1\r\nHost: localhost".format(path, query)
    if token:
        head += "\r\nCookie: {}={}".format(web.COOKIE, token)
    return web.route(conn, web.parse(head, ""))


def _post(conn, path: str, token: str, body: str):
    head = ("POST {} HTTP/1.1\r\nHost: localhost\r\nCookie: {}={}"
            "\r\nContent-Type: application/x-www-form-urlencoded").format(path, web.COOKIE, token)
    return web.route(conn, web.parse(head, body))


def test_без_входа_ничего_не_видно(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    response = _get(conn, "/")
    assert response.status.startswith("401")
    assert "/вход" in response.body and "Пароля нет" in response.body


def test_код_работает_один_раз(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    code = web.code_for(conn, 7)
    first = _get(conn, "/вход", query="?код=" + code)
    assert first.status.startswith("303") and first.location == "/"
    assert web.COOKIE in first.cookie and "HttpOnly" in first.cookie
    again = _get(conn, "/вход", query="?код=" + code)
    assert again.status.startswith("401"), "код сгорает при первом использовании"


def test_просроченный_код_не_пускает(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    code = web.code_for(conn, 7)
    conn.execute("UPDATE login_codes SET expires_at = datetime('now', '-1 minute')")
    conn.commit()
    assert _get(conn, "/вход", query="?код=" + code).status.startswith("401")


def test_темы_добавляются_и_удаляются_через_веб(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    mark = web.csrf(token)
    home = _get(conn, "/", token)
    assert "Тем пока нет" in home.body
    added = _post(conn, "/темы/добавить", token, "метка={}&слова=дроны%2C+бпла".format(mark))
    assert added.status.startswith("303")
    assert "дроны" in _get(conn, "/", token).body
    number = conn.execute("SELECT id FROM topics WHERE user_id = 7").fetchone()["id"]
    _post(conn, "/темы/удалить", token, "метка={}&номер={}".format(mark, number))
    assert "Тем пока нет" in _get(conn, "/", token).body


def test_форма_без_метки_отбивается(tmp_path: Path) -> None:
    """Чужая вкладка не знает метку сессии — значит не оформит подписку за вас."""
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    response = _post(conn, "/темы/добавить", token, "слова=дроны")
    assert response.status.startswith("400")
    assert conn.execute("SELECT COUNT(*) c FROM topics").fetchone()["c"] == 0


def test_чужие_темы_не_видны(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    bot_module.ensure_user(conn, 9, "Другой")
    bot_module.add_topic(conn, 9, "секретная тема")
    body = _get(conn, "/", web.new_session(conn, 7)).body
    assert "секретная" not in body


def test_выход_гасит_сессию(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    response = _post(conn, "/выход", token, "метка=" + web.csrf(token))
    assert response.status.startswith("303") and "Max-Age=0" in response.cookie
    assert _get(conn, "/", token).status.startswith("401")


def test_страницы_новостей_и_задержек(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    bot_module.add_topic(conn, 7, "дрон")
    item, _ = store.remember(conn, "meduza", "https://meduza.io/1", "Дрон над Пулково",
                             store.now())
    store.stamp(conn, item, "sent_at", store.now())
    conn.execute("INSERT INTO deliveries(item_id, user_id, topic_id, kind, sent_at) "
                 "VALUES(?,?,?,?,?)", (item, 7, 1, "сырое", store.now()))
    conn.commit()
    feed = _get(conn, "/новости", token).body
    assert "Дрон над Пулково" in feed and "https://meduza.io/1" in feed
    assert "Задержки" in _get(conn, "/задержки", token).body
    assert _get(conn, "/такого-нет", token).status.startswith("404")


def test_чужой_заголовок_не_ломает_страницу(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    item, _ = store.remember(conn, "meduza", "https://meduza.io/2", "<script>зло</script>",
                             store.now())
    conn.execute("INSERT INTO deliveries(item_id, user_id, topic_id, kind, sent_at) "
                 "VALUES(?,?,?,?,?)", (item, 7, None, "сырое", store.now()))
    conn.commit()
    body = _get(conn, "/новости", token).body
    assert "<script>зло" not in body and "&lt;script&gt;" in body


def test_бот_присылает_ссылку_на_вход(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FPNEWS_WEB_URL", "http://localhost:6769")
    conn = _db(tmp_path)
    text = bot_module.answer(conn, 7, "Владелец", "/вход")
    assert "http://localhost:6769/вход?код=" in text
    code = text.split("код=")[1].split("\n")[0]
    assert web.redeem(conn, code) == 7


def test_сервер_отвечает_по_настоящему(tmp_path: Path, monkeypatch) -> None:
    """Проверка целиком: сокет, разбор запроса, страница."""
    conn = _db(tmp_path)
    monkeypatch.setenv("FPNEWS_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("FPNEWS_WEB_PORT", "8791")

    async def go():
        stop = asyncio.Event()
        task = asyncio.ensure_future(web.serve(conn, stop))
        await asyncio.sleep(0.2)
        async with httpx.AsyncClient() as session:
            answer = await session.get("http://127.0.0.1:8791/", timeout=5.0)
        stop.set()
        await task
        return answer

    response = asyncio.run(go())
    assert response.status_code == 401
    assert "Чтобы войти" in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_разметка_не_течёт() -> None:
    assert "&lt;b&gt;" in pages.oops("<b>опасно</b>")
