"""Источники: дверь, политика опроса и разбор списка ссылок.

Дверь — адрес, который сторож дёргает, чтобы узнать «появилось ли что-то
новое». У каждого издания она своя, и это свойство источника, а не общая
настройка `[NEWS-005]`: у Медузы дешёвый условный запрос к RSS, у Фонтанки —
страница суток, потому что её лента весит 545 КБ и отвечает три секунды.

Замеры дверей и обоснование выбора — `docs/news-sources.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

# Мусорные метки в адресах: с ними один и тот же материал выглядит как разные.
JUNK_PARAMS = ("utm_", "from", "ysclid", "erid", "fbclid", "gclid")
# Ссылка на материал Фонтанки: /2026/09/24/76658444/. Год и число знаков
# проверяются, потому что этому же шаблону не должны соответствовать разделы.
FONTANKA_ITEM = re.compile(r"/(20\d\d)/(\d\d)/(\d\d)/(\d{6,9})/?$")
ANCHOR = re.compile(r"<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
TAG = re.compile(r"<[^>]+>|<!--.*?-->", re.S)
SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Found:
    """Одна строчка списка: что вообще можно узнать, не открывая материал."""

    url: str
    title: str = ""
    published_at: str = ""


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


MEDUZA = Source(
    code="meduza",
    label="Медуза",
    door="https://meduza.io/rss/all",
    kind="rss",
    interval=10.0,
    conditional=True,
    fallback="https://meduza.io/api/w5/screens/news?locale=ru",
    host="https://meduza.io",
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

ALL = (MEDUZA, FONTANKA)
BY_CODE = {source.code: source for source in ALL}


def canonical(url: str, host: str = "") -> str:
    """Адрес без меток переходов и якоря, с полным именем хоста.

    Без этого один материал из ленты, из письма и со страницы суток выглядит
    как три разных: `?from=main`, `?utm_source=`, `#comments`.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = host.rstrip("/") + raw
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
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    out: list[Found] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        link = canonical((item.findtext("link") or "").strip(), source.host)
        if not link or link in seen:
            continue
        seen.add(link)
        out.append(
            Found(
                url=link,
                title=(item.findtext("title") or "").strip(),
                published_at=(item.findtext("pubDate") or "").strip(),
            )
        )
    return out


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
    "FONTANKA",
    "Found",
    "MEDUZA",
    "Source",
    "canonical",
    "extract",
    "text_of",
)
