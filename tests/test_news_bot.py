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
    bot.ensure_user(conn, 7, "Имя")
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


def test_бот_закрыт_для_незнакомого(tmp_path: Path, monkeypatch) -> None:
    """Раньше учётку получал любой, кто написал «/старт» [CORE-016]."""
    from news import access

    monkeypatch.delenv("FPNEWS_OWNER", raising=False)
    monkeypatch.setenv("FPNEWS_SECRET", "секрет")
    access.забыть_попытки()
    conn = store.connect(tmp_path / "db.sqlite3")
    ответ = bot.answer(conn, 42, "Чужой", "/старт")
    assert "закрытая система" in ответ and "42" in ответ
    assert "приношу новости" not in ответ
    assert conn.execute("SELECT count(*) c FROM users").fetchone()["c"] == 0
    # И остальные команды тоже закрыты, а не «просто без тем».
    assert "закрытая система" in bot.answer(conn, 42, "Чужой", "/добавить дрон")
    assert conn.execute("SELECT count(*) c FROM topics").fetchone()["c"] == 0


def test_ключ_открывает_доступ_один_раз(tmp_path: Path, monkeypatch) -> None:
    from news import access

    monkeypatch.setenv("FPNEWS_SECRET", "секрет")
    access.забыть_попытки()
    conn = store.connect(tmp_path / "db.sqlite3")
    ключ, _ = access.выдать(conn, "Петя")
    assert "Ключ принят" in bot.answer(conn, 43, "Петя", "/ключ " + ключ)
    assert "приношу новости" in bot.answer(conn, 43, "Петя", "/старт")
    # Тем же ключом второй человек не войдёт.
    assert "закрытая система" in bot.answer(conn, 44, "Вася", "/ключ " + ключ)
    assert not access.известен(conn, 44)


def test_неверный_ключ_отвечает_одинаково_и_кончается(tmp_path: Path, monkeypatch) -> None:
    from news import access

    monkeypatch.setenv("FPNEWS_SECRET", "секрет")
    access.забыть_попытки()
    conn = store.connect(tmp_path / "db.sqlite3")
    чужой = bot.answer(conn, 45, "Чужой", "/ключ " + access.новый_ключ())
    мусор = bot.answer(conn, 45, "Чужой", "/ключ мусор")
    assert чужой == мусор == bot.ЗАКРЫТО.format(45)
    for _ in range(access.ПОПЫТОК):
        bot.answer(conn, 45, "Чужой", "/ключ " + access.новый_ключ())
    # Попытки кончились — ответ тот же, настоящий ключ уже не поможет.
    ключ, _ = access.выдать(conn, "Петя")
    assert bot.answer(conn, 45, "Чужой", "/ключ " + ключ) == bot.ЗАКРЫТО.format(45)
    assert not access.известен(conn, 45)


def test_приглашения_выдаёт_только_владелец(tmp_path: Path, monkeypatch) -> None:
    from news import access

    monkeypatch.setenv("FPNEWS_SECRET", "секрет")
    monkeypatch.setenv("FPNEWS_OWNER", "70")
    access.забыть_попытки()
    conn = store.connect(tmp_path / "db.sqlite3")
    хозяин = bot.answer(conn, 70, "Хозяин", "/пригласить Петя")
    assert "Приглашение №1" in хозяин
    ключ = хозяин.split("/ключ ")[1].split("<")[0].strip()
    assert access.похож_на_ключ(ключ)
    assert "Ключ принят" in bot.answer(conn, 71, "Петя", "/ключ " + ключ)
    assert "только владелец" in bot.answer(conn, 71, "Петя", "/пригласить Вася")
    assert "только владельцу" in bot.answer(conn, 71, "Петя", "/доступы")
    assert "Петя" in bot.answer(conn, 70, "Хозяин", "/доступы")
