"""Сохранённые запросы: подписка на вопрос, а не на тему.

Проверяется главное: подписка отдаёт только то, что появилось после её
сохранения, повторно то же самое не присылает и не ходит в модель.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from news import queries, search, store


def _conn(tmp_path: Path):
    return store.connect(tmp_path / "db.sqlite3")


def _add(conn, title: str, body: str, source: str = "fontanka") -> int:
    адрес = "https://example.org/{}".format(abs(hash(title)) % 10**9)
    item_id, _ = store.remember(conn, source, адрес, title, store.now())
    conn.execute("UPDATE items SET title = ?, lead = ?, body = ? WHERE id = ?",
                 (title, body[:200], body, item_id))
    conn.commit()
    return item_id


class _Бот:
    def __init__(self) -> None:
        self.отправлено: list[tuple[int, str]] = []

    async def send(self, user_id, text, **kwargs):  # noqa: ANN001, ANN003
        self.отправлено.append((user_id, text))
        return True


def test_подписка_молчит_о_прошлом(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    search.ensure_index(conn)
    _add(conn, "Тариф на воду вырос", "Городской тариф на воду вырос с октября")
    store.add_query(conn, 7, "тариф")
    бот = _Бот()
    assert asyncio.run(queries.once(бот, conn)) == 0
    assert бот.отправлено == [], "архив до подписки — не новость [NEWS-004]"


def test_новая_находка_уходит_в_бот(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    search.ensure_index(conn)
    store.add_query(conn, 7, "тариф")
    _add(conn, "Тариф на тепло подняли", "Комитет объяснил, почему тариф на тепло подняли")
    бот = _Бот()
    assert asyncio.run(queries.once(бот, conn)) == 1
    кому, текст = бот.отправлено[0]
    assert кому == 7 and "тариф" in текст.lower()


def test_повторный_обход_не_шлёт_то_же_самое(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    search.ensure_index(conn)
    store.add_query(conn, 7, "тариф")
    _add(conn, "Тариф на свет", "Тариф на свет пересчитают в ноябре, объяснили в комитете")
    бот = _Бот()
    asyncio.run(queries.once(бот, conn))
    assert asyncio.run(queries.once(бот, conn)) == 0
    assert len(бот.отправлено) == 1


def test_фильтр_издания_соблюдается(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    search.ensure_index(conn)
    store.add_query(conn, 7, "подряд", source="dp")
    _add(conn, "Подряд без конкурса", "Подряд на набережную отдали без конкурса", "fontanka")
    бот = _Бот()
    assert asyncio.run(queries.once(бот, conn)) == 0


def test_выключенное_уведомление_только_отмечает(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    search.ensure_index(conn)
    query_id = store.add_query(conn, 7, "суд", notify=False)
    _add(conn, "Суд отложил заседание", "Городской суд отложил заседание по делу подрядчика")
    бот = _Бот()
    assert asyncio.run(queries.once(бот, conn)) == 0
    assert бот.отправлено == []
    assert store.queries(conn, 7)[0]["last_item_id"] > 0, "находки всё равно засчитаны"
    assert query_id == store.queries(conn, 7)[0]["id"]


def test_чужой_запрос_не_удаляется(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    query_id = store.add_query(conn, 7, "тариф")
    assert store.drop_query(conn, query_id, 8) is False
    assert store.drop_query(conn, query_id, 7) is True
    assert store.queries(conn, 7) == []


def test_пачка_ограничена(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    search.ensure_index(conn)
    store.add_query(conn, 7, "тариф")
    for number in range(queries.BATCH + 3):
        _add(conn, "Тариф номер {}".format(number),
             "Очередной тариф номер {} обсудили в комитете по тарифам".format(number))
    бот = _Бот()
    assert asyncio.run(queries.once(бот, conn)) == queries.BATCH
