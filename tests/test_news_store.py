"""База новостей: метки времени и защита от повторов."""

from __future__ import annotations

from pathlib import Path

import pytest

from news import store


def test_адрес_записывается_один_раз(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    first, is_new = store.remember(conn, "meduza", "https://meduza.io/news/1", "Раз",
                                   "2026-09-24T19:00:00")
    again, is_new_again = store.remember(conn, "meduza", "https://meduza.io/news/1", "Раз",
                                         "2026-09-24T19:05:00")
    assert is_new and not is_new_again
    assert first == again
    kept = conn.execute("SELECT listed_at FROM items WHERE id = ?", (first,)).fetchone()
    assert kept["listed_at"] == "2026-09-24T19:00:00", "момент обнаружения не переписывается"


def test_три_задержки_считаются_раздельно(tmp_path: Path) -> None:
    """Редакционную мы не контролируем, свою обязаны сокращать [NEWS-001]."""
    conn = store.connect(tmp_path / "db.sqlite3")
    item, _ = store.remember(conn, "fontanka", "https://www.fontanka.ru/2026/09/24/1", "Два",
                             "2026-09-24T19:00:30", published_at="2026-09-24T19:00:00")
    store.stamp(conn, item, "sent_at", "2026-09-24T19:00:32")
    store.stamp(conn, item, "enriched_at", "2026-09-24T19:00:45")
    row = store.latency_rows(conn)[0]
    assert row["редакционная"] == 30.0
    assert row["до_отправки"] == 2.0
    assert row["до_полного"] == 13.0


def test_недостающая_метка_не_даёт_выдуманной_цифры(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    store.remember(conn, "meduza", "https://meduza.io/news/3", "Три", "2026-09-24T19:00:00")
    row = store.latency_rows(conn)[0]
    assert row["редакционная"] is None and row["до_отправки"] is None


def test_чужое_имя_метки_отвергается(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    item, _ = store.remember(conn, "meduza", "https://meduza.io/news/4", "", "2026-09-24T19:00:00")
    with pytest.raises(ValueError):
        store.stamp(conn, item, "title", "нет")


def test_повтор_отправки_невозможен(tmp_path: Path) -> None:
    import sqlite3

    conn = store.connect(tmp_path / "db.sqlite3")
    conn.execute("INSERT INTO deliveries(item_id,user_id,kind,sent_at) VALUES(1,7,'сырое','t')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO deliveries(item_id,user_id,kind,sent_at) VALUES(1,7,'сырое','t')")


def test_база_в_режиме_wal(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db.sqlite3")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
