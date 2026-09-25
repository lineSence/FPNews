"""Кнопки под новостью: когда модель зовут, когда нет и что видит человек."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from news import bot as bot_module
from news import enrich, model, store


class FakeBot:
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


class Gateway:
    """Поддельный LiteLLM: считает вызовы и отвечает по сценарию."""

    def __init__(self, script: dict[str, tuple[int, str]] | None = None) -> None:
        self.calls: list[str] = []
        self.script = script or {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        name = body["model"]
        self.calls.append(name)
        status, text = self.script.get(name, (200, "— раз\n— два\n— три"))
        if status != 200:
            return httpx.Response(status, json={"error": {"message": text}})
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    def session(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    item, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/2026/09/25/1/",
                             "Дрон над Пулково", store.now())
    store.fill(conn, item, "Лид", "Текст материала про дрон и аэропорт.", store.now())
    return conn, item


def _run(gate: Gateway, conn, item, kind="выжимка", budget=None):
    async def go():
        async with gate.session() as session:
            return await enrich.make(session, conn, item, kind,
                                     budget or model.Budget(max_calls=10))
    return asyncio.run(go())


def test_кнопки_висят_под_сырым_сообщением() -> None:
    keys = enrich.keyboard(42)["inline_keyboard"][0]
    assert [button["text"] for button in keys] == ["Выжимка", "Цитата", "Оценка"]
    for button in keys:
        assert len(button["callback_data"].encode()) <= 64, "телеграм режет на 64 байтах"
    assert enrich.parse("e:s:42") == ("выжимка", 42)
    assert enrich.parse("e:x:42") is None and enrich.parse("мусор") is None


def test_выжимка_приходит_с_пометкой_и_ссылкой(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FPNEWS_LLM_MODELS", "первая, вторая")
    conn, item = _db(tmp_path)
    gate = Gateway()
    text = _run(gate, conn, item)
    assert "Выжимка" in text and "— раз" in text
    assert "https://www.fontanka.ru/2026/09/25/1/" in text, "ссылка обязательна [NEWS-007]"
    assert "первая" in text, "видно, какая модель отвечала"
    assert gate.calls == ["первая"]


def test_второе_нажатие_бесплатно(tmp_path: Path, monkeypatch) -> None:
    """Десять человек под одной новостью — один вызов модели [CORE-016]."""
    monkeypatch.setenv("FPNEWS_LLM_MODELS", "первая")
    conn, item = _db(tmp_path)
    gate = Gateway()
    budget = model.Budget(max_calls=10)
    _run(gate, conn, item, budget=budget)
    again = _run(gate, conn, item, budget=budget)
    assert gate.calls == ["первая"], "второй раз модель не спрашивают"
    assert "из памяти" in again and budget.cached == 1


def test_отказ_выводит_модель_из_каскада(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FPNEWS_LLM_MODELS", "первая, вторая")
    conn, item = _db(tmp_path)
    gate = Gateway({"первая": (429, "кончилась квота")})
    budget = model.Budget(max_calls=10)
    text = _run(gate, conn, item, budget=budget)
    assert "— раз" in text and gate.calls == ["первая", "вторая"]
    assert "первая" in budget.dropped, "на 429 кандидат выбывает до конца суток"
    _run(gate, conn, item, kind="цитата", budget=budget)
    assert gate.calls[-1] == "вторая", "выбывшую больше не спрашивают"


def test_бюджет_суток_держит(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FPNEWS_LLM_MODELS", "первая")
    conn, item = _db(tmp_path)
    gate = Gateway()
    budget = model.Budget(max_calls=0)
    budget.max_calls = 0
    text = _run(gate, conn, item, budget=budget)
    assert "бюджет" in text and gate.calls == []
    assert "https://www.fontanka.ru" in text, "даже при отказе остаётся оригинал"


def test_оценка_подписана_как_мнение_модели(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FPNEWS_LLM_MODELS", "первая")
    conn, item = _db(tmp_path)
    text = _run(Gateway(), conn, item, kind="оценка")
    assert "суждение модели" in text, "пересказ не выдаётся за издание [NEWS-008]"


def test_нажатие_гасит_часики_и_шлёт_ответ(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FPNEWS_LLM_MODELS", "первая")
    conn, item = _db(tmp_path)
    gate, fake = Gateway(), FakeBot()

    async def go():
        async with gate.session() as session:
            query = {"id": "77", "data": "e:s:{}".format(item),
                     "message": {"chat": {"id": 7}}}
            return await bot_module.press(fake, session, conn, model.Budget(max_calls=5), query)

    assert asyncio.run(go()) is True
    assert fake.acks and fake.sent and "Выжимка" in fake.sent[0][1]


def test_чужая_кнопка_не_роняет_бота(tmp_path: Path) -> None:
    conn, _ = _db(tmp_path)
    fake = FakeBot()
    query = {"id": "1", "data": "e:s:нет", "message": {"chat": {"id": 7}}}
    assert asyncio.run(bot_module.press(fake, None, conn, model.Budget(), query)) is False
    assert fake.sent == []
