"""Поиск лент: правила вместо догадки, и без единого похода в сеть.

Проверяются чистые функции — нормализация адреса, разбор главной страницы
на кандидатов, принадлежность домену, разбор Atom и запись ленты в базу.
Сами походы в сеть — на живом сервере пробником, не в тестах: чужой сайт не
обязан быть живым ради наших проверок [DOC-003].
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from news import discover, sources, store


def test_домен_дополняется_до_адреса() -> None:
    assert discover.нормализовать("example.com") == "https://example.com/"
    assert discover.нормализовать("example.com/feed/") == "https://example.com/feed/"
    assert discover.нормализовать("http://example.com") == "http://example.com/"
    # Слово без точки — не сайт: правила не выдумывают домены, это работа модели.
    assert discover.нормализовать("Коммерсантъ") == ""
    assert discover.нормализовать("http://exa mple.com") == ""
    assert discover.нормализовать("") == ""


def test_локальный_адрес_не_проверяется() -> None:
    """Поле ввода открыто человеку, а HTTP-клиент ходит от имени сервера [CORE-016]."""
    assert asyncio.run(discover.наружный("https://localhost/feed/")) is False
    assert asyncio.run(discover.наружный("https://127.0.0.1:6769/")) is False
    assert asyncio.run(discover.наружный("https://192.168.1.5/feed/")) is False
    assert asyncio.run(discover.наружный("https://example.com:22/feed/")) is False


ГЛАВНАЯ = """
<html><head>
<link rel="alternate" type="application/rss+xml" href="/feed/">
<link rel="stylesheet" type="text/css" href="/style.css">
<link rel="alternate" type="application/atom+xml" href="https://feeds.example.com/atom.xml">
</head><body><a href="/news/rss.xml">RSS</a></body></html>
"""


def test_главная_объявляет_ленты_в_порядке_надёжности() -> None:
    found = discover.из_главной(ГЛАВНАЯ, "example.com")
    assert found[0] == "https://example.com/feed", "автодискавери идёт первым"
    assert "https://feeds.example.com/atom.xml" in found
    assert "https://example.com/news/rss.xml" in found, "якорь с «rss» в адресе — тоже кандидат"
    assert all("/style.css" not in адрес for адрес in found)


def test_чужой_агрегатор_не_проходит_проверку_домена() -> None:
    assert discover.тот_же_домен("https://feeds.example.com/rss", "https://example.com")
    assert discover.тот_же_домен("https://example.com/feed/", "https://www.example.com")
    assert not discover.тот_же_домен("https://feedburner.google.com/x", "https://example.com")
    assert not discover.тот_же_домен("https://notexample.com/feed/", "https://example.com")


def test_адрес_с_путём_не_водит_на_главную() -> None:
    """Указали саму ленту — она и проверяется, без захода на главную."""
    прямой = discover.кандидаты("https://example.com/rss.xml")
    assert прямой == ["https://example.com/rss.xml"]
    обычные = discover.кандидаты("https://example.com")
    assert обычные[0] == "https://example.com/feed/"
    assert "https://example.com/atom.xml" in обычные


ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Пример</title>
  <entry>
    <title>Первая</title>
    <link rel="alternate" href="https://example.com/news/1"/>
    <link rel="enclosure" href="https://example.com/media/1.mp3"/>
    <published>2026-09-29T10:00:00Z</published>
    <summary>лид первой</summary>
  </entry>
  <entry>
    <title>Вторая</title>
    <link href="https://example.com/news/2"/>
    <updated>2026-09-29T11:00:00Z</updated>
    <summary>лид второй</summary>
  </entry>
  <entry>
    <title>Третья</title>
    <link rel="enclosure" href="https://example.com/media/3.mp3"/>
    <link rel="alternate" href="https://example.com/news/3"/>
  </entry>
</feed>
"""


def test_атом_разбирается_тем_же_разбором() -> None:
    """Пространство имён и атрибутные ссылки не должны ронять разбор [CORE-017]."""
    проба = sources.Source(code="проба", label="проба", door="https://example.com/atom.xml",
                           kind="rss", interval=15.0, conditional=False,
                           host="https://example.com")
    found = sources.extract(проба, ATOM)
    assert len(found) == 3
    assert all(item.url.startswith("https://example.com/news/") for item in found), (
        "enclosure — не адрес новости")
    assert found[0].published_at == "2026-09-29T10:00:00Z"
    assert found[1].published_at == "2026-09-29T11:00:00Z", "время Atom берётся из updated"
    assert found[2].published_at == "", "у третьей записи времени нет — это не ошибка"
    assert found[0].lead == "лид первой"
    assert discover.название_ленты(ATOM) == "Пример"


def test_лента_записывается_и_выкидывается(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    code = store.add_feed(conn, label="Пример", door="https://example.com/feed/",
                          host="example.com", added_by=1)
    assert code == "example"
    known = sources.registry(conn)
    assert known[code].door == "https://example.com/feed/"
    assert known[code].kind == "rss"
    assert store.feed_label(conn, code) == "Пример"
    assert code not in sources.BY_CODE, "код добавленной не должен занимать встроенный"
    # Занятый домен получает суффикс: два источника с одним кодом — одна выдача.
    assert store.add_feed(conn, label="Ещё", door="https://example.com/other.xml",
                          host="example.com") == "example-2"
    # Встроенный источник кодом не убирается — только тумблером.
    assert store.drop_feed(conn, "meduza") is False
    assert store.drop_feed(conn, code) is True
    assert code not in sources.registry(conn)


def test_очередь_не_принимает_дубликатов() -> None:
    discover.ЗАДАНИЯ.clear()
    try:
        assert discover.попросить("example.com", 1) is True
        assert discover.попросить("example.com", 2) is False, "дубль не нужен"
        assert discover.попросить("", 1) is False
    finally:
        discover.ЗАДАНИЯ.clear()
