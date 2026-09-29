"""Подписки на издания целиком: что уходит, что не уходит и почему одно."""

from __future__ import annotations

import asyncio
from pathlib import Path

from news import bot, deliver, store


class FakeBot:
    """Телеграм без телеграма: помнит и сообщения, и ответы на кнопки."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.acks: list[str] = []

    async def send(self, chat_id: int, text: str, preview: bool = True,
                   keyboard: dict | None = None) -> bool:
        self.sent.append((chat_id, text))
        return True

    async def ack(self, callback_id: str, text: str = "") -> bool:
        self.acks.append(text)
        return True


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot.ensure_user(conn, 7, "Владелец")
    return conn


def test_подписка_приносит_новое_из_издания(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert store.follow_feed(conn, 7, "meduza") is True
    item, _ = store.remember(conn, "meduza", "https://meduza.io/news/1", "Заголовок",
                            store.now())
    fake = FakeBot()
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 1
    chat, text = fake.sent[0]
    assert chat == 7
    assert "Заголовок" in text and "https://meduza.io/news/1" in text
    assert "издание целиком" in text
    kind = conn.execute("SELECT kind FROM deliveries WHERE item_id = ?", (item,)).fetchone()
    assert kind["kind"] == "лента"


def test_подписка_не_проигрывает_историю(tmp_path: Path) -> None:
    """Что появилось раньше подписки, уйти не может [NEWS-004]."""
    conn = _db(tmp_path)
    item, _ = store.remember(conn, "meduza", "https://meduza.io/old", "Старое",
                            "2024-01-01T00:00:00")
    store.follow_feed(conn, 7, "meduza")
    fake = FakeBot()
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 0
    новое, _ = store.remember(conn, "meduza", "https://meduza.io/new", "Новое", store.now())
    assert asyncio.run(deliver.send_item(fake, conn, новое)) == 1


def test_подписка_на_всё_ловит_любое_издание(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert store.follow_feed(conn, 7, "все") is True
    item, _ = store.remember(conn, "fontanka", "https://fontanka.ru/x", "Заголовок",
                            store.now())
    fake = FakeBot()
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 1


def test_тема_и_подписка_не_дублируются(tmp_path: Path) -> None:
    """Одна новость — одно сообщение, темой, если она сработала."""
    conn = _db(tmp_path)
    bot.add_topic(conn, 7, "дрон")
    store.follow_feed(conn, 7, "meduza")
    item, _ = store.remember(conn, "meduza", "https://meduza.io/drone",
                            "Дрон над Пулково", store.now())
    fake = FakeBot()
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 1
    assert "тема «дрон»" in fake.sent[0][1]
    kind = conn.execute("SELECT kind FROM deliveries WHERE item_id = ?", (item,)).fetchone()
    assert kind["kind"] == "сырое"


def test_подписка_дважды_не_уходит(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.follow_feed(conn, 7, "meduza")
    item, _ = store.remember(conn, "meduza", "https://meduza.io/once", "Заголовок",
                            store.now())
    fake = FakeBot()
    asyncio.run(deliver.send_item(fake, conn, item))
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 0
    assert len(fake.sent) == 1


def test_отписка_снимает_подписку(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.follow_feed(conn, 7, "meduza")
    assert store.unfollow_feed(conn, 7, "meduza") is True
    assert store.unfollow_feed(conn, 7, "meduza") is False
    item, _ = store.remember(conn, "meduza", "https://meduza.io/silent", "Заголовок",
                            store.now())
    fake = FakeBot()
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 0


def test_вид_лента_отключается_на_странице_отдачи(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.follow_feed(conn, 7, "meduza")
    store.set_kinds(conn, 7, ["сырое"])  # «лента» не отмечена — не приходить
    item, _ = store.remember(conn, "meduza", "https://meduza.io/quiet", "Заголовок",
                            store.now())
    fake = FakeBot()
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 0


def test_команды_бота_про_подписки(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert "Такого издания нет" in bot.answer(conn, 7, "Имя", "/подписаться неттакого")
    assert "оформлена" in bot.answer(conn, 7, "Имя", "/подписаться meduza")
    assert "уже" in bot.answer(conn, 7, "Имя", "/подписаться meduza")
    assert "Медуза" in bot.answer(conn, 7, "Имя", "/подписки")
    assert "Подписка снята" in bot.answer(conn, 7, "Имя", "/отписаться meduza")
    assert "Такой подписки нет" in bot.answer(conn, 7, "Имя", "/отписаться fontanka")
    assert "Подписок" in bot.answer(conn, 7, "Имя", "/подписки")


def test_лента_показывает_последние_материалы(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert "Возможны" in bot.answer(conn, 7, "Имя", "/лента неттакого")
    assert "пока нет" in bot.answer(conn, 7, "Имя", "/лента meduza")
    store.remember(conn, "meduza", "https://meduza.io/a", "Заголовок A", store.now())
    store.remember(conn, "meduza", "https://meduza.io/b", "Заголовок B", store.now())
    текст = bot.answer(conn, 7, "Имя", "/лента meduza 1")
    assert "Заголовок B" in текст and "Заголовок A" not in текст


def test_кнопка_отзыва_учитывается_и_чужой_не_портит(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    bot.add_topic(conn, 7, "дрон")
    item, _ = store.remember(conn, "meduza", "https://meduza.io/d", "Дрон над заливом",
                            store.now())
    fake = FakeBot()
    query = {"id": "42", "message": {"chat": {"id": 7}}, "data": "f:1:{}:1".format(item)}
    assert asyncio.run(bot.press(fake, None, conn, None, query)) is True
    assert fake.acks == ["Учтено"]
    assert "Дрон над заливом" in store.feedback_texts(conn, 1)[0]
    # чужой отзыв о чужой теме не записывается
    bot.ensure_user(conn, 8, "Гость")
    чужой = {"id": "43", "message": {"chat": {"id": 8}}, "data": "f:1:{}:0".format(item)}
    assert asyncio.run(bot.press(fake, None, conn, None, чужой)) is False
    assert "не записан" in fake.acks[-1]