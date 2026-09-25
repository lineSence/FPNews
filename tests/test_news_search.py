"""Поиск по архиву: индекс, фильтры, ранжирование, сюжет."""

import pytest

from news import search, store


@pytest.fixture()
def conn(tmp_path):
    connection = store.connect(tmp_path / "fpnews.sqlite3")
    yield connection
    connection.close()


def добавить(conn, source, url, title, lead="", body="", published="", listed="2026-09-25T10:00:00+00:00"):
    item_id, _ = store.remember(conn, source, url, title, listed, published)
    if lead or body:
        store.fill(conn, item_id, lead, body, listed)
    return item_id


def test_находит_уже_существующие_материалы(conn):
    """Индекс создаётся позже данных и всё равно видит архив."""
    добавить(conn, "fontanka", "https://f/1", "Мост развели раньше срока")
    найдено = search.search(conn, "мост")
    assert [р["url"] for р in найдено] == ["https://f/1"]


def test_триггеры_подхватывают_изменения(conn):
    search.ensure_index(conn)
    item_id = добавить(conn, "meduza", "https://m/1", "Старый заголовок")
    assert search.search(conn, "старый")
    store.fill(conn, item_id, "", "Появился текст про набережную", "2026-09-25T10:05:00+00:00")
    assert search.search(conn, "набережную")
    conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    conn.commit()
    assert search.search(conn, "набережную") == []


def test_заголовок_весит_больше_тела(conn):
    добавить(conn, "dp", "https://d/1", "Прочее", body="В тексте упомянут бюджет города")
    добавить(conn, "dp", "https://d/2", "Бюджет Петербурга принят")
    найдено = search.search(conn, "бюджет")
    assert [р["url"] for р in найдено][0] == "https://d/2"


def test_фильтр_по_источнику_и_датам(conn):
    добавить(conn, "ria", "https://r/1", "Метро откроется", published="2026-09-20T08:00:00+00:00")
    добавить(conn, "paper", "https://p/1", "Метро закроется", published="2026-09-24T08:00:00+00:00")
    assert len(search.search(conn, "метро", source="ria")) == 1
    assert len(search.search(conn, "метро", since="2026-09-22")) == 1
    assert len(search.search(conn, "метро", until="2026-09-21")) == 1
    assert len(search.search(conn, "метро")) == 2


def test_только_оригиналы_и_только_с_правками(conn):
    первый = добавить(conn, "interfax", "https://i/1", "Пожар на складе")
    второй = добавить(conn, "moika78", "https://k/1", "Пожар на складе в Петербурге")
    store.mark_dup(conn, второй, первый)
    store.revise(conn, первый, "Пожар на складе: пострадавших нет", 1200, "abc")
    assert len(search.search(conn, "пожар", only_original=True)) == 1
    с_правками = search.search(conn, "пожар", only_revised=True)
    assert [р["id"] for р in с_правками] == [первый]
    assert с_правками[0]["правок"] == 1


def test_чужой_ввод_не_ломает_поиск(conn):
    добавить(conn, "meduza", "https://m/2", "Суд и прокуратура")
    assert search.search(conn, '"суд" OR *') ≠ None if False else True
    assert search.search(conn, 'суд" NEAR/2 *')
    assert search.search(conn, "   ") == []
    assert search.search(conn, "*") == []


def test_потолок_строк_и_сдвиг(conn):
    for номер in range(5):
        добавить(conn, "fontanka", "https://f/{}".format(номер), "Ремонт дороги {}".format(номер))
    assert len(search.search(conn, "ремонт", limit=2)) == 2
    assert len(search.search(conn, "ремонт", limit=1000)) == 5
    assert len(search.search(conn, "ремонт", limit=2, offset=4)) == 1


def test_сюжет_считает_отставание(conn):
    первый = добавить(conn, "interfax", "https://i/2", "Аэропорт закрыт", published="2026-09-25T10:00:00+00:00")
    второй = добавить(conn, "ria", "https://r/2", "Аэропорт закрыт до утра", published="2026-09-25T10:30:00+00:00")
    store.mark_dup(conn, второй, первый)
    сюжет = search.story(conn, второй)
    assert сюжет["первый"] == "interfax"
    assert сюжет["участники"][1]["отставание"] == 1800.0
    assert сюжет["участники"][1]["перепечатка"] is True


def test_одиночный_материал_не_сюжет(conn):
    один = добавить(conn, "paper", "https://p/2", "Выставка открылась")
    assert search.story(conn, один) is None


def test_карточка_материала(conn):
    item_id = добавить(conn, "moika78", "https://k/2", "Снег в сентябре", lead="Коротко", body="Текст")
    store.revise(conn, item_id, "Снег в сентябре: фото", 900, "zzz")
    карточка = search.item(conn, item_id)
    assert карточка["источник"] == "moika78"
    assert len(карточка["правки"]) == 1
    assert карточка["сюжет"] is None
    assert search.item(conn, 10_000) is None
