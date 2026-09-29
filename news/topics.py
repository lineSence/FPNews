"""Темы: кому эта новость нужна. Профили, а не модель.

Отбор стоит в горячем пути, поэтому здесь нет ни сети, ни векторов
`[NEWS-002]`. Тема — концептный профиль: взвешенные слова-синонимы,
стоп-слова и порог, а не одно ключевое слово.

Русский текст ловится по основе слова, а не по точному вхождению: «дрон»
должен находить «дроны» и «дронов», иначе половина попаданий теряется на
падежах. Когда в системе есть pymorphy3, основа превращается в лемму:
«нейросеть» ловит «нейросетями», и никакого нечёткого сравнения. Без него
остаётся усечение последней буквы — то же поведение, что и раньше.
Основу короче четырёх букв не берём — «ЕС» не должен ловить «если».

Порог намеренно низкий: достаточно одного совпадения `[NEWS-004]`. Лишнее
сообщение стоит раздражения, пропущенное — потери смысла всей затеи. Зато
видно, **где** нашлось: совпадение в заголовке сильнее, чем в теле, и это
попадает в сообщение, чтобы человек сам решал, открывать ли.

Слово может нести вес: «дрон*2» считается за два совпадения. Стоп-слова
выбрасывают совпавший кусок текста из подсчёта: тема «процесс» со стоп-словом
«процессор» не должна срабатывать на обзоре чипов. Все правила детерминированы:
одинаковый текст даёт одинаковый вердикт, и это проверяется тестами.
"""

from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass

try:  # pymorphy3 — чистый Python без сервисов; если его нет, работаем по основе
    from pymorphy3 import MorphAnalyzer
    _MORPH = MorphAnalyzer()
except ImportError:  # прагматично: сервер может быть ещё без новой зависимости
    _MORPH = None

WORD = re.compile(r"[0-9a-zа-я]+")
SPACE = re.compile(r"\s+")
# Со слова такой длины ищем по основе: «дрон» → «дрона», «дронами».
STEM_FROM = 5
MIN_STEM = 4
# Вес в слове темы: «важное*3» — за одно совпадение три очка.
WEIGHT = re.compile(r"^(.*?)\*(\d+(?:[.,]\d+)?)$")


@dataclass(frozen=True)
class Hit:
    """Почему тема сработала: какие слова и где именно."""

    topic_id: int
    user_id: int
    title: str
    words: tuple[str, ...]
    in_title: bool
    score: float = 0.0

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


def _вес(word: str) -> tuple[str, float]:
    """Слово темы → (слово без веса, вес). «важное*3» → («важное», 3)."""
    found = WEIGHT.match(word)
    if not found:
        return word, 1.0
    try:
        return normalize(found.group(1)), float(found.group(2).replace(",", "."))
    except ValueError:
        return word, 1.0


def parse_profile(raw: str) -> list[tuple[str, float]]:
    """Слова темы с весами: [(«дрон», 1.0), («важное», 3.0)]."""
    out: list[tuple[str, float]] = []
    for word in parse_words(raw):
        base, weight = _вес(word)
        if base and weight > 0:
            out.append((base, weight))
    return out


@lru_cache(maxsize=65536)
def _lemma(token: str) -> str:
    """Лемма токена по словарю. Нет словаря или слова в нём — сам токен.

    Токен уже нормализован: строчные буквы, «ё» заменена. Кэш стоит потому,
    что тема прогоняется по каждому новому материалу, а словарь большой.
    """
    if _MORPH is None:
        return token
    parsed = _MORPH.parse(token)
    if not parsed:
        return token
    form = parsed[0].normal_form
    # Чужую основу словарь именно угадывает: «бпла» → «бпнуть». Угаданное
    # не берём: форма должна начинаться как само слово, иначе слово —
    # сам себе лемма. Проверка буквальная, потому и предсказуемая.
    if len(token) >= 3 and not form.startswith(token[:3]):
        return token
    return form


def _stem(word: str) -> str:
    return word[:-1] if len(word) >= STEM_FROM else word


def matched(words: list[str], text: str) -> list[str]:
    """Какие слова темы нашлись в тексте. Пустой список — не по теме."""
    return [word for word, _ in _matched_tokens(words, text)]


def _matched_tokens(words: list[str], text: str) -> list[tuple[str, set[str]]]:
    """Какие слова темы нашлись и на каких именно токенах текста.

    Токены нужны стоп-словам: совпавший кусок текста выбрасывается из
    подсчёта целиком, а не «слово темы перестаёт работать везде».
    """
    hay = normalize(text)
    if not hay:
        return []
    tokens = WORD.findall(hay)
    found: list[tuple[str, set[str]]] = []
    for word in words:
        if " " in word:
            # Фраза ищется целиком: «северный поток» не должен срабатывать на
            # «поток машин» и «северный ветер» по отдельности.
            if word in hay:
                found.append((word, {word}))
            continue
        base = _lemma(word)
        stem = _stem(word)
        if len(stem) < MIN_STEM:
            if word in tokens:
                found.append((word, {word}))
            continue
        here = {
            token for token in tokens
            if token == word or token.startswith(stem) or _lemma(token) == base
        }
        if here:
            found.append((word, here))
    return found


def suggest_words(known: list[str], stop: list[str], texts: list[str],
                   limit: int = 3) -> list[tuple[str, int]]:
    """Что добавить в тему по кнопкам «в тему»: частые леммы, которых нет.

    Правила честные и подсчитываемые: лемма встретилась в двух разных
    материалах, отмеченных «в тему», не входит в слова и стоп-слова — она
    кандидат. Предложение, а не изменение: человек нажимает «добавить» сам.
    """
    have = {word for word in known if word} | {word for word in stop if word}
    stop_tokens: set[str] = set()
    for word in stop:
        for _, tokens in _matched_tokens([word], " ".join(texts)):
            stop_tokens |= tokens
    counts: dict[str, int] = {}
    # считаем по леммам в каждом тексте один раз
    for text in texts:
        hay = normalize(text)
        seen = set()
        for token in WORD.findall(hay):
            lemma = _lemma(token)
            if len(lemma) < MIN_STEM or lemma in have or lemma in seen:
                continue
            if lemma in stop_tokens:
                continue
            seen.add(lemma)
            counts[lemma] = counts.get(lemma, 0) + 1
    ranked = sorted(
        ((lemma, count) for lemma, count in counts.items() if count >= 2),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return ranked[:limit]


def pick(topics: list[dict], title: str, body: str = "", source: str = "") -> list[Hit]:
    """Темы, которым подходит новость. Порядок — сначала попавшие в заголовок."""
    hits: list[Hit] = []
    for topic in topics:
        if not topic.get("enabled", 1):
            continue
        only = [code for code in str(topic.get("sources") or "").split(",") if code.strip()]
        if only and source and source not in [code.strip() for code in only]:
            continue
        profile = parse_profile(topic.get("words") or "")
        stop = parse_words(topic.get("stopwords") or "")
        if not profile:
            continue
        words = [word for word, _ in profile]
        hit_title = _matched_tokens(words, title)
        hit_body = _matched_tokens(words, body) if not hit_title else []
        banned = {
            token
            for _, tokens in _matched_tokens(stop, title) + _matched_tokens(stop, body)
            for token in tokens
        }
        counted = [
            (word, tokens - banned)
            for word, tokens in hit_title or hit_body
            if tokens - banned
        ]
        if not counted:
            continue
        weights = dict(profile)
        score = sum(weights.get(word, 1.0) for word, _ in counted)
        try:
            threshold = float(str(topic.get("threshold") or 1).replace(",", "."))
        except ValueError:
            threshold = 1.0
        if score < max(0.0, threshold):
            continue
        in_title = bool(hit_title)
        hits.append(
            Hit(
                topic_id=int(topic.get("id") or 0),
                user_id=int(topic.get("user_id") or 0),
                title=str(topic.get("title") or ""),
                words=tuple(word for word, _ in counted),
                in_title=in_title,
                score=score,
            )
        )
    hits.sort(key=lambda hit: (not hit.in_title, -hit.score, -len(hit.words)))
    return hits


__all__ = ("Hit", "matched", "normalize", "parse_profile", "parse_words", "pick",
           "suggest_words")
