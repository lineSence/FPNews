"""Доступ по личному приглашению: ключ, роли, учёт выдач.

Зачем это вообще. До сих пор интерфейс и бот были открыты любому, кто узнал
имя бота: `ensure_user` заводила учётку каждому, кто написал «/старт».
Для наблюдательной системы это неверно — она показывает, за какими темами и
изданиями следят, и чужой человек не должен видеть даже этого `[CORE-016]`.

Замысел. Приглашение и вход — разные вещи:

* **приглашение** выдаёт владелец, оно одноразовое и живёт две недели;
* **вход** остаётся прежним: одноразовый код из бота и кука сессии.

Ключ намеренно выглядит как ключ WireGuard: 32 случайных байта в base64,
44 символа с «=» на конце. Никаких префиксов вроде «fpnews_» — по самой
строке нельзя понять, от чего она, а перехваченная переписка с такой строкой
читается как обмен настройками VPN `[CORE-016]`. Ценности в маскировке самой
по себе нет (стойкость даёт длина, а не вид), но она убирает подсказку тому,
кто ключ случайно увидел.

Хранение. В базе лежит не ключ, а `blake2b(ключ, key=секрет)`: кража копии
базы не даёт войти. Секрет берётся из `FPNEWS_SECRET`, а если переменной нет —
создаётся один раз и хранится в `settings`. Сравнение отпечатков — через
`secrets.compare_digest`, чтобы время ответа не подсказывало совпадение.

Ключ никогда не попадает в журнал: пишем только номер приглашения `[NEWS-006]`.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import sqlite3
import time
from typing import Any

from . import store

log = logging.getLogger("fpnews.access")

# 32 байта — столько же, сколько в ключе WireGuard, и столько же, сколько
# берёт blake2b как ключ. Меньше нельзя, больше незачем.
KEY_BYTES = 32
# Сколько живёт невостребованное приглашение. Две недели — срок, за который
# человек либо воспользуется ссылкой, либо про неё забудут все.
DEFAULT_DAYS = 14
ВЛАДЕЛЕЦ = "владелец"
ЧИТАТЕЛЬ = "читатель"
РОЛИ = (ВЛАДЕЛЕЦ, ЧИТАТЕЛЬ)

# Попытки ввода ключа: пять на человека в час. Держим в памяти, а не в базе —
# перезапуск сбрасывает счётчик, и это допустимо: ключ всё равно не подобрать
# перебором, ограничитель бережёт журнал и процессор [NEWS-002].
ПОПЫТОК = 5
ОКНО = 3600.0
_попытки: dict[int, list[float]] = {}


def secret(conn: sqlite3.Connection) -> bytes:
    """Ключ для отпечатков. Из переменной окружения, иначе — свой в базе."""
    из_среды = (os.getenv("FPNEWS_SECRET") or "").strip()
    if из_среды:
        return из_среды.encode("utf-8")
    свой = get_setting(conn, "secret")
    if not свой:
        свой = secrets.token_urlsafe(32)
        set_setting(conn, "secret", свой)
    return свой.encode("utf-8")


def get_setting(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (name,)).fetchone()
    return str(row["value"]) if row is not None else ""


def set_setting(conn: sqlite3.Connection, name: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (name, value, store.now()),
    )
    conn.commit()


def новый_ключ() -> str:
    """Случайный ключ. Вид — как у ключа WireGuard, суть — 256 бит случайности."""
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")


def похож_на_ключ(raw: Any) -> bool:
    """Беглая проверка вида. Нужна, чтобы не считать отпечаток от мусора."""
    ключ = str(raw or "").strip()
    return len(ключ) == 44 and ключ.endswith("=")


def отпечаток(conn: sqlite3.Connection, ключ: str) -> str:
    """Отпечаток ключа. Только его и видит база."""
    return hashlib.blake2b(
        str(ключ or "").strip().encode("utf-8"), key=secret(conn), digest_size=32
    ).hexdigest()


def выдать(conn: sqlite3.Connection, кому: str = "", *, роль: str = ЧИТАТЕЛЬ,
           дней: int = DEFAULT_DAYS) -> tuple[str, int]:
    """Создать приглашение. Ключ возвращается один раз — потом только отпечаток."""
    роль = роль if роль in РОЛИ else ЧИТАТЕЛЬ
    ключ = новый_ключ()
    курсор = conn.execute(
        "INSERT INTO invites(fingerprint, note, role, issued_at, expires_at) "
        "VALUES(?,?,?,?, datetime('now', '+{} days'))".format(int(дней)),
        (отпечаток(conn, ключ), str(кому or "").strip()[:200], роль, store.now()),
    )
    conn.commit()
    log.info("выдано приглашение %s (роль %s)", курсор.lastrowid, роль)
    return ключ, int(курсор.lastrowid or 0)


def принять(conn: sqlite3.Connection, ключ: str, user_id: int, name: str = "") -> str:
    """Погасить приглашение и завести человека. Пусто — ключ не подошёл.

    Причину не уточняем: «нет такого», «просрочено» и «уже использовано» дают
    одинаковый ответ, иначе перебор подсказывал бы, какие ключи существуют.
    """
    if not похож_на_ключ(ключ):
        return ""
    мой = отпечаток(conn, ключ)
    строка = None
    for row in conn.execute(
        "SELECT id, fingerprint, role FROM invites "
        "WHERE used_at IS NULL AND revoked_at IS NULL "
        "AND julianday(expires_at) > julianday('now')"
    ):
        # Перебираем сами и сравниваем постоянным по времени сличением:
        # обычное «WHERE fingerprint = ?» по индексу тоже годится, но так
        # правило одно для всех сравнений секретов [CORE-016].
        if secrets.compare_digest(str(row["fingerprint"]), мой):
            строка = row
            break
    if строка is None:
        return ""
    роль = str(строка["role"]) if str(строка["role"]) in РОЛИ else ЧИТАТЕЛЬ
    conn.execute(
        "INSERT INTO users(id, name, role, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, role = excluded.role",
        (int(user_id), str(name or ""), роль, store.now()),
    )
    conn.execute(
        "UPDATE invites SET used_at = ?, used_by = ? WHERE id = ?",
        (store.now(), int(user_id), int(строка["id"])),
    )
    conn.commit()
    log.info("приглашение %s принято, роль %s", строка["id"], роль)
    return роль


def отозвать(conn: sqlite3.Connection, invite_id: Any) -> bool:
    """Отозвать приглашение. Если им уже вошли — гасим человека и его сессии."""
    try:
        номер = int(str(invite_id).strip())
    except (TypeError, ValueError):
        return False
    row = conn.execute("SELECT used_by, revoked_at FROM invites WHERE id = ?",
                       (номер,)).fetchone()
    if row is None or row["revoked_at"]:
        return False
    conn.execute("UPDATE invites SET revoked_at = ? WHERE id = ?", (store.now(), номер))
    if row["used_by"]:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (int(row["used_by"]),))
        conn.execute("DELETE FROM login_codes WHERE user_id = ?", (int(row["used_by"]),))
        conn.execute("UPDATE users SET role = '' WHERE id = ?", (int(row["used_by"]),))
    conn.commit()
    log.info("приглашение %s отозвано", номер)
    return True


def приглашения(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Список выдач для страницы «Доступы». Ключей здесь нет и быть не может."""
    out = []
    for row in conn.execute(
        "SELECT i.id, i.note, i.role, i.issued_at, i.expires_at, i.used_at, "
        "i.used_by, i.revoked_at, u.name AS имя FROM invites i "
        "LEFT JOIN users u ON u.id = i.used_by ORDER BY i.id DESC"
    ):
        out.append({
            "номер": int(row["id"]),
            "кому": row["note"] or "",
            "роль": row["role"] or ЧИТАТЕЛЬ,
            "выдан": row["issued_at"] or "",
            "годен_до": row["expires_at"] or "",
            "использован": row["used_at"] or "",
            "кем": int(row["used_by"]) if row["used_by"] else 0,
            "имя": row["имя"] or "",
            "отозван": row["revoked_at"] or "",
            "состояние": состояние(row),
        })
    return out


def состояние(row: Any) -> str:
    """Словами: что с приглашением сейчас. Наблюдение, а не приговор [NEWS-008]."""
    if row["revoked_at"]:
        return "отозвано"
    if row["used_at"]:
        return "использовано"
    return "ждёт"


def роль(conn: sqlite3.Connection, user_id: Any) -> str:
    """Роль человека. Пусто — такого у нас нет, и это не то же, что «читатель»."""
    row = conn.execute("SELECT role FROM users WHERE id = ?",
                       (int(user_id or 0),)).fetchone()
    return str(row["role"] or "") if row is not None else ""


def известен(conn: sqlite3.Connection, user_id: Any) -> bool:
    return роль(conn, user_id) in РОЛИ


def владелец(conn: sqlite3.Connection, user_id: Any) -> bool:
    return роль(conn, user_id) == ВЛАДЕЛЕЦ


def назначить(conn: sqlite3.Connection, user_id: Any, новая: str) -> bool:
    if новая not in РОЛИ:
        return False
    conn.execute(
        "INSERT INTO users(id, role, created_at) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET role = excluded.role",
        (int(user_id), новая, store.now()),
    )
    conn.commit()
    return True


def владельцы(conn: sqlite3.Connection) -> list[int]:
    return [int(row["id"]) for row in
            conn.execute("SELECT id FROM users WHERE role = ?", (ВЛАДЕЛЕЦ,))]


def бутстрап(conn: sqlite3.Connection) -> int:
    """Первый владелец — из `FPNEWS_OWNER`. Без него систему некому открыть.

    Переменная, а не ключ: ключ надо кому-то выдать, а выдавать пока некому.
    Значение — Telegram-id владельца, его показывает сам бот в ответе на
    попытку написать без приглашения.
    """
    raw = (os.getenv("FPNEWS_OWNER") or "").strip()
    try:
        номер = int(raw)
    except ValueError:
        return 0
    if номер <= 0:
        return 0
    назначить(conn, номер, ВЛАДЕЛЕЦ)
    return номер


def попытка(user_id: Any) -> bool:
    """Можно ли ещё пробовать ключ. Пять попыток в час на человека."""
    ключ = int(user_id or 0)
    сейчас = time.monotonic()
    было = [t for t in _попытки.get(ключ, []) if сейчас - t < ОКНО]
    _попытки[ключ] = было
    if len(было) >= ПОПЫТОК:
        return False
    было.append(сейчас)
    return True


def забыть_попытки(user_id: Any = None) -> None:
    """Сброс счётчика: после удачного ключа и в тестах."""
    if user_id is None:
        _попытки.clear()
    else:
        _попытки.pop(int(user_id or 0), None)


__all__ = ("DEFAULT_DAYS", "KEY_BYTES", "ВЛАДЕЛЕЦ", "ЧИТАТЕЛЬ", "РОЛИ", "бутстрап",
           "владелец", "владельцы", "выдать", "get_setting", "забыть_попытки",
           "известен", "назначить", "новый_ключ", "отозвать", "отпечаток",
           "похож_на_ключ", "попытка", "приглашения", "принять", "роль", "secret",
           "set_setting", "состояние")
