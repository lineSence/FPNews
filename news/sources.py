"""Источники: дверь, политика опроса и разбор списка ссылок.

Дверь — адрес, который сторож дёргает, чтобы узнать «появилось ли что-то
новое». У каждого издания она своя, и это свойство источника, а не общая
настройка `[NEWS-005]`: у Медузы дешёвый условный запрос к RSS, у Фонтанки —
страница суток, потому что её лента весит 545 КБ и отвечает три секунды.

Замеры дверей и обоснование выбора — `docs/news-sources.md`.
"""

from __future__ import annotations

import re
from typing import Any
from dataclasses import dataclass
from xml.etree import ElementTree

# Мусорные метки в адресах: с ними один и тот же материал выглядит как разные.
JUNK_PARAMS = ("utm_", "from", "ysclid", "erid", "fbclid", "gclid")
# Ссылка на материал Фонтанки: /2026/09/24/76658444/. Год и число знаков
# проверяются, потому что этому же шаблону не должны соответствовать разделы.
FONTANKA_ITEM = re.compile(r"/(20\d\d)/(\d\d)/(\d\d)/(\d{6,9})/?$")
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
# Деловой Петербург кладёт полный текст в тег для Яндекса. Нам он тоже
# годится: это тот же материал, и он снимает поход на страницу.
YANDEX_FULL = "{http://news.yandex.ru}full-text"
# Схемы, которые мы согласны считать ссылкой на материал. Всё остальное —
# `javascript:`, `data:`, `mailto:` — не адрес новости, а способ выполнить
# чужой код в браузере читателя или в клиенте Telegram [CORE-016]. Проверка
# стоит здесь, а не на странице: адрес из ленты уезжает и в базу, и в бот, и
# чинить его в каждой точке вывода поздно.
СХЕМЫ = ("http://", "https://")
# Схема в начале строки: «javascript:», «data:», «tel:». Нужна, чтобы
# отличить чужую схему от обычной относительной ссылки «2026/09/24/1/».
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
ANCHOR = re.compile(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
TAG = re.compile(r"<[^>]+>|<!--.*?-->", re.S)
SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Found:
    """Одна строчка списка: что вообще можно узнать, не открывая материал.

    У Медузы в ленте лежит весь текст материала (`content:encoded`), поэтому
    `body` приходит заполненным и заходить на страницу не нужно вовсе — минус
    одно сетевое обращение на каждой новости. У Фонтанки в списке только адрес
    и заголовок, текст качается отдельно.
    """

    url: str
    title: str = ""
    published_at: str = ""
    lead: str = ""
    body: str = ""

    @property
    def whole(self) -> bool:
        """Материал пришёл целиком: разбор страницы не нужен."""
        return bool(self.body)


@dataclass(frozen=True)
class Source:
    """Описание источника целиком: дверь, как её читать и как часто.

    `conditional` — понимает ли дверь `If-Modified-Since`. У Медузы да, и тогда
    опрос стоит сорок миллисекунд и ноль байт. У Фонтанки нет ни `ETag`, ни
    `Last-Modified`, поэтому каждый опрос — полный ответ, и интервал крупнее.
    """

    code: str
    label: str
    door: str
    kind: str  # "rss" или "html"
    interval: float
    conditional: bool
    fallback: str = ""
    host: str = ""
    # Пометка, которую обязан нести пересказ материала (например, статус
    # иностранного агента). Живёт в описании источника, а не в шаблоне
    # сообщения: это свойство издания [NEWS-005].
    notice: str = ""


MEDUZA = Source(
    code="meduza",
    label="Медуза",
    door="https://meduza.io/rss/all",
    kind="rss",
    interval=10.0,
    conditional=True,
    fallback="https://meduza.io/api/w5/screens/news?locale=ru",
    host="https://meduza.io",
    notice="издание признано в РФ иностранным агентом",
)

FONTANKA = Source(
    code="fontanka",
    label="Фонтанка",
    door="https://www.fontanka.ru/24hours/",
    kind="html",
    interval=15.0,
    conditional=False,
    fallback="https://www.fontanka.ru/rss-feeds/zen-news.xml",
    host="https://www.fontanka.ru",
)

INTERFAX = Source(
    code="interfax",
    label="Интерфакс",
    # Лучшая дверь из всех измеренных: 5,8 КБ, треть секунды, честный
    # Last-Modified. Повтор стоит ноль байт, поэтому интервал маленький.
    door="https://www.interfax.ru/rss",
    kind="rss",
    interval=10.0,
    conditional=True,
    host="https://www.interfax.ru",
)

DP = Source(
    code="dp",
    label="Деловой Петербург",
    # Ответ дорогой: 210 КБ и секунда-четыре. Валидаторы дверь отдаёт, но
    # перегенерирует ленту постоянно, поэтому 304 приходит редко — отсюда
    # интервал вдвое крупнее прочих. Зато текст материала есть прямо в ленте,
    # и страницу открывать не нужно.
    door="https://www.dp.ru/exportnews.xml",
    kind="rss",
    interval=30.0,
    conditional=True,
    host="https://www.dp.ru",
)

RIA = Source(
    code="ria",
    label="РИА Новости",
    # В ленте только заголовок и адрес — за текстом идём на страницу.
    door="https://ria.ru/export/rss2/archive/index.xml",
    kind="rss",
    interval=15.0,
    conditional=False,
    host="https://ria.ru",
)

MOIKA = Source(
    code="moika",
    label="Мойка78",
    # Всего десять записей в ленте: при всплеске новостей она вымывается за
    # минуты, поэтому опрашивать реже нельзя — пропуск дороже трафика
    # [NEWS-004].
    door="https://moika78.ru/feed/",
    kind="rss",
    interval=15.0,
    conditional=False,
    host="https://moika78.ru",
)

PAPER = Source(
    code="paper",
    label="Бумага",
    # Домен paperpaper.ru продан: там теперь сайт про микрозаймы. Издание
    # живёт на .io, лента обычная вордпрессовская, с текстом целиком.
    door="https://paperpaper.io/feed/",
    kind="rss",
    interval=15.0,
    conditional=True,
    host="https://paperpaper.io",
    notice="издание признано в РФ иностранным агентом",
)

ALL = (MEDUZA, FONTANKA, INTERFAX, DP, RIA, MOIKA, PAPER)
BY_CODE = {source.code: source for source in ALL}

def registry(conn: Any) -> dict[str, Source]:
    """Все источники: встроенные из кода и добавленные в интерфейсе из базы.

    Код и разбор у них одинаковый, разница только в том, кто записал —
    человек в форме или мы в этой странице. Собираем их в один словарь на
    момент вызова, чтобы сторожа и страницы видели одно и то же `[NEWS-005]`.
    """
    from . import store  # noqa: PLC0415 — импорт здесь разрывает круг

    out = dict(BY_CODE)
    for source in store.custom_sources(conn):
        out[source.code] = source
    return out


def canonical(url: str, host: str = "") -> str:
    """Адрес без меток переходов и якоря, с полным именем хоста.

    Без этого один материал из ленты, из письма и со страницы суток выглядит
    как три разных: `?from=main`, `?utm_source=`, `#comments`.
    """
    raw = "".join(знак for знак in (url or "").strip()
                  if ord(знак) >= 32 and ord(знак) != 127)
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = host.rstrip("/") + raw
    elif host and not SCHEME.match(raw):
        # Относительная ссылка без ведущей косой: у Фонтанки в разметке
        # встречается и такая.
        raw = host.rstrip("/") + "/" + raw
    if not raw.lower().startswith(СХЕМЫ):
        # Пусто, а не исключение: разбор ленты не должен падать от одной
        # странной ссылки, а сторож пропустит такую строку [CORE-017].
        return ""
    raw = raw.split("#", 1)[0]
    head, _, query = raw.partition("?")
    if query:
        kept = [
            part
            for part in query.split("&")
            if part and not any(part.lower().startswith(junk) for junk in JUNK_PARAMS)
        ]
        raw = head + ("?" + "&".join(kept) if kept else "")
    return raw.rstrip("/") if raw.count("/") > 3 else raw


def text_of(html: str) -> str:
    """Внутренность тега без разметки и комментариев Vue (`<!--[-->`)."""
    return SPACE.sub(" ", TAG.sub(" ", html or "")).strip()


def extract(source: Source, body: str) -> list[Found]:
    """Список материалов из ответа двери. Порядку не доверяем.

    Лента Фонтанки для Дзена не отсортирована по времени, поэтому новизна
    определяется только по множеству известных адресов, а не по позиции.
    """
    if source.kind == "rss":
        return _from_rss(body, source)
    return _from_html(body, source)


def _from_rss(body: str, source: Source) -> list[Found]:
    """Разбор XML-ленты: RSS и Atom, с любыми пространствами имён.

    Теги ленты расставляют по-разному: RSS 2.0 обходится без пространств,
    RSS 1.0 живёт в своих, Atom — в пространстве w3.org. Точное имя тега —
    свойство чужой разметки, поэтому сравниваем местные имена: `item`,
    `entry`, `link`, `title` — без префиксов `[CORE-017]`.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    out: list[Found] = []
    seen: set[str] = set()
    entries = _имена(root, "item") or _имена(root, "entry")
    for item in entries:
        link = canonical((_текст(item, "link").strip() or _ссылка(item)), source.host)
        if not link or link in seen:
            continue
        seen.add(link)
        whole = text_of(_текст(item, "encoded") or _текст(item, "full-text")
                        or _текст(item, "content") or "")
        lead = text_of(_текст(item, "description") or _текст(item, "summary") or "")
        out.append(
            Found(
                url=link,
                title=(_текст(item, "title") or "").strip(),
                published_at=(_текст(item, "pubDate") or _текст(item, "published")
                              or _текст(item, "updated") or "").strip(),
                lead=lead or whole[:400],
                body=whole,
            )
        )
    return out

def _местное(tag: str) -> str:
    """Имя тега без пространства имён. Ленты их расставляют по-разному."""
    return str(tag or "").rsplit("}", 1)[-1]

def _имена(root: Any, name: str) -> list[Any]:
    """Все элементы с этим именем — независимо от пространства имён."""
    return [node for node in root.iter() if _местное(node.tag) == name]

def _текст(node: Any, name: str) -> str:
    """Текст первого вложенного тега с этим именем. Пусто — тега не было."""
    for child in node:
        if _местное(child.tag) == name:
            return child.text or ""
    return ""

def _ссылка(node: Any) -> str:
    """Адрес из Atom-тега `<link>`: он в атрибуте, и их бывает несколько.

    Атрибут `rel` без значения и со значением `alternate` — сам материал;
    прочие (enclosure, replies) адресом новости не являются.
    """
    any_link = ""
    for child in node:
        if _местное(child.tag) != "link":
            continue
        href = (child.get("href") or "").strip()
        if not href:
            continue
        if (child.get("rel") or "alternate") == "alternate":
            return href
        any_link = any_link or href
    return any_link


def _from_html(body: str, source: Source) -> list[Found]:
    """Разбор страницы суток по шаблону адреса, а не по классам.

    Классы на сайте Фонтанки — хэши сборки (`item_qKDDK`), они меняются при
    каждом релизе вёрстки. Адрес материала не меняется годами, поэтому ссылки
    ищутся по нему, а заголовком становится текст самой ссылки. Одна и та же
    новость встречается на странице несколько раз (лента, сюжеты, блок
    «главное») — берём самый длинный из найденных заголовков.
    """
    best: dict[str, str] = {}
    for href, inner in ANCHOR.findall(body or ""):
        url = canonical(href, source.host)
        if not FONTANKA_ITEM.search(url):
            continue
        title = text_of(inner)
        if len(title) > len(best.get(url, "")):
            best[url] = title
    return [Found(url=url, title=title) for url, title in best.items()]


__all__ = (
    "ALL",
    "BY_CODE",
    "DP",
    "FONTANKA",
    "INTERFAX",
    "MOIKA",
    "PAPER",
    "RIA",
    "YANDEX_FULL",
    "СХЕМЫ",
    "SCHEME",
    "Found",
    "MEDUZA",
    "Source",
    "registry",
    "canonical",
    "extract",
    "text_of",
)
