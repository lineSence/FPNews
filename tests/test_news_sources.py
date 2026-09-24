"""Разбор дверей: что сторож увидит в ответе ленты и страницы.

Проверяется на настоящих дампах (`tests/fixtures/news/`), а не на выдуманной
разметке: у Фонтанки классы — хэши сборки, и синтетика ничего бы не доказала.
"""

from __future__ import annotations

from pathlib import Path

from news import sources

FIXTURES = Path(__file__).parent / "fixtures" / "news"


def test_медуза_отдаёт_список_со_временем() -> None:
    body = (FIXTURES / "meduza-rss.xml").read_text(encoding="utf-8")
    found = sources.extract(sources.MEDUZA, body)
    assert len(found) >= 3
    assert all(item.url.startswith("https://meduza.io/") for item in found)
    assert all(item.title for item in found)
    # Время публикации у Медузы есть прямо в ленте — редакционная задержка
    # считается без открытия материала [NEWS-001].
    assert all(item.published_at for item in found)


def test_фонтанка_разбирается_по_адресу_а_не_по_классам() -> None:
    body = (FIXTURES / "fontanka-24hours.html").read_text(encoding="utf-8")
    found = sources.extract(sources.FONTANKA, body)
    assert len(found) >= 10
    assert all("/2026/" in item.url or "/2025/" in item.url for item in found)
    assert all(item.title for item in found), "заголовок берётся из текста ссылки"
    assert len({item.url for item in found}) == len(found), "дубли схлопнуты"


def test_один_материал_не_превращается_в_три_адреса() -> None:
    """Метки переходов и якорь — не часть адреса."""
    base = "https://www.fontanka.ru/2026/09/24/76658444"
    for tail in ("/", "/?from=main", "/?utm_source=telegram&utm_medium=cpc", "/#comments"):
        assert sources.canonical(base + tail) == base
    # Относительная ссылка достраивается хостом источника.
    assert sources.canonical("/2026/09/24/76658444/", sources.FONTANKA.host) == base


def test_полезный_параметр_остаётся() -> None:
    """Режем только мусор: чужой параметр может быть частью адреса."""
    assert sources.canonical("https://meduza.io/news?page=2") == "https://meduza.io/news?page=2"


def test_битый_ответ_не_роняет_разбор() -> None:
    """Сторож не имеет права падать от чужой ошибки [CORE-017]."""
    assert sources.extract(sources.MEDUZA, "<html>503</html>") == []
    assert sources.extract(sources.FONTANKA, "") == []


def test_у_каждого_источника_своя_политика() -> None:
    """[NEWS-005]: общей настройки интервала не существует."""
    assert sources.MEDUZA.conditional and sources.MEDUZA.interval <= 10
    assert not sources.FONTANKA.conditional and sources.FONTANKA.interval >= 15
    assert all(source.fallback for source in sources.ALL)
