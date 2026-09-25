"""Выгрузка и состояние: то же, что на экране, но файлом и цифрами."""

from __future__ import annotations

from pathlib import Path

from news import bot as bot_module
from news import entities, export, store, web


def _conn(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    return conn


def _add(conn, title: str, source: str = "fontanka") -> int:
    адрес = "https://example.org/{}".format(abs(hash(title + source)) % 10**9)
    item_id, _ = store.remember(conn, source, адрес, title, store.now(),
                                published_at=store.now())
    return item_id


def test_csv_содержит_ссылки_и_заголовки(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Мост закрыли")
    _add(conn, "Тариф вырос", "dp")
    тело, тип, имя = export.make(conn, {"период": "месяц"}, "csv")
    assert тип.startswith("text/csv") and имя.endswith(".csv")
    assert тело.startswith("\ufeff"), "BOM — чтобы Excel не показал кракозябры"
    assert "Мост закрыли" in тело and "https://example.org" in тело
    assert тело.count("\r\n") == 3, "заголовок и две строки"


def test_json_и_фильтр_издания(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Мост закрыли")
    _add(conn, "Тариф вырос", "dp")
    import json  # noqa: PLC0415

    тело, тип, _ = export.make(conn, {"период": "месяц"}, "json",
                               lambda name: ["dp"] if name == "издание" else [])
    assert тип.startswith("application/json")
    данные = json.loads(тело)
    assert len(данные) == 1 and данные[0]["источник"] == "dp"


def test_выгрузка_отдаётся_файлом(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Мост закрыли")
    token = web.new_session(conn, 7)
    head = "GET /выгрузка?формат=csv HTTP/1.1\r\nHost: x\r\nCookie: {}={}".format(
        web.COOKIE, token)
    ответ = web.route(conn, web.parse(head, ""))
    assert ответ.status.startswith("200") and ответ.filename.endswith(".csv")
    assert b"Content-Disposition: attachment" in ответ.raw()


def test_состояние_считает_объём_и_молчание(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = _add(conn, "Мост закрыли")
    store.save_snapshot(conn, item, "<html>" + "страница " * 2000 + "</html>")
    entities.save(conn, item, "«Метрострой» и мост", "Иван Петров подписал.")
    размеры = store.sizes(conn)
    assert размеры["база_кб"] > 0 and размеры["копии_кб"] > 0
    assert размеры["строк"]["items"] == 1 and размеры["строк"]["mentions"] >= 1
    здоровье = {row["код"]: row for row in store.source_health(conn)}
    assert здоровье["fontanka"]["всего"] == 1 and not здоровье["fontanka"]["молчит"]
    token = web.new_session(conn, 7)
    страница = web.route(conn, web.parse(
        "GET /состояние HTTP/1.1\r\nHost: x\r\nCookie: {}={}".format(web.COOKIE, token), ""))
    assert страница.status.startswith("200") and "файл базы" in страница.body


def test_дозаполнение_сущностей_в_архиве(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item = _add(conn, "«Метрострой» получил контракт")
    conn.execute("UPDATE items SET body = ? WHERE id = ?", ("Иван Петров подписал.", item))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"] == 0
    assert entities.backfill(conn) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM mentions").fetchone()["n"] >= 2
    assert entities.backfill(conn) == 0, "второй проход не делает ту же работу"
