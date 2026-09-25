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
    assert sources.MEDUZA.fallback and sources.FONTANKA.fallback, "у тяжёлых дверей есть запасная"
    # Дверь, интервал и условный запрос описаны у каждого, кодов не дублируется.
    assert len({source.code for source in sources.ALL}) == len(sources.ALL)
    assert all(source.door.startswith("https://") and source.interval >= 10
               for source in sources.ALL)


def test_ленты_с_текстом_целиком() -> None:
    """Где текст есть в ленте, поход на страницу не нужен."""
    поток = (
        '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:yandex="http://news.yandex.ru"><channel>'
        "<item><title>Полиция пришла с проверкой</title>"
        "<link>https://paperpaper.io/papernews/2026/9/25/policiya/</link>"
        "<pubDate>Fri, 25 Sep 2026 07:50:10 +0000</pubDate>"
        "<content:encoded>&lt;p&gt;Сотрудники пришли утром.&lt;/p&gt;</content:encoded>"
        "</item></channel></rss>"
    )
    найдено = sources.extract(sources.PAPER, поток)
    assert len(найдено) == 1 and найдено[0].whole
    assert найдено[0].body == "Сотрудники пришли утром."

    деловой = (
        '<rss xmlns:yandex="http://news.yandex.ru"><channel>'
        "<item><title>Премии и тихое увольнение</title>"
        "<link>https://www.dp.ru/a/2026/09/25/rossijanam</link>"
        "<yandex:full-text>Работодатели могут лишить премии.</yandex:full-text>"
        "</item></channel></rss>"
    )
    строки = sources.extract(sources.DP, деловой)
    assert строки[0].whole, "ДП отдаёт текст в теге для Яндекса"
    assert "лишить премии" in строки[0].body


def test_ленты_без_текста_дают_адрес_и_заголовок() -> None:
    """Интерфакс и РИА текста в ленте не дают — за ним идёт разбор страницы."""
    поток = (
        "<rss><channel><item><title>Денежная база выросла</title>"
        "<link>https://www.interfax.ru/business/1118437</link>"
        "<description>Объём составил 22837,4 млрд рублей.</description>"
        "<pubDate>Fri, 25 Sep 2026 11:05:00 +0300</pubDate>"
        "</item></channel></rss>"
    )
    найдено = sources.extract(sources.INTERFAX, поток)
    assert найдено[0].url == "https://www.interfax.ru/business/1118437"
    assert not найдено[0].whole and "22837,4" in найдено[0].lead


def test_пометка_издания_живёт_в_источнике() -> None:
    """Статус иноагента — свойство издания, а не шаблона сообщения [NEWS-005]."""
    assert sources.PAPER.notice and sources.MEDUZA.notice
    assert not sources.FONTANKA.notice


def test_разбор_страницы_без_абзацев() -> None:
    """РИА верстает молнии блоками, а не тегом <p>; скрипты в текст не идут."""
    from news import article

    страница = (
        '<html><head><meta property="og:title" content="Памфилова о выборах">'
        '<script type="application/ld+json">{"@type":"ImageObject"}</script></head>'
        '<body><div class="article__block"><div class="article__text">'
        "ЕДГ-2026 стал одной из самых сложных кампаний в новой истории страны."
        "</div></div></body></html>"
    )
    разбор = article.parse(страница)
    assert разбор.title == "Памфилова о выборах"
    assert "самых сложных кампаний" in разбор.body
    assert "ImageObject" not in разбор.body, "разметка для поисковиков — не текст"
