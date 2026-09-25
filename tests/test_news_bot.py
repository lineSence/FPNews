"""Бот и рассылка: кому уходит, что написано, и почему дважды не уйдёт."""

from __future__ import annotations

import asyncio
from pathlib import Path

from news import bot, deliver, store, topics


class FakeBot:
    """Телеграм без телеграма: запоминает, что отправили."""

    def __init__(self, broken: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.broken = broken

    async def send(self, chat_id: int, text: str, preview: bool = True,
                   keyboard: dict | None = None) -> bool:
        if self.broken:
            return False
        self.sent.append((chat_id, text))
        return True


def _setup(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot.ensure_user(conn, 7, "Владелец")
    bot.add_topic(conn, 7, "дрон, бпла")
    item, _ = store.remember(
        conn, "meduza", "https://meduza.io/news/1", "Дрон над Пулково", store.now()
    )
    store.fill(conn, item, "Короткий лид", "Текст про дроны", store.now())
    return conn, item


def test_новость_уходит_подписчику(tmp_path: Path) -> None:
    conn, item = _setup(tmp_path)
    fake = FakeBot()
    sent = asyncio.run(deliver.send_item(fake, conn, item))
    assert sent == 1
    chat, text = fake.sent[0]
    assert chat == 7
    assert "Дрон над Пулково" in text
    assert "https://meduza.io/news/1" in text, "ссылка на оригинал обязательна [NEWS-007]"
    assert "тема «дрон»" in text and "в заголовке" in text
    row = conn.execute("SELECT sent_at FROM items WHERE id = ?", (item,)).fetchone()
    assert row["sent_at"], "метка отправки — конец нашей задержки [NEWS-001]"


def test_дважды_не_уходит(tmp_path: Path) -> None:
    conn, item = _setup(tmp_path)
    fake = FakeBot()
    asyncio.run(deliver.send_item(fake, conn, item))
    again = asyncio.run(deliver.send_item(fake, conn, item))
    assert again == 0 and len(fake.sent) == 1


def test_холодный_старт_не_рассылается(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    bot.ensure_user(conn, 7, "Владелец")
    bot.add_topic(conn, 7, "дрон")
    item, _ = store.remember(conn, "meduza", "https://meduza.io/old", "Дрон вчерашний",
                             store.now(), cold=True)
    assert asyncio.run(deliver.send_item(FakeBot(), conn, item)) == 0


def test_недоставленное_не_помечается_отправленным(tmp_path: Path) -> None:
    """Телеграм молчит — значит человек не получил, и метки быть не должно."""
    conn, item = _setup(tmp_path)
    assert asyncio.run(deliver.send_item(FakeBot(broken=True), conn, item)) == 0
    row = conn.execute("SELECT sent_at FROM items WHERE id = ?", (item,)).fetchone()
    assert row["sent_at"] is None


def test_чужая_тема_не_видна(tmp_path: Path) -> None:
    conn, _ = _setup(tmp_path)
    bot.ensure_user(conn, 9, "Другой")
    assert bot.list_topics(conn, 9).startswith("Тем пока нет")
    assert bot.drop_topic(conn, 9, "1") == "Такой темы у вас нет"
    assert "дрон" in bot.list_topics(conn, 7)


def test_команды_бота(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    assert "приношу новости" in bot.answer(conn, 7, "Имя", "/старт")
    assert "добавлена" in bot.answer(conn, 7, "Имя", "/добавить метро, транспорт")
    assert "метро" in bot.answer(conn, 7, "Имя", "/темы")
    assert "Нужны слова" in bot.answer(conn, 7, "Имя", "/добавить")
    assert "нечего мерить" in bot.answer(conn, 7, "Имя", "/задержка")
    assert "Удалено" in bot.answer(conn, 7, "Имя", "/удалить 1")
    assert "Тем пока нет" in bot.answer(conn, 7, "Имя", "/темы")


def test_сообщение_экранируется(tmp_path: Path) -> None:
    """Чужой заголовок не должен ломать разметку."""
    item = {"source": "meduza", "title": "<b>Дрон</b> & Ко", "url": "https://meduza.io/x"}
    hit = topics.Hit(topic_id=1, user_id=7, title="дрон", words=("дрон",), in_title=True)
    text = deliver.message(item, hit)
    assert "&lt;b&gt;Дрон&lt;/b&gt; &amp; Ко" in text


def test_токен_не_попадает_в_лог(monkeypatch) -> None:
    """Токен — часть адреса Telegram, а журнал читают и пересылают [CORE-012]."""
    import logging

    from news import telegram

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:AAsecretsecretsecretsecret")
    telegram.hush()
    assert logging.getLogger("httpx").level == logging.WARNING
    ошибка = "error at https://api.telegram.org/bot123456:AAsecretsecretsecretsecret/getUpdates"
    assert "AAsecret" not in telegram.safe(ошибка)
    # Чужой токен в тексте тоже маскируется.
    assert "bot…" in telegram.safe("bot999999:BBotherotherotherother/sendMessage")
