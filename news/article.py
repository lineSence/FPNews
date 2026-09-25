"""Разбор страницы материала. Нужен тем источникам, где текста нет в ленте.

У Медузы текст приходит вместе со списком, у Фонтанки — нет, и за ним надо
идти на страницу. Это второе сетевое обращение на новость, и оно стоит вне
пути к сырому сообщению: заголовок и ссылка уходят раньше `[NEWS-003]`.

Разбор идёт по `ld+json` (schema.org Article), а не по вёрстке: там лежат
заголовок, время публикации и полный `articleBody`. Классы на странице —
хэши сборки и меняются при каждом релизе, а разметка для поисковиков
стабильна, потому что от неё зависит их собственный трафик. Если её не
окажется — откат на мета-теги Open Graph и абзацы `<p>`.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from . import fetch, sources

log = logging.getLogger("fpnews.article")

LD = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
META = re.compile(r'<meta[^>]+property="(og:[a-z:]+)"[^>]+content="([^"]*)"', re.I)
PARA = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
# Запасной вариант запасного: РИА верстает абзацы не тегом <p>, а блоками
# `div.article__text`, и без этого от их молний остаётся пустой текст. По
# классам мы разбирать не любим (они меняются), но здесь это последняя
# попытка перед пустотой, и имя класса сужено до «article…text», чтобы не
# затащить форму регистрации и подвал [NEWS-004].
BLOCK = re.compile(r'<div[^>]+class="[^"]*article[^"]*text[^"]*"[^>]*>(.*?)</div>', re.S | re.I)
LEAD_LIMIT = 400


@dataclass(frozen=True)
class Parsed:
    title: str = ""
    published_at: str = ""
    lead: str = ""
    body: str = ""

    @property
    def empty(self) -> bool:
        return not (self.title or self.body)


def _articles(body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in LD.findall(body or ""):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in data.get("@graph", [data]) if isinstance(data, dict) else data:
            if isinstance(node, dict) and "Article" in str(node.get("@type", "")):
                out.append(node)
    return out


def parse(body: str) -> Parsed:
    """Заголовок, время и текст со страницы. Пустой результат — не исключение."""
    for node in _articles(body):
        text = sources.text_of(html_lib.unescape(str(node.get("articleBody") or "")))
        if not text:
            continue
        return Parsed(
            title=html_lib.unescape(str(node.get("headline") or "")).strip(),
            published_at=str(node.get("datePublished") or "").strip(),
            lead=text[:LEAD_LIMIT],
            body=text,
        )
    return _fallback(body)


SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def _fallback(body: str) -> Parsed:
    """Запасной разбор: Open Graph плюс абзацы. Хуже, но лучше пустоты."""
    meta = {key.lower(): html_lib.unescape(value) for key, value in META.findall(body or "")}
    # Скрипты выбрасываем до разбора: иначе в «текст материала» попадает
    # разметка для поисковиков и счётчики.
    body = SCRIPT.sub(" ", body or "")
    chunks = [sources.text_of(chunk) for chunk in PARA.findall(body or "")]
    text = " ".join(chunk for chunk in chunks if len(chunk) > 40)
    if not text:
        blocks = [sources.text_of(chunk) for chunk in BLOCK.findall(body or "")]
        text = " ".join(chunk for chunk in blocks if len(chunk) > 40)
    lead = meta.get("og:description", "") or text[:LEAD_LIMIT]
    return Parsed(
        title=meta.get("og:title", "").strip(),
        published_at="",
        lead=lead[:LEAD_LIMIT],
        body=text,
    )


async def load_page(session: Any, url: str) -> tuple[Any, Parsed]:
    """Опрос и разбор вместе: нужен и код ответа, и исходный HTML.

    Код ответа отличает «сеть моргнула» от «материал сняли», а исходный HTML —
    это доказательная копия: пересказ проверить нельзя, копию можно
    `[NEWS-007]`.
    """
    poll = await fetch.poll(session, url, fetch.Door())
    if not poll.body:
        log.warning("страница %s не прочиталась: код %s %s", url, poll.status, poll.error)
        return poll, Parsed()
    return poll, parse(poll.body)


async def load(session: Any, url: str) -> Parsed:
    """Скачать и разобрать. Сбой сети — пустой разбор, а не исключение."""
    _, parsed = await load_page(session, url)
    return parsed


__all__ = ("BLOCK", "Parsed", "SCRIPT", "load", "load_page", "parse")
