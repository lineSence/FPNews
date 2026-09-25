"""Общая лента: отбор, порядок, папки — и те же настройки в отдаче.

Лента показывает весь собранный выход, а не только то, что прошло темы.
Поэтому проверяем не вёрстку, а отбор: что фильтр сужает, порядок меняет
и папки раскладывают ровно то же множество строк, ничего не теряя.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from news import bot as bot_module
from news import deliver, search, store, stream, web


def _conn(tmp_path: Path):
    conn = store.connect(tmp_path / "db.sqlite3")
    bot_module.ensure_user(conn, 7, "Владелец")
    return conn


def _add(conn, title: str, source: str = "fontanka", *, body: str = "",
         published: str = "", listed: str = "") -> int:
    адрес = "https://example.org/{}/{}".format(source, abs(hash(title)) % 10**9)
    item_id, _ = store.remember(conn, source, адрес, title, listed or store.now(),
                                published_at=published)
    conn.execute("UPDATE items SET lead = ?, body = ? WHERE id = ?",
                 ((body or title)[:200], body or title, item_id))
    conn.commit()
    return item_id


class _Бот:
    def __init__(self) -> None:
        self.отправлено: list[tuple[int, str]] = []

    async def send(self, user_id, text, **kwargs):  # noqa: ANN001, ANN003
        self.отправлено.append((user_id, text))
        return True


def test_лента_без_фильтров_показывает_всё(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Мост закрыли")
    _add(conn, "Тариф вырос", "dp")
    строки = stream.select(conn, stream.Filter())
    assert {row["заголовок"] for row in строки} == {"Мост закрыли", "Тариф вырос"}


def test_отбор_по_словам_и_изданиям(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    search.ensure_index(conn)
    _add(conn, "Тариф на воду вырос", "fontanka")
    _add(conn, "Тариф на свет вырос", "dp")
    _add(conn, "Мост развели", "dp")
    по_слову = stream.select(conn, stream.Filter(words="тариф"))
    assert len(по_слову) == 2
    по_изданию = stream.select(conn, stream.Filter(sources=("dp",)))
    assert {row["источник"] for row in по_изданию} == {"dp"}
    вместе = stream.select(conn, stream.Filter(words="тариф", sources=("dp",)))
    assert [row["заголовок"] for row in вместе] == ["Тариф на свет вырос"]


def test_период_и_даты_ограничивают_выборку(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Сегодняшняя", published="2026-09-25T10:00:00+00:00",
         listed="2026-09-25T10:01:00+00:00")
    _add(conn, "Прошлогодняя", published="2025-01-05T10:00:00+00:00",
         listed="2025-01-05T10:01:00+00:00")
    свежее = stream.select(conn, stream.Filter(period="месяц"))
    assert [row["заголовок"] for row in свежее] == ["Сегодняшняя"]
    окно = stream.select(conn, stream.Filter(period="всё время", since="2025-01-01",
                                             until="2025-12-31"))
    assert [row["заголовок"] for row in окно] == ["Прошлогодняя"]


def test_порядок_меняется(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Ранняя", "fontanka", published="2026-09-20T08:00:00+00:00",
         listed="2026-09-20T08:02:00+00:00")
    _add(conn, "Поздняя", "dp", published="2026-09-24T08:00:00+00:00",
         listed="2026-09-24T12:00:00+00:00")
    новые = stream.select(conn, stream.Filter(period="всё время"))
    assert новые[0]["заголовок"] == "Поздняя"
    старые = stream.select(conn, stream.Filter(period="всё время", sort="старые сверху"))
    assert старые[0]["заголовок"] == "Ранняя"
    по_задержке = stream.select(conn, stream.Filter(period="всё время", sort="по задержке"))
    assert по_задержке[0]["заголовок"] == "Поздняя", "четыре часа больше двух минут"


def test_папки_по_дате_и_изданию(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Первая", "fontanka", published="2026-09-24T08:00:00+00:00")
    _add(conn, "Вторая", "dp", published="2026-09-25T08:00:00+00:00")
    _add(conn, "Третья", "dp", published="2026-09-25T09:00:00+00:00")
    строки = stream.select(conn, stream.Filter(period="всё время"))
    по_дате = stream.folders(conn, строки, stream.Filter(group="по дате"), 7)
    assert [name for name, _ in по_дате] == ["2026-09-25", "2026-09-24"]
    по_изданию = stream.folders(conn, строки, stream.Filter(group="по изданию"), 7)
    assert по_изданию[0][0] == "dp" and len(по_изданию[0][1]) == 2


def test_умные_папки_по_темам(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    bot_module.add_topic(conn, 7, "мост, метро")
    _add(conn, "Мост развели раньше срока")
    _add(conn, "Метро закрыли на ремонт")
    _add(conn, "Погода испортилась")
    строки = stream.select(conn, stream.Filter())
    папки = stream.folders(conn, строки, stream.Filter(group="по теме"), 7)
    assert папки[0][0] == "мост" and len(папки[0][1]) == 2
    assert папки[-1][0] == "Прочее" and len(папки[-1][1]) == 1
    assert sum(len(group) for _, group in папки) == len(строки), "ничего не потеряли"


def test_папка_на_сюжет_собирает_перепечатки(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    первый = _add(conn, "Мост закрыли", "fontanka")
    второй = _add(conn, "Мост закрыли на ремонт", "dp")
    store.mark_dup(conn, второй, первый)
    строки = stream.select(conn, stream.Filter())
    папки = stream.folders(conn, строки, stream.Filter(group="по сюжету"), 7)
    assert папки[0][0] == "Мост закрыли" and len(папки[0][1]) == 2


def test_чужая_ссылка_не_роняет_ленту(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Что-нибудь")
    flt = stream.read({"порядок": "как попало", "папки": "выдумка", "период": "вечность",
                       "стр": "ерунда", "тема": "нет"})
    assert flt.sort == "новые сверху" and flt.group == "без папок"
    assert flt.period == "неделя" and flt.page == 1 and flt.topic_id == 0
    assert stream.select(conn, flt) != []


def test_ссылка_сохраняет_фильтры(tmp_path: Path) -> None:
    flt = stream.Filter(words="тариф", sources=("dp", "fontanka"), period="месяц",
                        sort="по изданию", group="по теме", page=2)
    адрес = stream.link(flt, стр=3)
    assert "q=%D1%82%D0%B0%D1%80%D0%B8%D1%84" in адрес
    assert адрес.count("%D0%B8%D0%B7%D0%B4%D0%B0%D0%BD%D0%B8%D0%B5=") == 2
    assert "%D1%81%D1%82%D1%80=3" in адрес


def test_страница_ленты_отдаётся(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _add(conn, "Мост закрыли", "fontanka")
    _add(conn, "Тариф вырос", "dp")
    token = web.new_session(conn, 7)
    head = ("GET /лента?издание=dp&папки=%D0%BF%D0%BE+%D0%B8%D0%B7%D0%B4%D0%B0%D0%BD%D0%B8%D1%8E"
            " HTTP/1.1\r\nHost: localhost\r\nCookie: {}={}".format(web.COOKIE, token))
    тело = web.route(conn, web.parse(head, "")).body
    assert "Тариф вырос" in тело and "Мост закрыли" not in тело


def test_отдача_уважает_выбор_изданий(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    bot_module.add_topic(conn, 7, "мост")
    store.set_user_sources(conn, 7, ["fontanka"])
    свой = _add(conn, "Мост развели", "fontanka")
    чужой = _add(conn, "Мост закрыли", "dp")
    бот = _Бот()
    assert asyncio.run(deliver.send_item(бот, conn, свой)) == 1
    assert asyncio.run(deliver.send_item(бот, conn, чужой)) == 0
    assert deliver.allowed(conn, 7, "fontanka") and not deliver.allowed(conn, 7, "dp")


def test_пустой_список_изданий_значит_все(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    store.set_user_sources(conn, 7, [])
    assert store.user_sources(conn, 7) == set()
    assert deliver.allowed(conn, 7, "dp")


def test_выключенная_тема_не_отдаётся(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    bot_module.add_topic(conn, 7, "мост")
    номер = int(store.topics_of(conn, 7)[0]["id"])
    assert store.set_topic_delivery(conn, номер, 7, enabled=False)
    item = _add(conn, "Мост развели")
    бот = _Бот()
    assert asyncio.run(deliver.send_item(бот, conn, item)) == 0
    store.set_topic_delivery(conn, номер, 7, enabled=True, sources=["dp"])
    assert asyncio.run(deliver.send_item(бот, conn, item)) == 0, "тема слушает только dp"
    store.set_topic_delivery(conn, номер, 7, enabled=True, sources=["fontanka"])
    assert asyncio.run(deliver.send_item(бот, conn, item)) == 1


def test_чужую_тему_не_переключить(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    bot_module.ensure_user(conn, 8, "Сосед")
    bot_module.add_topic(conn, 8, "мост")
    чужая = int(store.topics_of(conn, 8)[0]["id"])
    assert not store.set_topic_delivery(conn, чужая, 7, enabled=False)
    assert store.topics_of(conn, 8)[0]["enabled"] == 1


def test_формы_отдачи_сохраняют_настройки(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    bot_module.add_topic(conn, 7, "мост")
    номер = int(store.topics_of(conn, 7)[0]["id"])
    token = web.new_session(conn, 7)
    метка = "метка=" + web.csrf(token)

    def _post(path: str, body: str):
        head = ("POST {} HTTP/1.1\r\nHost: localhost\r\nCookie: {}={}"
                "\r\nContent-Type: application/x-www-form-urlencoded").format(
                    path, web.COOKIE, token)
        return web.route(conn, web.parse(head, body))

    assert _post("/телеграм/издания", метка + "&издание=dp&издание=fontanka").location == \
        "/телеграм"
    assert store.user_sources(conn, 7) == {"dp", "fontanka"}
    _post("/телеграм/тема", метка + "&номер={}&издание=dp".format(номер))
    тема = store.topics_of(conn, 7)[0]
    assert тема["enabled"] == 0 and тема["sources"] == "dp"
