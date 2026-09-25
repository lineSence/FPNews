"""Смысловые дубли и досылки: когда склеиваем, когда молчим, кому шлём."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from news import bot as bot_module
from news import deliver, embed, model, recheck, store, story


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str, preview: bool = True,
                   keyboard: dict | None = None) -> bool:
        self.sent.append((chat_id, text))
        return True


class Gateway:
    """Поддельный шлюз векторов: отдаёт заранее заданный вектор на текст."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        self.calls += 1
        for key, values in self.table.items():
            if key in body["input"]:
                return httpx.Response(200, json={"data": [{"embedding": values}]})
        return httpx.Response(200, json={"data": [{"embedding": [0.0, 0.0, 1.0]}]})

    def session(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _db(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    bot_module.add_topic(conn, 7, "суд, сизо, чиновник")
    return conn


def test_косинус_считается_по_упакованным(tmp_path: Path) -> None:
    first = embed.pack([3.0, 0.0])
    same = embed.pack([10.0, 0.0])
    other = embed.pack([0.0, 1.0])
    assert round(embed.similarity(first, same), 6) == 1.0, "длина не важна, важно направление"
    assert round(embed.similarity(first, other), 6) == 0.0
    assert embed.similarity(first, embed.pack([1.0, 0.0, 0.0])) == 0.0, "разная длина — не сравнение"


def test_чужая_модель_не_считается_вектором(tmp_path: Path, monkeypatch) -> None:
    """Векторы разных моделей несравнимы [LLM-011]."""
    conn = _db(tmp_path)
    item, _ = store.remember(conn, "meduza", "https://meduza.io/1", "Суд", store.now())
    store.save_vector(conn, item, "старая-модель", embed.pack([1.0, 0.0]))
    monkeypatch.setenv("FPNEWS_EMBED_MODEL", "новая-модель")
    assert store.vector_of(conn, item, embed.name()) == b""


def test_разные_слова_одно_событие_склеиваются(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FPNEWS_EMBED_MODEL", "проверочная")
    conn = _db(tmp_path)
    first, _ = store.remember(conn, "meduza", "https://meduza.io/1",
                              "Суд арестовал главу комитета", store.now())
    store.fill(conn, first, "", "Суд отправил чиновника под стражу.", store.now())
    store.stamp(conn, first, "sent_at", store.now())
    store.save_vector(conn, first, "проверочная", embed.pack([1.0, 0.1, 0.0]))
    second, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/2/",
                               "Чиновника отправили в СИЗО", store.now())
    store.fill(conn, second, "", "Главу комитета взяли под стражу.", store.now())
    gate = Gateway({"Чиновника": [0.99, 0.14, 0.0]})
    item = dict(conn.execute("SELECT * FROM items WHERE id = ?", (second,)).fetchone())

    async def go():
        async with gate.session() as session:
            return await story.link(session, conn, item, model.Budget())

    match = asyncio.run(go())
    assert match is not None and match.item_id == first
    assert match.score >= story.threshold()
    assert store.vector_of(conn, second, "проверочная"), "вектор остался в базе"


def test_похожее_но_не_то_же_не_склеивается(tmp_path: Path, monkeypatch) -> None:
    """Пропуск дороже лишнего сообщения [NEWS-004]."""
    monkeypatch.setenv("FPNEWS_EMBED_MODEL", "проверочная")
    conn = _db(tmp_path)
    first, _ = store.remember(conn, "meduza", "https://meduza.io/1", "Метро закрыли",
                              store.now())
    store.stamp(conn, first, "sent_at", store.now())
    store.save_vector(conn, first, "проверочная", embed.pack([1.0, 0.0]))
    second, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/2/",
                               "Метро открыли", store.now())
    gate = Gateway({"Метро открыли": [0.7, 0.7]})
    item = dict(conn.execute("SELECT * FROM items WHERE id = ?", (second,)).fetchone())

    async def go():
        async with gate.session() as session:
            return await story.link(session, conn, item, model.Budget())

    assert asyncio.run(go()) is None


def test_без_шлюза_склейка_молчит(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    item, _ = store.remember(conn, "meduza", "https://meduza.io/1", "Суд", store.now())
    row = dict(conn.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone())
    assert asyncio.run(story.link(None, conn, row, model.Budget())) is None


def test_потолок_векторов_держит(tmp_path: Path) -> None:
    budget = model.Budget()
    budget.max_embeds = 1
    assert budget.take_embed() is True and budget.take_embed() is False


def test_расписание_перечитывания(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    item, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/1/", "Пожар",
                             store.now())
    store.stamp(conn, item, "sent_at", store.now())
    assert recheck.due(conn) == [], "только что найденное не перечитывают"
    conn.execute("UPDATE items SET listed_at = datetime('now', '-20 minutes') WHERE id = ?",
                 (item,))
    conn.commit()
    assert [row["id"] for row in recheck.due(conn)] == [item]
    conn.execute("UPDATE items SET checks = ? WHERE id = ?", (len(recheck.STEPS), item))
    conn.commit()
    assert recheck.due(conn) == [], "после последнего захода материал не трогают"


def test_что_считается_изменением() -> None:
    old = {"title": "Задержан глава комитета", "body": "а" * 1000}
    assert recheck.changed(old, "Арестован глава комитета", "а" * 1000) == "заголовок"
    assert recheck.changed(old, "Задержан глава комитета", "а" * 1000 + "б" * 450) == "дополнен"
    assert recheck.changed(old, "Задержан глава комитета", "а" * 1000 + "б" * 50) == "", \
        "строчка «читайте также» — не повод будить человека"


def test_досылка_уходит_только_получившим(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    item, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/1/",
                             "Суд и чиновник", store.now())
    store.fill(conn, item, "", "Суд отправил чиновника в СИЗО", store.now())
    fake = FakeBot()
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 1
    text = recheck.message({"title": "Суд и чиновник", "url": "https://www.fontanka.ru/1/"},
                           "заголовок", "Чиновник арестован")
    assert asyncio.run(deliver.send_change(fake, conn, item, text)) == 1
    assert "Изменение в новости" in fake.sent[-1][1]
    assert "https://www.fontanka.ru/1/" in fake.sent[-1][1]
    # Второй раз то же изменение не уходит.
    assert asyncio.run(deliver.send_change(fake, conn, item, text)) == 0
    # Не получившему первую версию досылка не нужна.
    bot_module.ensure_user(conn, 9, "Другой")
    bot_module.add_topic(conn, 9, "суд")
    assert asyncio.run(deliver.send_change(fake, conn, item, text)) == 0


PAGE = (
    '<html><head><meta property="og:title" content="{title}">'
    '<meta property="og:description" content="Коротко о событии"></head>'
    "<body>{paras}</body></html>"
)


def test_перечитывание_замечает_правку_и_досылает(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    item, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/1/",
                             "Задержан чиновник", store.now())
    store.fill(conn, item, "", "Суд разбирается. " * 20, store.now())
    fake = FakeBot()
    assert asyncio.run(deliver.send_item(fake, conn, item)) == 1
    conn.execute("UPDATE items SET listed_at = datetime('now', '-20 minutes') WHERE id = ?",
                 (item,))
    conn.commit()
    page = PAGE.format(title="Чиновник арестован судом",
                       paras="<p>{}</p>".format("Суд отправил его в СИЗО. " * 20))

    async def go():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page))
        ) as session:
            return await recheck.once(fake, session, conn)

    assert asyncio.run(go()) == 1
    assert "Заголовок изменился" in fake.sent[-1][1]
    row = conn.execute("SELECT title, checks FROM items WHERE id = ?", (item,)).fetchone()
    assert row["title"] == "Чиновник арестован судом" and row["checks"] == 1
    assert store.last_revision(conn, item)["title"] == "Чиновник арестован судом"
    # Второй заход по расписанию ещё не наступил.
    assert recheck.due(conn) == []
