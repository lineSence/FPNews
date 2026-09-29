"""Меню бота: управление кнопками — без команд и без сети.

Проверяем поведение, а не разметку: нажатия меняют состояние в базе,
читателю не видны владельческие экраны, необратимые действия требуют
подтверждения, а диалоги закрываются командой, кнопкой отмены и сами —
по возрасту.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from news import access, bridge, menu, store
from news import bot as bot_module
from news import telegram


@pytest.fixture(autouse=True)
def _чистый_процесс():
    """Меню и мост держат состояние в памяти процесса (диалоги, очередь
    проверок связи). Без уборки оно протекает в чужие тесты: «проверка связи»
    из меню оставляла 7 в bridge.ТЕСТЫ, и test_news_security падал."""
    yield
    menu._диалоги.clear()
    bridge.забыть()


class FakeBot:
    """Телеграм без телеграма: запоминает отправленное и перерисованное."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str, dict | None]] = []
        self.edited: list[tuple[int, int, str, dict | None]] = []
        self.acked: list[str] = []

    async def send(self, chat_id: int, text: str, preview: bool = True,
                   keyboard: dict | None = None) -> bool:
        self.sent.append((chat_id, text, keyboard))
        return True

    async def edit(self, chat_id: int, message_id: int, text: str,
                   preview: bool = False, keyboard: dict | None = None) -> bool:
        self.edited.append((chat_id, message_id, text, keyboard))
        return True

    async def ack(self, callback_id: str, text: str = "") -> bool:
        self.acked.append(callback_id)
        return True


def _база(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    return conn


def _жмём(conn, bot: FakeBot, user_id: int, data: str) -> str:
    """Одно нажатие кнопки меню. Возвращает текст перерисованного экрана."""
    asyncio.run(menu.press(bot, conn, user_id, user_id, 1, data))
    return bot.edited[-1][2]


def _кнопки(bot: FakeBot) -> list[str]:
    return [кнопка["callback_data"]
            for _, _, _, клавиатура in bot.edited
            for ряд in (клавиатура or {}).get("inline_keyboard", [])
            for кнопка in ряд]


# --- Главное меню ---

def test_меню_открывается_командой(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    экран = menu.сообщение(conn, 7, "/меню")
    assert экран is not None
    текст, ряды = экран
    assert "Главное меню" in текст
    assert any("Мои издания" in надпись for ряд in ряды for надпись, _ in ряд)


def test_читателю_не_видны_кнопки_владельца(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    _, ряды = menu.сообщение(conn, 7, "/меню")
    данные = {нажатие for ряд in ряды for _, нажатие in ряд}
    assert "m:kat" not in данные and "m:dost" not in данные
    access.назначить(conn, 7, "владелец")
    _, ряды = menu.сообщение(conn, 7, "/меню")
    данные = {нажатие for ряд in ряды for _, нажатие in ряд}
    assert "m:kat" in данные and "m:dost" in данные and "m:sost" in данные


def test_незнакомому_меню_не_открывается(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    assert menu.сообщение(conn, 45, "/меню") is None


# --- Мои издания ---

def test_три_состояния_издания_кнопками(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:izd")
    _жмём(conn, bot, 7, "m:izd:meduza")
    assert "Как поступить с этим изданием" in bot.edited[-1][2]
    _жмём(conn, bot, 7, "m:mode:meduza:всё")
    assert store.source_mode(conn, 7, "meduza") == "всё"
    _жмём(conn, bot, 7, "m:mode:meduza:тишина")
    assert store.source_mode(conn, 7, "meduza") == "тишина"
    assert "meduza" not in store.user_sources(conn, 7)
    _жмём(conn, bot, 7, "m:mode:meduza:темы")
    assert store.source_mode(conn, 7, "meduza") == "темы"
    assert "meduza" in store.user_sources(conn, 7)


def test_все_издания_сразу_и_обратно(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:vse:1")
    assert any(row["code"] == "" for row in store.feed_subs_of(conn, 7))
    _жмём(conn, bot, 7, "m:vse:0")
    assert not store.feed_subs_of(conn, 7)


# --- Мои темы ---

def test_новая_тема_диалогом(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:temy")
    _жмём(conn, bot, 7, "m:tnovy")
    assert "слова новой темы" in bot.edited[-1][2]
    ответ = menu.сообщение(conn, 7, "дрон, бпла")
    assert ответ is not None
    темы = store.topics_of(conn, 7)
    assert len(темы) == 1 and темы[0]["title"] == "дрон"
    assert "Тема «дрон»" in ответ[0]


def test_слова_и_стоп_слова_диалогом(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    bot_module.add_topic(conn, 7, "движ")
    тема = store.topics_of(conn, 7)[0]["id"]
    _жмём(conn, bot, 7, "m:tslova:{}".format(тема))
    menu.сообщение(conn, 7, "марш")
    assert "марш" in store.topics_of(conn, 7)[0]["words"]
    _жмём(conn, bot, 7, "m:tstop:{}".format(тема))
    menu.сообщение(conn, 7, "музыка")
    assert "музыка" in store.topics_of(conn, 7)[0]["stopwords"]


def test_порог_и_выключение_кнопками(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    bot_module.add_topic(conn, 7, "дрон")
    тема = store.topics_of(conn, 7)[0]["id"]
    _жмём(conn, bot, 7, "m:tporog:{}:2".format(тема))
    assert float(store.topics_of(conn, 7)[0]["threshold"]) == 2
    _жмём(conn, bot, 7, "m:tvkl:{}".format(тема))
    assert not store.topics_of(conn, 7)[0]["enabled"]
    _жмём(conn, bot, 7, "m:tvkl:{}".format(тема))
    assert store.topics_of(conn, 7)[0]["enabled"]


def test_удаление_темы_спрашивает_подтверждение(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    bot_module.add_topic(conn, 7, "дрон")
    тема = store.topics_of(conn, 7)[0]["id"]
    _жмём(conn, bot, 7, "m:tudal:{}".format(тема))
    assert "Восстановить нельзя" in bot.edited[-1][2]
    assert store.topics_of(conn, 7), "первое нажатие ничего не удаляет"
    _жмём(conn, bot, 7, "m:tudal:{}:1".format(тема))
    assert not store.topics_of(conn, 7)


# --- Сводка и отдача ---

def test_час_сводки_кнопкой(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:svodcas:09:00")
    строка = conn.execute("SELECT digest_at FROM users WHERE id = 7").fetchone()
    assert строка["digest_at"] == "09:00"
    _жмём(conn, bot, 7, "m:svodoff")
    строка = conn.execute("SELECT digest_at FROM users WHERE id = 7").fetchone()
    assert строка["digest_at"] == ""


def test_сводка_сейчас_отдельным_сообщением(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:svodnow")
    assert bot.sent, "сводка уходит сообщением, а не кнопкой"
    assert "Сводка — сообщением выше" in bot.edited[-1][2]


def test_виды_тишина_и_задержка_кнопками(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:vid:сырое")
    assert "сырое" not in store.kinds_of(conn, 7)
    _жмём(conn, bot, 7, "m:vid:сырое")
    assert "сырое" in store.kinds_of(conn, 7)
    _жмём(conn, bot, 7, "m:tiho:23")
    строка = conn.execute(
        "SELECT quiet_from, quiet_to FROM users WHERE id = 7").fetchone()
    assert (строка["quiet_from"], строка["quiet_to"]) == ("23:00", "08:00")
    _жмём(conn, bot, 7, "m:tiho:0")
    строка = conn.execute(
        "SELECT quiet_from, quiet_to FROM users WHERE id = 7").fetchone()
    assert (строка["quiet_from"], строка["quiet_to"]) == ("", "")
    _жмём(conn, bot, 7, "m:zader:10")
    assert store.delay_of(conn, 7) == 10


# --- Запросы ---

def test_запрос_диалогом_и_кнопками(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:znew")
    ответ = menu.сообщение(conn, 7, "тариф")
    assert ответ is not None and "тариф" in ответ[0]
    запрос = store.queries(conn, 7)
    assert len(запрос) == 1 and запрос[0]["notify"]
    _жмём(conn, bot, 7, "m:znot:{}".format(запрос[0]["id"]))
    assert not store.queries(conn, 7)[0]["notify"]
    _жмём(conn, bot, 7, "m:zdel:{}".format(запрос[0]["id"]))
    assert not store.queries(conn, 7)


def test_диалог_источника_открывается_и_закрывается(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:nayti")
    assert "Пришлите адрес" in bot.edited[-1][2]
    _жмём(conn, bot, 7, "m:otmena")
    assert "Отменено" in bot.edited[-1][2]
    # После отмены обычный текст — не диалог, и уходит прежним путём.
    assert menu.сообщение(conn, 7, "example.com") is None


# --- Владелец: каталог ---

def test_читателю_отказывают_владельческие_кнопки(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:kat")
    assert "только владельцу" in bot.edited[-1][2]
    _жмём(conn, bot, 7, "m:dvyd:читатель")
    assert not access.приглашения(conn), "приглашение не выдаётся по нажатию"


def test_остановка_сбора_без_подписчиков_напрямую(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    access.назначить(conn, 7, "владелец")
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:kreg:meduza")
    assert not store.source_enabled(conn, "meduza")
    assert "Сбор остановлен" in bot.edited[-1][2]
    _жмём(conn, bot, 7, "m:kreg:meduza")
    assert store.source_enabled(conn, "meduza")


def test_остановка_сбора_с_подписчиками_требует_подтверждения(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    access.назначить(conn, 7, "владелец")
    bot_module.ensure_user(conn, 8, "Читатель")
    store.follow_feed(conn, 8, "meduza")
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:kreg:meduza")
    assert "1 чел." in bot.edited[-1][2]
    assert store.source_enabled(conn, "meduza"), "первое нажатие не выключает"
    _жмём(conn, bot, 7, "m:kreg:meduza:1")
    assert not store.source_enabled(conn, "meduza")


def test_интервал_кнопкой(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    access.назначить(conn, 7, "владелец")
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:ksek:meduza:600")
    assert store.source_every(conn, "meduza") == 600
    assert "10 мин" in _жмём(conn, bot, 7, "m:kint:meduza")


def test_удаление_издания_требует_подтверждения_с_подписчиками(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    access.назначить(conn, 7, "владелец")
    код = store.add_feed(conn, label="Тест", door="https://x.example/feed", kind="rss")
    bot_module.ensure_user(conn, 8, "Читатель")
    store.follow_feed(conn, 8, код)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:kdel:{}".format(код))
    assert "1 чел." in bot.edited[-1][2]
    assert код in __import__("news.sources", fromlist=["x"]).registry(conn)
    _жмём(conn, bot, 7, "m:kdel:{}:1".format(код))
    assert код not in __import__("news.sources", fromlist=["x"]).registry(conn)


# --- Владелец: доступы, состояние, хранение ---

def test_приглашение_диалогом_выдаёт_ключ(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    access.назначить(conn, 7, "владелец")
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:dvyd:владелец")
    ответ = menu.сообщение(conn, 7, "Петя")
    assert ответ is not None
    assert "/ключ" in ответ[0]
    список = access.приглашения(conn)
    assert список[0]["кому"] == "Петя" and список[0]["роль"] == "владелец"
    _жмём(conn, bot, 7, "m:dotz:{}".format(список[0]["номер"]))
    assert access.приглашения(conn)[0]["состояние"] == "отозвано"


def test_состояние_и_действия_владельцу(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    access.назначить(conn, 7, "владелец")
    bridge.забыть()
    bot = FakeBot()
    текст = _жмём(conn, bot, 7, "m:sost")
    assert "Материалов в базе" in текст and "Правок за сутки" in текст
    assert "m:opros" in _кнопки(bot), "кнопки опроса и проверки — на экране"
    assert "Разбуждено сторожей: 0" in _жмём(conn, bot, 7, "m:opros")
    assert "Проверка попросила" in _жмём(conn, bot, 7, "m:proverka")


def test_хранение_кнопками(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    access.назначить(conn, 7, "владелец")
    bot = FakeBot()
    текст = _жмём(conn, bot, 7, "m:khr")
    assert "База:" in текст
    assert "Выброшено копий" in _жмём(conn, bot, 7, "m:kopii:30")
    assert "освободило" in _жмём(conn, bot, 7, "m:szhat")


# --- Диалоги: гигиена ---

def test_команда_отменяет_диалог(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = FakeBot()
    _жмём(conn, bot, 7, "m:tnovy")
    assert menu.сообщение(conn, 7, "/темы") is None
    assert menu.сообщение(conn, 7, "дрон") is None, "после команды диалог закрыт"


def test_диалог_устаревает_по_возрасту(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    menu._диалоги[7] = ("тема", "", time.monotonic() - 1)
    ответ = menu.сообщение(conn, 7, "дрон")
    assert ответ is not None and "устарел" in ответ[0]
    assert not store.topics_of(conn, 7)


def test_нажатие_меню_через_бота_доходит_до_меню(tmp_path: Path) -> None:
    """bot.press разводит кнопки меню по своему пути: модель не трогается."""
    conn = _база(tmp_path)
    bot = FakeBot()
    запрос = {"id": "1", "message": {"chat": {"id": 7}, "message_id": 5},
              "data": "m:izd"}
    assert asyncio.run(bot_module.press(bot, None, conn, None, запрос))
    assert bot.edited and "издания" in bot.edited[-1][2].lower()
    assert bot.acked, "часики погашены сразу"


def test_клавиатура_собирается_из_рядов() -> None:
    клавиатура = telegram.inline([[("Первая", "m:1"), ("Вторая", "m:2")],
                                  [("Третья", "m:3")]])
    assert клавиатура == {"inline_keyboard": [
        [{"text": "Первая", "callback_data": "m:1"},
         {"text": "Вторая", "callback_data": "m:2"}],
        [{"text": "Третья", "callback_data": "m:3"}],
    ]}


# --- Путь через serve: то, что реально уходит в телеграм ---

class _Стоп:
    def is_set(self) -> bool:
        return False


class _Опрос(FakeBot):
    """Бот с одной порцией обновлений — как длинный опрос телеграма."""

    def __init__(self, updates: list[dict]) -> None:
        super().__init__()
        self._updates = updates

    async def updates(self) -> list[dict]:
        порция, self._updates = self._updates, []
        return порция


def test_меню_через_serve_шлёт_объект_клавиатуры(tmp_path: Path) -> None:
    """Регрессия шага 19: serve отдавал в reply_markup голый список рядов,
    телеграм отвергал sendMessage, и /меню молчало."""
    conn = _база(tmp_path)
    bot = _Опрос([{"message": {"chat": {"id": 7}, "from": {"first_name": "В"},
                               "text": "/меню"}}])
    assert asyncio.run(bot_module.serve(bot, conn, _Стоп(), rounds=1)) == 1
    _, текст, клавиатура = bot.sent[-1]
    assert "Главное меню" in текст
    assert isinstance(клавиатура, dict)
    ряды = клавиатура["inline_keyboard"]
    assert ряды and all(isinstance(кнопка, dict) and кнопка["callback_data"]
                        for ряд in ряды for кнопка in ряд)


def test_обычная_команда_через_serve_без_клавиатуры(tmp_path: Path) -> None:
    conn = _база(tmp_path)
    bot = _Опрос([{"message": {"chat": {"id": 7}, "from": {"first_name": "В"},
                               "text": "/темы"}}])
    asyncio.run(bot_module.serve(bot, conn, _Стоп(), rounds=1))
    assert bot.sent[-1][2] is None


def test_системное_меню_принимается_телеграмом(tmp_path: Path) -> None:
    """setMyCommands отвергает весь список из-за одной нелатинской команды."""
    conn = _база(tmp_path)
    for команда, описание in telegram.КОМАНДЫ:
        assert telegram.КОМАНДА_RE.match(команда), команда
        assert 1 <= len(описание) <= 256
        # Каждую команду из системного меню бот понимает.
        ответ = menu.сообщение(conn, 7, "/" + команда) or (
            bot_module.answer(conn, 7, "В", "/" + команда), None)
        assert not ответ[0].startswith("Не понимаю"), команда
