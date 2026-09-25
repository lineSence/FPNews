"""Задержка отдачи и адресат: кому, куда и когда уходит сообщение.

Проверяем поведение: что задержка откладывает, а не отменяет, что досылка
приходит ровно один раз и что канал получает вместо лички [NEWS-004].
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from news import bot as bot_module
from news import deliver, hold, store


class Бот:
    def __init__(self) -> None:
        self.письма: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str, *args, **kwargs) -> bool:
        self.письма.append((int(chat_id), text))
        return True


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    bot_module.add_topic(conn, 7, "мост")
    return conn


def _item(conn, минут_назад: int = 0) -> int:
    from datetime import datetime, timedelta

    когда = (datetime.now().astimezone() - timedelta(minutes=минут_назад)).isoformat(
        timespec="seconds")
    номер, _ = store.remember(conn, "fontanka", "https://f/{}".format(минут_назад),
                              "Мост закрыт на ремонт", когда, published_at=когда)
    store.fill(conn, номер, "", "Мост через Неву закрыт.", когда)
    return номер


def test_задержка_откладывает_а_не_отменяет(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.set_delay(conn, 7, 30)
    свежий = _item(conn, 1)
    бот = Бот()
    assert asyncio.run(deliver.send_item(бот, conn, свежий)) == 0, "рано — не шлём"
    assert not бот.письма
    старый = _item(conn, 45)
    assert asyncio.run(deliver.send_item(бот, conn, старый)) == 1, "срок вышел — шлём"


def test_отложенное_досылается_контуром(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.set_delay(conn, 7, 10)
    номер = _item(conn, 30)
    бот = Бот()
    assert asyncio.run(hold.once(бот, conn)) == 1
    assert len(бот.письма) == 1
    assert asyncio.run(hold.once(бот, conn)) == 0, "второй раз то же самое не уходит"
    assert store.delay_of(conn, 7) == 10 and номер


def test_без_задержки_контур_не_трогает_базу(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert hold.someone_waits(conn) is False
    assert asyncio.run(hold.once(Бот(), conn)) == 0


def test_адресат_канал_получает_вместо_лички(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    store.set_target(conn, 7, "-1001234567890")
    assert store.target_of(conn, 7) == -1001234567890
    бот = Бот()
    asyncio.run(deliver.send_item(бот, conn, _item(conn, 1)))
    assert бот.письма and бот.письма[0][0] == -1001234567890


def test_мусор_в_полях_это_личка_и_ноль(tmp_path: Path) -> None:
    """Непонятный ввод не выдумывает канал и не выдумывает задержку [NEWS-001]."""
    conn = _db(tmp_path)
    assert store.set_target(conn, 7, "мой канал") == ""
    assert store.target_of(conn, 7) == 7
    assert store.set_delay(conn, 7, "потом") == 0
    assert store.set_delay(conn, 7, "-5") == 0
    assert store.set_delay(conn, 7, "9999") == 1440
