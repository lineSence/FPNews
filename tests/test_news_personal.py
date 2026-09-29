"""Личная область видимости: свои источники и темы, чужих лент не видно.

Аккаунт — это не только имя, но и граница: страницы ленты, поиска, правок и
выгрузок показывают человеку его выбор, а не весь архив. Здесь проверяем
именно границу: что читатель видит своё, не видит чужого, и что владелец
видит всё и может выключить издание, но не молча, если на нём люди.
"""

from __future__ import annotations

from pathlib import Path

from news import access
from news import bot as bot_module
from news import export, search, store, stream, web


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    access.назначить(conn, 7, access.ВЛАДЕЛЕЦ)
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


def _add(conn, title: str, source: str, url: str) -> int:
    item_id, _ = store.remember(conn, source, url, title, store.now(),
                                published_at=store.now())
    return item_id


def test_поток_показывает_только_выбранные_издания(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    store.follow_feed(conn, 8, "meduza")
    _add(conn, "Своё событие", "meduza", "https://meduza.io/a")
    _add(conn, "Чужое событие", "fontanka", "https://fontanka.ru/b")
    тело = _get(conn, "/лента", token).body
    assert "Своё событие" in тело and "Чужое событие" not in тело


def test_подписка_на_всё_открывает_весь_поток(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    store.follow_feed(conn, 8, "все")
    _add(conn, "Своё событие", "meduza", "https://meduza.io/a")
    _add(conn, "Чужое событие", "fontanka", "https://fontanka.ru/b")
    тело = _get(conn, "/лента", token).body
    assert "Своё событие" in тело and "Чужое событие" in тело


def test_доставленное_видно_без_подписки(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    item_id = _add(conn, "Сработала тема", "fontanka", "https://fontanka.ru/t")
    conn.execute("INSERT INTO deliveries(item_id, user_id, kind, sent_at) "
                 "VALUES(?,?,?,?)", (item_id, 8, "тема", store.now()))
    conn.commit()
    тело = _get(conn, "/лента", token).body
    assert "Сработала тема" in тело


def test_новичок_получает_подсказку(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    _add(conn, "Чужое событие", "fontanka", "https://fontanka.ru/b")
    тело = _get(conn, "/лента", token).body
    assert "Вы пока ничего не выбрали" in тело and "/подписки" in тело


def test_владелец_видит_весь_поток(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 7)
    _add(conn, "Своё событие", "meduza", "https://meduza.io/a")
    _add(conn, "Чужое событие", "fontanka", "https://fontanka.ru/b")
    тело = _get(conn, "/лента", token).body
    assert "Своё событие" in тело and "Чужое событие" in тело


def test_поиск_в_личной_области(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    search.ensure_index(conn)
    token = web.new_session(conn, 8)
    store.follow_feed(conn, 8, "dp")
    _add(conn, "Мост в копейске", "dp", "https://dp.ru/a")
    _add(conn, "Мост в пригороде", "fontanka", "https://fontanka.ru/b")
    тело = _get(conn, "/поиск?q=мост", token).body
    assert "Мост в копейске" in тело and "Мост в пригороде" not in тело


def test_выгрузка_и_досье_только_свои(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    search.ensure_index(conn)
    store.follow_feed(conn, 8, "dp")
    _add(conn, "Мост в копейске", "dp", "https://dp.ru/a")
    _add(conn, "Мост в пригороде", "fontanka", "https://fontanka.ru/b")
    тело, _, _ = export.make(conn, {"q": "мост"}, "csv", None, 8)
    assert "Мост в копейске" in тело and "Мост в пригороде" not in тело
    текст, _ = export.dossier(conn, {"q": "мост"}, None, 8)
    assert "Мост в копейске" in текст and "Мост в пригороде" not in текст
    # Владельцу — всё: он отвечает за сбор целиком.
    тело, _, _ = export.make(conn, {"q": "мост"}, "csv", None, 7)
    assert "Мост в копейске" in тело and "Мост в пригороде" in тело


def test_чужой_материал_и_копия_скрыты(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    item_id = _add(conn, "Чужое событие", "fontanka", "https://fontanka.ru/b")
    assert _get(conn, "/материал?id={}".format(item_id), token).status.startswith("404")
    assert _get(conn, "/сюжет?id={}".format(item_id), token).status.startswith("404")
    свой = _add(conn, "Своё событие", "meduza", "https://meduza.io/a")
    store.follow_feed(conn, 8, "meduza")
    assert _get(conn, "/материал?id={}".format(свой), token).status.startswith("200")
    assert _get(conn, "/материал?id={}".format(item_id),
                web.new_session(conn, 7)).status.startswith("200")


def test_правки_и_снятия_в_личной_области(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    store.follow_feed(conn, 8, "dp")
    своё = _add(conn, "Своё событие", "dp", "https://dp.ru/a")
    чужое = _add(conn, "Чужое событие", "fontanka", "https://fontanka.ru/b")
    store.revise(conn, своё, "Своё событие изменилось", 50, "d", "новый текст")
    store.revise(conn, чужое, "Чужое событие изменилось", 50, "d", "новый текст")
    тело = _get(conn, "/правки", token).body
    # Правка показывается разницей «было → стало», поэтому смотрим слово целиком
    assert "Своё" in тело and "Чужое" not in тело


def test_чужая_тема_не_подсматривается(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    search.ensure_index(conn)
    bot_module.add_topic(conn, 7, "пожар")
    topic_id = conn.execute("SELECT id FROM topics WHERE user_id = 7").fetchone()["id"]
    _add(conn, "Пожар на складе", "fontanka", "https://fontanka.ru/f")
    # Прямой вызов поиска от чужого имени и страница ленты — обе молчат.
    assert search.search(conn, "склад", topic_id=topic_id, user_id=8) == []
    flt = stream.read({"q": "склад", "тема": str(topic_id)})
    assert stream.select(conn, flt, 8) == []
    # Своя тема работает и у владельца: тема — личное, но своя.
    assert search.search(conn, "склад", topic_id=topic_id, user_id=7) != []


def test_читатель_добавляет_издание_но_не_настройку_сбора(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    ответ = _post(conn, "/источники/добавить", token, _mark(token) + "&сайт=example.com")
    assert ответ.status.startswith("303"), "добавление издания открыто читателю"
    ответ = _post(conn, "/источники/переключить", token, _mark(token) + "&код=meduza")
    assert ответ.status.startswith("403"), "настройка сбора осталась владельцу"
    ответ = _post(conn, "/источники/удалить", token, _mark(token) + "&код=meduza")
    assert ответ.status.startswith("403")


def test_бот_подключает_издание_читателю(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    # Очередь поиска помнит сайт целиком: другой домен, чтобы не мешать
    # веб-тесту про то же самое.
    ответ = bot_module.answer(conn, 8, "Читатель", "/источник other.example")
    assert ответ.startswith("Ищу ленту"), "читатель просит ленту сам"


def test_выключение_издания_с_подписчиками_требует_подтверждения(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.follow_feed(conn, 8, "meduza")
    token = web.new_session(conn, 7)
    ответ = _post(conn, "/источники/переключить", token, _mark(token) + "&код=meduza")
    assert ответ.status.startswith("200") and "1 чел." in ответ.body
    assert store.source_enabled(conn, "meduza"), "первый клик ничего не выключил"
    ответ = _post(conn, "/источники/переключить", token,
                 _mark(token) + "&код=meduza&точно=1")
    assert ответ.status.startswith("303")
    assert not store.source_enabled(conn, "meduza")


def test_удаление_издания_с_подписчиками_требует_подтверждения(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    code = store.add_feed(conn, label="Тестовая лента", door="https://xample.com/feed",
                          host="xample.com")
    store.follow_feed(conn, 8, code)
    token = web.new_session(conn, 7)
    ответ = _post(conn, "/источники/удалить", token, _mark(token) + "&код={}".format(code))
    assert ответ.status.startswith("200") and "1 чел." in ответ.body
    assert any(row["code"] == code for row in store.feeds(conn))
    ответ = _post(conn, "/источники/удалить", token,
                 _mark(token) + "&код={}&точно=1".format(code))
    assert ответ.status.startswith("303")
    assert not any(row["code"] == code for row in store.feeds(conn))


def test_мои_издания_три_состояния(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    token = web.new_session(conn, 8)
    тело = _get(conn, "/подписки", token).body
    assert "Мои издания" in тело and "тишина" in тело and "темы" in тело
    # «всё» — подписка целиком
    assert _post(conn, "/подписки/режим", token,
                 _mark(token) + "&код=meduza&режим=всё").status.startswith("303")
    assert store.source_mode(conn, 8, "meduza") == "всё"
    assert store.source_allowed(conn, 8, "meduza")
    # «тишина» — ничего из издания, даже по темам; фильтр хранит «все, кроме»
    assert _post(conn, "/подписки/режим", token,
                 _mark(token) + "&код=meduza&режим=тишина").status.startswith("303")
    assert store.source_mode(conn, 8, "meduza") == "тишина"
    assert not store.source_allowed(conn, 8, "meduza")
    assert store.source_allowed(conn, 8, "dp"), "остальные издания не молчат"
    # «темы» — умолчание: разрешено, но без подписки целиком
    assert _post(conn, "/подписки/режим", token,
                 _mark(token) + "&код=meduza&режим=темы").status.startswith("303")
    assert store.source_mode(conn, 8, "meduza") == "темы"
    assert store.source_allowed(conn, 8, "meduza")
    assert {row["code"] for row in store.feed_subs_of(conn, 8)} == set()
    # Несуществующее издание и чужой режим не принимаются
    assert not store.set_source_mode(conn, 8, "неттакого", "всё", {"meduza", "dp"})
    assert not store.set_source_mode(conn, 8, "meduza", "кривое", {"meduza", "dp"})


def test_сущности_в_личной_области(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.follow_feed(conn, 8, "dp")
    своё = _add(conn, "Метрострой строит", "dp", "https://dp.ru/a")
    чужое = _add(conn, "Метрострой платит", "fontanka", "https://fontanka.ru/b")
    conn.execute("INSERT INTO entities(kind, name, norm, created_at) "
                 "VALUES('организация', 'Метрострой', 'метрострой', ?)", (store.now(),))
    номер = conn.execute("SELECT id FROM entities").fetchone()["id"]
    for item in (своё, чужое):
        conn.execute("INSERT INTO mentions(item_id, entity_id, in_title, times) "
                     "VALUES(?,?,1,1)", (item, номер))
    conn.commit()
    top = store.entities_top(conn, user_id=8)
    assert top and int(top[0]["материалов"]) == 1, "второй материал из чужого издания"
    материалы = store.entity_items(conn, номер, user_id=8)
    assert len(материалы) == 1 and материалы[0]["id"] == своё
    assert len(store.entity_items(conn, номер, user_id=7)) == 2


def test_страница_источников_считает_подписчиков_владельцу(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.follow_feed(conn, 8, "meduza")
    тело = _get(conn, "/источники", web.new_session(conn, 7)).body
    assert "1 чел." in тело and "подписчиков" in тело
    тело_читателя = _get(conn, "/источники", web.new_session(conn, 8)).body
    assert "подписчиков" not in тело_читателя
    assert "Добавить издание" in тело_читателя, "читатель тоже подключает ленты"


def test_страницы_не_падают_без_входа(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    for path in ("/лента", "/поиск?q=мост", "/правки", "/всплески", "/сущности"):
        assert _get(conn, path).status.startswith("401")
