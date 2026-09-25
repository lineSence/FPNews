"""Разбор страницы Фонтанки и точная склейка дублей."""

from __future__ import annotations

import asyncio
from pathlib import Path

from news import article, dedup, deliver, store
from news import bot as bot_module

FIXTURES = Path(__file__).parent / "fixtures" / "news"


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str, preview: bool = True) -> bool:
        self.sent.append((chat_id, text))
        return True


def test_страница_разбирается_по_разметке_для_поисковиков() -> None:
    body = (FIXTURES / "fontanka-article.html").read_text(encoding="utf-8")
    parsed = article.parse(body)
    assert "ипотеч" in parsed.title.lower()
    assert parsed.published_at.startswith("2026-09-24T")
    assert len(parsed.body) > 200 and parsed.lead
    assert "которого нет в разметке" not in parsed.body, "ld+json важнее случайных абзацев"


def test_есть_откат_на_мета_теги() -> None:
    """Разметку для поисковиков могут убрать — разбор не должен обнулиться."""
    body = (FIXTURES / "fontanka-article.html").read_text(encoding="utf-8")
    without = body[: body.index("<script")] + body[body.index("</script>") + 9 :]
    parsed = article.parse(without)
    assert parsed.title and parsed.body
    assert not parsed.empty


def test_битая_страница_не_роняет_разбор() -> None:
    assert article.parse("<html>502 Bad Gateway</html>").empty


def test_симхэш_терпит_мелкую_правку() -> None:
    first = dedup.fingerprint("Дрон над Пулково", "Ночью над аэропортом заметили дрон. " * 8)
    same = dedup.fingerprint("Дрон над Пулково.", "Ночью над аэропортом заметили дрон! " * 8)
    other = dedup.fingerprint("Курс доллара вырос", "Биржа закрылась ростом валюты. " * 8)
    assert dedup.distance(first, same) <= dedup.MAX_DISTANCE
    assert dedup.distance(first, other) > dedup.MAX_DISTANCE


def test_перепечатка_находится_в_окне(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    first, _ = store.remember(conn, "meduza", "https://meduza.io/1", "Дрон над Пулково",
                              store.now())
    store.set_fingerprint(conn, first, dedup.fingerprint("Дрон над Пулково", "Текст " * 20))
    second, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/2026/09/24/2",
                               "ДРОН над Пулково!", store.now())
    found = dedup.find(conn, {"id": second, "title": "ДРОН над Пулково!", "simhash": ""})
    assert found == first


def test_разные_новости_не_склеиваются(tmp_path: Path) -> None:
    """Склейка прячет новость, поэтому сомнение решается в пользу отправки."""
    conn = store.connect(tmp_path / "db.sqlite3")
    first, _ = store.remember(conn, "meduza", "https://meduza.io/1", "Дрон над Пулково",
                              store.now())
    store.set_fingerprint(conn, first, dedup.fingerprint("Дрон над Пулково", "Аэропорт " * 20))
    second, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/2026/09/24/2",
                               "Курс доллара вырос", store.now())
    mark = dedup.fingerprint("Курс доллара вырос", "Биржа " * 20)
    assert dedup.find(conn, {"id": second, "title": "Курс доллара вырос", "simhash": mark}) is None


def test_тоже_написали_уходит_только_знающим(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Знает")
    bot_module.add_topic(conn, 7, "дрон")
    bot_module.ensure_user(conn, 8, "Не знает")
    bot_module.add_topic(conn, 8, "дрон")

    first, _ = store.remember(conn, "meduza", "https://meduza.io/1", "Дрон над Пулково",
                              store.now())
    fake = FakeBot()
    asyncio.run(deliver.send_item(fake, conn, first))
    assert {chat for chat, _ in fake.sent} == {7, 8}

    # Восьмой «не получал» оригинал: уберём его доставку и проверим развилку.
    conn.execute("DELETE FROM deliveries WHERE user_id = 8")
    conn.commit()
    second, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/2026/09/24/2",
                               "Дрон над Пулково", store.now())
    store.mark_dup(conn, second, first)
    fake2 = FakeBot()
    also = asyncio.run(deliver.send_also(fake2, conn, second, first))
    plain = asyncio.run(deliver.send_item(fake2, conn, second))

    assert also == 1 and plain == 1
    тексты = {chat: text for chat, text in fake2.sent}
    assert "Тоже написали" in тексты[7], "знающему — короткая досылка"
    assert "Тоже написали" not in тексты[8], "остальным — обычная новость"
