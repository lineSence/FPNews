"""Отбор по теме: правила, падежи и осознанно мягкий порог."""

from __future__ import annotations

from news import topics


def _topic(words: str, **rest):
    base = {"id": 1, "user_id": 7, "title": "тема", "words": words, "enabled": 1, "sources": ""}
    base.update(rest)
    return base


def test_слово_ловится_в_любой_форме() -> None:
    """Русский падеж не должен прятать новость."""
    words = topics.parse_words("дрон, беспилотник")
    assert topics.matched(words, "Над городом сбили дроны") == ["дрон"]
    assert topics.matched(words, "Запуск беспилотников отложен") == ["беспилотник"]
    assert topics.matched(words, "Дронов не было") == ["дрон"]


def test_короткое_слово_не_ловит_лишнего() -> None:
    """«ЕС» не должно срабатывать на «если» и «есть»."""
    words = topics.parse_words("ес")
    assert topics.matched(words, "Если честно, есть сомнения") == []
    assert topics.matched(words, "Саммит ЕС в Брюсселе") == ["ес"]


def test_фраза_ищется_целиком() -> None:
    words = topics.parse_words("северный поток")
    assert topics.matched(words, "Поток машин на северном шоссе") == []
    assert topics.matched(words, "Северный поток снова обсуждают") == ["северный поток"]


def test_заголовок_сильнее_тела() -> None:
    hits = topics.pick(
        [_topic("дрон", id=1), _topic("суд", id=2)],
        title="Дрон над Пулково",
        body="Суд рассмотрит дело",
    )
    assert [hit.topic_id for hit in hits] == [1, 2]
    assert hits[0].in_title and not hits[1].in_title
    assert hits[0].strength == "в заголовке"


def test_одного_совпадения_достаточно() -> None:
    """[NEWS-004]: пропуск дороже лишнего, порог низкий намеренно."""
    hits = topics.pick([_topic("метро, транспорт, автобус")], title="Метро закроют на ремонт")
    assert len(hits) == 1 and hits[0].words == ("метро",)


def test_тема_может_слушать_одно_издание() -> None:
    only_meduza = _topic("суд", sources="meduza")
    assert topics.pick([only_meduza], title="Суд", source="fontanka") == []
    assert len(topics.pick([only_meduza], title="Суд", source="meduza")) == 1


def test_выключенная_тема_молчит() -> None:
    assert topics.pick([_topic("суд", enabled=0)], title="Суд") == []
