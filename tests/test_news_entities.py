"""Сущности и всплески: правила извлечения и счёт упоминаний.

Проверяем ровно две вещи: что правило берёт то, что должно, и не берёт то,
что похоже. Ложная сущность в карточке стоит дороже пропущенной [NEWS-008].
"""

from __future__ import annotations

from pathlib import Path

from news import bot as bot_module
from news import entities, store, web


def _conn(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    return conn


def _add(conn, title: str, body: str = "", *, source: str = "fontanka",
         published: str = "") -> int:
    адрес = "https://example.org/{}".format(abs(hash(title + body)) % 10**9)
    item_id, _ = store.remember(conn, source, адрес, title, store.now(),
                                published_at=published or store.now())
    conn.execute("UPDATE items SET body = ? WHERE id = ?", (body, item_id))
    conn.commit()
    entities.save(conn, item_id, title, body)
    return item_id


def test_организации_по_кавычкам_и_форме() -> None:
    найдено = entities.organisations('ООО «Балтийская Стройка» и АО Метрострой подписали')
    ключи = {entities.normalize(name) for name in найдено}
    assert "балтийская стройка" in ключи and "метрострой" in ключи


def test_люди_по_фамилии_а_не_по_заглавным() -> None:
    текст = ("Вице-губернатор Иван Петров заявил, что ремонт Литейного моста продолжится. "
             "А. Смирнов возглавил комиссию.")
    люди = {entities.normalize(name) for name in entities.people(текст)}
    assert "иван петров" in люди and "а. смирнов" in люди
    assert "ремонт литейного" not in люди, "заглавные — ещё не человек"
    assert "вице-губернатор иван" not in люди


def test_деньги_и_места() -> None:
    текст = "Контракт на 1,2 млрд рублей: работы на набережной Фонтанки и Литейном мосту."
    assert "1,2 млрд рублей" in entities.money(текст)
    места = {entities.normalize(name) for name in entities.places(текст)}
    assert "набережной фонтанки" in места and "литейном мосту" in места


def test_упоминания_пишутся_и_не_удваиваются(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = _add(conn, "Смольный и «Метрострой»", "ООО «Метрострой» получило контракт.")
    первый = conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"]
    entities.save(conn, item, "Смольный и «Метрострой»", "ООО «Метрострой» получило контракт.")
    assert conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"] == первый


def test_сущности_склеиваются_по_написанию(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "«Метрострой» получил контракт", "первый текст")
    _add(conn, "Суд и «МЕТРОСТРОЙ»", "второй текст")
    строки = [row for row in store.entities_top(conn) if "етрострой" in str(row["name"]).lower()]
    assert len(строки) == 1 and строки[0]["материалов"] == 2


def test_карточка_сущности_показывает_материалы(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "«Метрострой» получил контракт", "Работы начнутся в мае.")
    номер = int(store.entities_top(conn)[0]["id"])
    материалы = store.entity_items(conn, номер)
    assert материалы and материалы[0]["in_title"] == 1
    assert store.entity_days(conn, номер, 30)


def test_всплеск_виден_а_ровный_фон_нет(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for номер in range(4):
        _add(conn, "«Метрострой» и стройка {}".format(номер), "текст {}".format(номер))
    for номер in range(2):
        _add(conn, "«Фонарь» и будни {}".format(номер), "старый текст {}".format(номер),
             published="2026-09-01T10:00:00+00:00")
    всплески = {row["имя"].lower(): row for row in store.bursts(conn)}
    assert any("метрострой" in имя for имя in всплески), "четыре материала за сутки — всплеск"
    assert not any("фонарь" in имя for имя in всплески), "прошлый месяц — не всплеск"


def test_страницы_сущностей_открываются(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "«Метрострой» получил контракт", "Иван Петров подписал документ.")
    token = web.new_session(conn, 7)

    def _get(path: str, query: str = ""):
        head = "GET {}{} HTTP/1.1\r\nHost: x\r\nCookie: {}={}".format(
            path, query, web.COOKIE, token)
        return web.route(conn, web.parse(head, ""))

    список = _get("/сущности")
    assert список.status.startswith("200") and "Метрострой" in список.body
    только_люди = _get("/сущности", "?вид=человек")
    assert "Иван Петров" in только_люди.body and "Метрострой" not in только_люди.body
    номер = int(store.entities_top(conn)[0]["id"])
    карточка = _get("/сущность", "?id={}".format(номер))
    assert карточка.status.startswith("200")
    assert _get("/сущность", "?id=нет").status.startswith("404")
    assert _get("/всплески").status.startswith("200")


def test_разметка_в_имени_экранируется(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, 'Компания «<script>alert(1)</script>» подала иск', "текст")
    token = web.new_session(conn, 7)
    head = "GET /сущности HTTP/1.1\r\nHost: x\r\nCookie: {}={}".format(web.COOKIE, token)
    тело = web.route(conn, web.parse(head, "")).body
    assert "<script>alert" not in тело
