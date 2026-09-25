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
    главная = _get(conn, "/темы", token)
    assert "Тем пока нет" in главная.body
    added = _post(conn, "/темы/добавить", token, "метка={}&слова=дроны%2C+бпла".format(mark))
    assert added.status.startswith("303")
    assert "дроны" in _get(conn, "/темы", token).body
    number = conn.execute("SELECT id FROM topics WHERE user_id = 7").fetchone()["id"]
    _post(conn, "/темы/удалить", token, "метка={}&номер={}".format(mark, number))
    assert "Тем пока нет" in _get(conn, "/темы", token).body


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
    body = _get(conn, "/темы", web.new_session(conn, 7)).body
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


# --- шаг 9: поиск, карточка материала, сюжет ---------------------------


def test_поиск_без_запроса_ничего_не_ищет(tmp_path: Path) -> None:
    """«Показать всё» на одном ядре стоит дороже, чем пользы [CORE-025]."""
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    store.remember(conn, "fontanka", "https://f/10", "Мост развели", store.now())
    body = _get(conn, "/поиск", token).body
    assert "Введите слово" in body
    assert "Мост развели" not in body


def test_поиск_ведёт_на_карточку_и_на_оригинал(tmp_path: Path) -> None:
    """Ссылка на источник обязательна в любой выдаче [NEWS-007]."""
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    item, _ = store.remember(conn, "fontanka", "https://fontanka.ru/мост",
                             "Мост развели раньше срока", store.now())
    body = _get(conn, "/поиск", token, "?q=мост").body
    assert "Мост развели раньше срока" in body
    assert "https://fontanka.ru/мост" in body
    assert "/материал?id=" + str(item) in body


def test_пустая_выдача_не_доказательство(tmp_path: Path) -> None:
    """Отсутствие записи — не отсутствие события [NEWS-001]."""
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    body = _get(conn, "/поиск", token, "?q=неттакогослова").body
    assert "Ничего не найдено" in body
    assert "не значит, что события не было" in body


def test_в_выдаче_чужой_заголовок_экранируется(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    store.remember(conn, "dp", "https://d/9", "<script>бюджет</script>", store.now())
    body = _get(conn, "/поиск", token, "?q=бюджет").body
    assert "<script>бюджет" not in body and "&lt;script&gt;" in body


def test_поиск_постранично(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    for номер in range(pages.PER_PAGE + 1):
        store.remember(conn, "fontanka", "https://f/ремонт/" + str(номер),
                       "Ремонт дороги " + str(номер), store.now())
    первая = _get(conn, "/поиск", token, "?q=ремонт").body
    assert первая.count("/материал?id=") == pages.PER_PAGE
    assert "дальше" in первая and "назад" not in первая
    вторая = _get(conn, "/поиск", token, "?q=ремонт&стр=2").body
    assert вторая.count("/материал?id=") == 1
    assert "назад" in вторая and "дальше" not in вторая


def test_карточка_не_перепечатывает_чужой_текст(tmp_path: Path) -> None:
    """Лид и ссылка — да, полное тело чужой статьи — нет [NEWS-007]."""
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    item, _ = store.remember(conn, "moika78", "https://k/9", "Снег в сентябре", store.now())
    store.fill(conn, item, "Короткий лид", "ПОЛНЫЙ ЧУЖОЙ ТЕКСТ статьи", store.now())
    response = _get(conn, "/материал", token, "?id=" + str(item))
    assert response.status.startswith("200")
    assert "Снег в сентябре" in response.body
    assert "Короткий лид" in response.body
    assert "https://k/9" in response.body
    assert "ПОЛНЫЙ ЧУЖОЙ ТЕКСТ" not in response.body


def test_несуществующий_материал_даёт_404(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    assert _get(conn, "/материал", token, "?id=10000").status.startswith("404")
    assert _get(conn, "/материал", token, "?id=абв").status.startswith("404")
    assert _get(conn, "/материал", token).status.startswith("404")


def test_сюжет_показывает_кто_первый_и_отставание(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    первый, _ = store.remember(conn, "interfax", "https://i/9", "Аэропорт закрыт",
                                "2026-09-25T10:00:00+00:00", "2026-09-25T10:00:00+00:00")
    второй, _ = store.remember(conn, "ria", "https://r/9", "Аэропорт закрыт до утра",
                                "2026-09-25T10:30:00+00:00", "2026-09-25T10:30:00+00:00")
    store.mark_dup(conn, второй, первый)
    response = _get(conn, "/сюжет", token, "?id=" + str(второй))
    assert response.status.startswith("200")
    assert "Первым опубликовало" in response.body
    assert "30 мин" in response.body
    assert "https://r/9" in response.body


def test_одиночный_материал_сюжета_не_даёт(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    один, _ = store.remember(conn, "paper", "https://p/9", "Выставка открылась", store.now())
    response = _get(conn, "/сюжет", token, "?id=" + str(один))
    assert response.status.startswith("404")
    assert "других изданий" in response.body


def test_поиск_и_карточка_требуют_входа(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert _get(conn, "/поиск", query="?q=мост").status.startswith("401")
    assert _get(conn, "/материал", query="?id=1").status.startswith("401")
    assert _get(conn, "/сюжет", query="?id=1").status.startswith("401")
