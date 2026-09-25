"""Центр управления: тема, правки, запросы, источники, отдача в Telegram.

Проверяем не вёрстку, а поведение: что кнопка меняет строку в базе, что
чужое не трогается и что чужой HTML не попадает на страницу как разметка
[CORE-016].
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from news import bot as bot_module
from news import deliver, pages, store, web


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    return conn


def _get(conn, path: str, token: str = "", query: str = "", theme: str = ""):
    head = "GET {}{} HTTP/1.1\r\nHost: localhost".format(path, query)
    cookies = []
    if token:
        cookies.append("{}={}".format(web.COOKIE, token))
    if theme:
        cookies.append("{}={}".format(web.THEME_COOKIE, urllib.parse.quote(theme)))
    if cookies:
        head += "\r\nCookie: " + "; ".join(cookies)
    return web.route(conn, web.parse(head, ""))


def _post(conn, path: str, token: str, body: str):
    head = ("POST {} HTTP/1.1\r\nHost: localhost\r\nCookie: {}={}"
            "\r\nContent-Type: application/x-www-form-urlencoded").format(path, web.COOKIE, token)
    return web.route(conn, web.parse(head, body))


def _mark(token: str) -> str:
    return "метка=" + web.csrf(token)


def test_тема_ставится_кукой_и_возвращает_назад(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    ответ = _get(conn, "/тема", token, "?вид=тёмная&откуда=/поиск")
    assert ответ.status.startswith("303") and ответ.location == "/поиск"
    assert web.THEME_COOKIE in ответ.cookie
    страница = _get(conn, "/", token, theme="тёмная").body
    assert 'class="тёмная"' in страница
    светлая = _get(conn, "/", token, theme="светлая").body
    assert 'class=""' in светлая, "светлая — это просто отсутствие класса"


def test_тема_не_уводит_на_чужой_сайт(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    for куда in ("//зло.рф", "https://зло.рф/вход", "зло"):
        ответ = _get(conn, "/тема", token,
                     "?вид=тёмная&откуда=" + urllib.parse.quote(куда, safe=""))
        assert ответ.location == "/", "открытый редирект из настройки оформления"


def test_неизвестная_тема_это_система(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    ответ = _get(conn, "/тема", token, "?вид=кислотная")
    assert "%D1%81%D0%B8%D1%81%D1%82%D0%B5%D0%BC%D0%B0" in ответ.cookie
    assert web.theme_of(web.parse("GET / HTTP/1.1\r\nCookie: fpnews_theme=мусор", "")) == "система"


def test_страница_правок_показывает_диф_и_снятие(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    первый, _ = store.remember(conn, "fontanka", "https://f/1",
                               "Мост закрыт на ремонт", store.now())
    store.fill(conn, первый, "", "Движение перекрыто до мая.", store.now())
    store.revise(conn, первый, "Мост закрыт на неделю", 40, "отпечаток",
                 "Движение перекрыто до июня.")
    второй, _ = store.remember(conn, "fontanka", "https://f/2", "Снятая новость", store.now())
    store.mark_gone(conn, второй, 404)
    тело = _get(conn, "/правки", token).body
    assert "<del>неделю</del>" in тело and "<ins>ремонт</ins>" in тело
    assert "Снятая новость" in тело and "404" in тело


def test_сохранённые_запросы_добавляются_и_убираются(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    ответ = _post(conn, "/запросы/добавить", token,
                  _mark(token) + "&запрос=тариф&источник=fontanka&уведомлять=1")
    assert ответ.location == "/запросы"
    сохранён = store.queries(conn, 7)
    assert len(сохранён) == 1 and сохранён[0]["query"] == "тариф"
    assert сохранён[0]["source"] == "fontanka" and сохранён[0]["notify"] == 1
    номер = int(сохранён[0]["id"])
    _post(conn, "/запросы/уведомления", token, _mark(token) + "&номер=" + str(номер))
    assert store.queries(conn, 7)[0]["notify"] == 0
    _post(conn, "/запросы/удалить", token, _mark(token) + "&номер=" + str(номер))
    assert store.queries(conn, 7) == []


def test_чужой_запрос_не_трогается(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    bot_module.ensure_user(conn, 8, "Сосед")
    чужой = store.add_query(conn, 8, "подрядчик")
    token = web.new_session(conn, 7)
    _post(conn, "/запросы/удалить", token, _mark(token) + "&номер=" + str(чужой))
    _post(conn, "/запросы/уведомления", token, _mark(token) + "&номер=" + str(чужой))
    остался = store.queries(conn, 8)
    assert len(остался) == 1 and остался[0]["notify"] == 1


def test_форма_без_метки_ничего_не_делает(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    ответ = _post(conn, "/запросы/добавить", token, "метка=чужая&запрос=тариф")
    assert ответ.status.startswith("400") and store.queries(conn, 7) == []


def test_источник_выключается_и_получает_свой_интервал(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    assert store.source_enabled(conn, "fontanka")
    _post(conn, "/источники/переключить", token, _mark(token) + "&код=fontanka")
    assert not store.source_enabled(conn, "fontanka")
    _post(conn, "/источники/переключить", token, _mark(token) + "&код=fontanka")
    assert store.source_enabled(conn, "fontanka")
    _post(conn, "/источники/интервал", token, _mark(token) + "&код=fontanka&секунд=600")
    assert store.source_every(conn, "fontanka") == 600
    _post(conn, "/источники/интервал", token, _mark(token) + "&код=fontanka&секунд=ерунда")
    assert store.source_every(conn, "fontanka") == 0, "мусор в форме — не падение"


def test_виды_сообщений_и_тихие_часы_сохраняются(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    _post(conn, "/телеграм/сохранить", token,
          _mark(token) + "&вид=сырое&вид=изменение&с=23:00&по=08:00")
    assert store.kinds_of(conn, 7) == {"сырое", "изменение"}
    assert deliver.wants(conn, 7, "сырое") and not deliver.wants(conn, 7, "тоже_написали")
    строка = conn.execute("SELECT quiet_from, quiet_to FROM users WHERE id = 7").fetchone()
    assert строка["quiet_from"] == "23:00" and строка["quiet_to"] == "08:00"
    тело = _get(conn, "/телеграм", token).body
    assert "checked" in тело and "23:00" in тело


def test_копия_отдаётся_текстом(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    item, _ = store.remember(conn, "fontanka", "https://f/3", "Заголовок", store.now())
    store.save_snapshot(conn, item, "<html><script>alert(1)</script></html>")
    номер = int(store.snapshots(conn, item)[0]["id"])
    ответ = _get(conn, "/копия", token, "?id=" + str(номер))
    assert ответ.kind.startswith("text/plain"), "чужой HTML не выполняем у себя"
    assert "<script>" in ответ.body
    assert _get(conn, "/копия", token, "?id=999").status.startswith("404")


def test_разметка_в_запросе_экранируется(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    _post(conn, "/запросы/добавить", token, _mark(token) +
          "&запрос=" + urllib.parse.quote("<script>alert(1)</script>"))
    тело = _get(conn, "/запросы", token).body
    assert "<script>alert" not in тело and "&lt;script&gt;" in тело


def test_меню_подсвечивает_открытый_раздел(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    for путь, _ in [(href, title) for _, links in pages.MENU for href, title in links]:
        тело = _get(conn, путь, token).body
        assert 'href="{}" class=тут'.format(путь) in тело, путь


def test_несуществующая_форма_даёт_404(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    assert _post(conn, "/выдумка", token, _mark(token)).status.startswith("404")
