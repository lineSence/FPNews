"""Доступ по приглашению: ключ, отпечаток, роли, отзыв."""

from __future__ import annotations

import base64

import pytest

from news import access, store, web


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("FPNEWS_SECRET", "секрет-для-тестов")
    monkeypatch.delenv("FPNEWS_OWNER", raising=False)
    access.забыть_попытки()
    соединение = store.connect(tmp_path / "t.sqlite3")
    yield соединение
    соединение.close()


def test_ключ_выглядит_как_ключ_vpn():
    ключ = access.новый_ключ()
    assert len(ключ) == 44 and ключ.endswith("=")
    assert len(base64.b64decode(ключ)) == 32
    assert "fpnews" not in ключ.lower()
    assert access.похож_на_ключ(ключ)


def test_ключи_не_повторяются():
    assert len({access.новый_ключ() for _ in range(50)}) == 50


def test_в_базе_лежит_отпечаток_а_не_ключ(conn):
    ключ, номер = access.выдать(conn, "Петя")
    строка = conn.execute("SELECT fingerprint FROM invites WHERE id = ?",
                          (номер,)).fetchone()
    assert строка["fingerprint"] != ключ
    assert ключ not in str(dict(строка))
    assert len(строка["fingerprint"]) == 64


def test_приглашение_одноразовое(conn):
    ключ, _ = access.выдать(conn, "Петя")
    assert access.принять(conn, ключ, 101, "Петя") == access.ЧИТАТЕЛЬ
    assert access.известен(conn, 101)
    assert access.принять(conn, ключ, 102) == ""
    assert not access.известен(conn, 102)


def test_чужой_и_кривой_ключ_не_проходят(conn):
    access.выдать(conn, "Петя")
    assert access.принять(conn, access.новый_ключ(), 103) == ""
    assert access.принять(conn, "", 103) == ""
    assert access.принять(conn, "не ключ", 103) == ""


def test_просроченное_приглашение_не_проходит(conn):
    ключ, номер = access.выдать(conn, "Петя", дней=1)
    conn.execute("UPDATE invites SET expires_at = datetime('now', '-1 day') WHERE id = ?",
                 (номер,))
    conn.commit()
    assert access.принять(conn, ключ, 104) == ""


def test_роль_владельца_переносится_из_приглашения(conn):
    ключ, _ = access.выдать(conn, "Хозяин", роль=access.ВЛАДЕЛЕЦ)
    assert access.принять(conn, ключ, 105) == access.ВЛАДЕЛЕЦ
    assert access.владелец(conn, 105)
    assert access.владельцы(conn) == [105]


def test_отзыв_гасит_доступ_и_сессии(conn):
    ключ, номер = access.выдать(conn, "Петя")
    access.принять(conn, ключ, 106)
    web.new_session(conn, 106)
    assert access.отозвать(conn, номер)
    assert not access.известен(conn, 106)
    assert conn.execute("SELECT count(*) c FROM sessions WHERE user_id = 106"
                        ).fetchone()["c"] == 0
    assert not access.отозвать(conn, номер)
    assert not access.отозвать(conn, "мусор")


def test_список_приглашений_показывает_состояние(conn):
    ключ, первый = access.выдать(conn, "Петя")
    access.выдать(conn, "Вася")
    access.принять(conn, ключ, 107, "Петя")
    записи = {row["номер"]: row for row in access.приглашения(conn)}
    assert записи[первый]["состояние"] == "использовано"
    assert записи[первый]["кем"] == 107
    assert записи[первый + 1]["состояние"] == "ждёт"
    access.отозвать(conn, первый)
    assert {row["номер"]: row for row in access.приглашения(conn)
            }[первый]["состояние"] == "отозвано"


def test_отпечаток_зависит_от_секрета(conn, monkeypatch):
    ключ = access.новый_ключ()
    один = access.отпечаток(conn, ключ)
    monkeypatch.setenv("FPNEWS_SECRET", "другой секрет")
    assert access.отпечаток(conn, ключ) != один


def test_секрет_заводится_сам_и_не_меняется(tmp_path, monkeypatch):
    monkeypatch.delenv("FPNEWS_SECRET", raising=False)
    соединение = store.connect(tmp_path / "s.sqlite3")
    первый = access.secret(соединение)
    assert первый and access.secret(соединение) == первый
    соединение.close()


def test_бутстрап_владельца_из_переменной(conn, monkeypatch):
    monkeypatch.setenv("FPNEWS_OWNER", "555")
    assert access.бутстрап(conn) == 555
    assert access.владелец(conn, 555)
    monkeypatch.setenv("FPNEWS_OWNER", "не число")
    assert access.бутстрап(conn) == 0


def test_ограничитель_попыток():
    access.забыть_попытки()
    assert all(access.попытка(1) for _ in range(access.ПОПЫТОК))
    assert not access.попытка(1)
    assert access.попытка(2)
    access.забыть_попытки(1)
    assert access.попытка(1)


def test_старые_пользователи_остаются_читателями(tmp_path):
    путь = tmp_path / "old.sqlite3"
    соединение = store.connect(путь)
    соединение.execute("DROP TABLE users")
    соединение.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT "
                       "NOT NULL DEFAULT '', created_at TEXT)")
    соединение.execute("INSERT INTO users(id, name) VALUES(7, 'Старый')")
    соединение.commit()
    store.ensure(соединение)
    assert access.роль(соединение, 7) == access.ЧИТАТЕЛЬ
    соединение.close()
