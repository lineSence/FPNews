"""Темы: кому эта новость нужна. Только правила, никакой модели.

Отбор стоит в горячем пути, поэтому здесь нет ни сети, ни векторов
`[NEWS-002]`. Тема — список слов, как запросы профиля в родительском проекте.

Русский текст ловится по основе слова, а не по точному вхождению: «дрон»
должен находить «дроны» и «дронов», иначе половина попаданий теряется на
падежах. Основу короче четырёх букв не берём — «ЕС» не должен ловить «если».

Порог намеренно низкий: достаточно одного совпадения `[NEWS-004]`. Лишнее
сообщение стоит раздражения, пропущенное — потери смысла всей затеи. Зато
видно, **где** нашлось: совпадение в заголовке сильнее, чем в теле, и это
попадает в сообщение, чтобы человек сам решал, открывать ли.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WORD = re.compile(r"[0-9a-zа-я]+")
SPACE = re.compile(r"\s+")
# Со слова такой длины ищем по основе: «дрон» → «дрона», «дронами».
STEM_FROM = 5
MIN_STEM = 4


@dataclass(frozen=True)
class Hit:
    """Почему тема сработала: какие слова и где именно."""

    topic_id: int
    user_id: int
    title: str
    words: tuple[str, ...]
    in_title: bool

    @property
    def strength(self) -> str:
        return "в заголовке" if self.in_title else "в тексте"


def normalize(text: object) -> str:
    return SPACE.sub(" ", str(text or "").lower().replace("ё", "е")).strip()


def parse_words(raw: str) -> list[str]:
    """Слова темы из строки через запятую. Фраза остаётся фразой."""
    out: list[str] = []
    for part in str(raw or "").split(","):
        word = normalize(part)
        if word and word not in out:
            out.append(word)
    return out


def _stem(word: str) -> str:
    return word[:-1] if len(word) >= STEM_FROM else word


def matched(words: list[str], text: str) -> list[str]:
    """Какие слова темы нашлись в тексте. Пустой список — не по теме."""
    hay = normalize(text)
    if not hay:
        return []
    tokens = WORD.findall(hay)
    found: list[str] = []
    for word in words:
        if " " in word:
            # Фраза ищется целиком: «северный поток» не должен срабатывать на
            # «поток машин» и «северный ветер» по отдельности.
            if word in hay:
                found.append(word)
            continue
        stem = _stem(word)
        if len(stem) < MIN_STEM:
            if word in tokens:
                found.append(word)
            continue
        if any(token == word or token.startswith(stem) for token in tokens):
            found.append(word)
    return found


def pick(topics: list[dict], title: str, body: str = "", source: str = "") -> list[Hit]:
    """Темы, которым подходит новость. Порядок — сначала попавшие в заголовок."""
    hits: list[Hit] = []
    for topic in topics:
        if not topic.get("enabled", 1):
            continue
        only = [code for code in str(topic.get("sources") or "").split(",") if code.strip()]
        if only and source and source not in [code.strip() for code in only]:
            continue
        words = parse_words(topic.get("words") or "")
        if not words:
            continue
        in_title = matched(words, title)
        in_body = matched(words, body) if not in_title else []
        if not in_title and not in_body:
            continue
        hits.append(
            Hit(
                topic_id=int(topic.get("id") or 0),
                user_id=int(topic.get("user_id") or 0),
                title=str(topic.get("title") or ""),
                words=tuple(in_title or in_body),
                in_title=bool(in_title),
            )
        )
    hits.sort(key=lambda hit: (not hit.in_title, -len(hit.words)))
    return hits


__all__ = ("Hit", "matched", "normalize", "parse_words", "pick")
