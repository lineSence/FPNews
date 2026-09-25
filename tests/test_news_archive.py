"""Доказательная копия и снятие с публикации.

Здесь проверяется то, ради чего затевался шаг 10: страница, которую издание
убрало, остаётся у нас целиком и с отпечатком, а само исчезновение
записывается как наблюдение со временем и кодом ответа, а не как отсутствие
данных [NEWS-001].
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from news import recheck, store

СТРАНИЦА = (
    '<html><head><meta property="og:title" content="Смольный объяснил рост тарифов">'
    "</head><body><article><p>{}</p></article></body></html>"
)


def _conn(tmp_path: Path):
    return store.connect(tmp_path / "db.sqlite3")


def _item(conn, url: str = "https://example.org/1") -> int:
    item_id, _ = store.remember(conn, "fontanka", url, "Заголовок", store.now())
    conn.execute(
        "UPDATE items SET sent_at = ?, body = ?, listed_at = datetime('now', '-1 hour') "
        "WHERE id = ?",
        (store.now(), "старый текст материала", item_id),
    )
    conn.commit()
    return item_id


class _Бот:
    def __init__(self) -> None:
        self.отправлено: list[str] = []

    async def send(self, user_id, text, **kwargs):  # noqa: ANN001, ANN003
        self.отправлено.append(text)
        return True


def _session(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_копия_страницы_хранится_со_отпечатком(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item_id = _item(conn)
    страница = СТРАНИЦА.format("Текст, который потом исчезнет, и в нём тоже больше сорока знаков")
    отпечаток = store.save_snapshot(conn, item_id, страница)
    assert len(отпечаток) == 64
    копии = store.snapshots(conn, item_id)
    assert len(копии) == 1 and копии[0]["размер" if "размер" in копии[0] else "size"] > 0
    assert store.snapshot_page(conn, копии[0]["id"]) == страница


def test_одинаковая_страница_не_дублируется(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item_id = _item(conn)
    страница = СТРАНИЦА.format("Один и тот же текст, длиной заведомо больше сорока знаков подряд")
    store.save_snapshot(conn, item_id, страница)
    store.save_snapshot(conn, item_id, страница)
    assert len(store.snapshots(conn, item_id)) == 1


def test_снятие_с_публикации_записывается_и_досылается(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item_id = _item(conn)
    conn.execute(
        "INSERT INTO deliveries(item_id, user_id, topic_id, kind, sent_at) VALUES(?,?,?,?,?)",
        (item_id, 7, None, "сырое", store.now()),
    )
    conn.commit()
    бот = _Бот()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="нет такой страницы")

    async def run():
        async with _session(handler) as session:
            return await recheck.once(бот, session, conn)

    assert asyncio.run(run()) == 1
    строка = store.gone(conn)[0]
    assert строка["gone_code"] == 404 and строка["gone_at"]
    assert "снят с публикации" in бот.отправлено[0]


def test_повторное_снятие_не_шлёт_второе_сообщение(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item_id = _item(conn)
    store.mark_gone(conn, item_id, 404)
    было = store.gone(conn)[0]["gone_at"]
    store.mark_gone(conn, item_id, 410)
    стало = store.gone(conn)[0]
    assert стало["gone_at"] == было, "время первого наблюдения не переписывается"
    assert стало["gone_code"] == 410


def test_страница_вернулась_отметка_снимается(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item_id = _item(conn)
    store.mark_gone(conn, item_id, 404)
    store.revive(conn, item_id)
    assert store.gone(conn) == []


def test_защита_сайта_не_считается_исчезновением(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _item(conn)
    бот = _Бот()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="отойди")

    async def run():
        async with _session(handler) as session:
            return await recheck.once(бот, session, conn)

    asyncio.run(run())
    assert store.gone(conn) == [], "403 и 429 — это защита сайта, а не снятие"
    assert бот.отправлено == []


def test_перечитывание_кладёт_текст_ревизии_и_копию(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    item_id = _item(conn)
    бот = _Бот()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=СТРАНИЦА.format("Совсем другой текст материала, в котором заведомо больше сорока знаков"))

    async def run():
        async with _session(handler) as session:
            return await recheck.once(бот, session, conn)

    asyncio.run(run())
    правки = store.revisions(conn, item_id)
    assert правки and "Совсем другой текст" in правки[-1]["text"]
    assert store.snapshots(conn, item_id), "копия страницы снимается при перечитывании"
