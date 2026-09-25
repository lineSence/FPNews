"""Бот: подписки на темы. Один бот на всех, выдача индивидуальная.

Команды нарочно короткие и русские — ими пользуются с телефона:

    /старт          — завести себя
    /темы           — список своих тем
    /добавить дроны, беспилотники, бпла
    /удалить 3
    /задержка       — как быстро доходят новости
    /сводка 09:00   — ежедневная сводка вместо потока

Команд управления источниками нет: список изданий общий и меняется в коде, а
не пользователем. Что своё у каждого — темы и подписка `[NEWS-005]`.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any

from . import enrich, model
from . import run as run_module
from . import store, topics

log = logging.getLogger("fpnews.bot")

HELP = (
    "Я приношу новости выбранных изданий по вашим темам.\n\n"
    "<b>/добавить</b> слова через запятую — новая тема\n"
    "<b>/темы</b> — список\n"
    "<b>/удалить</b> номер — убрать тему\n"
    "<b>/задержка</b> — как быстро доходят новости\n"
    "<b>/сводка</b> — что я пропустил; «/сводка 09:00» — присылать каждый день\n"
    "<b>/вход</b> — ссылка в веб-интерфейс\n\n"
    "Тема ловит слова в любой форме: «дрон» найдёт «дроны» и «дронов». "
    "Фраза в кавычках ищется целиком.\n\n"
    "Под каждой новостью три кнопки — выжимка, цитата, оценка. "
    "Модель работает только по нажатию и только на текст этой новости."
)


def ensure_user(conn: sqlite3.Connection, user_id: int, name: str = "") -> None:
    conn.execute(
        "INSERT INTO users(id, name, created_at) VALUES(?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        (user_id, name, store.now()),
    )
    conn.commit()


def add_topic(conn: sqlite3.Connection, user_id: int, raw: str) -> str:
    words = topics.parse_words(raw)
    if not words:
        return "Нужны слова: <code>/добавить дроны, бпла</code>"
    title = words[0]
    conn.execute(
        "INSERT INTO topics(user_id, title, words, created_at) VALUES(?,?,?,?)",
        (user_id, title, ", ".join(words), store.now()),
    )
    conn.commit()
    return "Тема «{}» добавлена: {}".format(title, ", ".join(words))


def list_topics(conn: sqlite3.Connection, user_id: int) -> str:
    rows = conn.execute(
        "SELECT id, title, words, enabled FROM topics WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    if not rows:
        return "Тем пока нет. <code>/добавить дроны, бпла</code>"
    lines = [
        "{}. <b>{}</b> — {}{}".format(
            row["id"], row["title"], row["words"], "" if row["enabled"] else " (выключена)"
        )
        for row in rows
    ]
    return "\n".join(lines)


def drop_topic(conn: sqlite3.Connection, user_id: int, raw: str) -> str:
    try:
        topic_id = int(str(raw).strip())
    except ValueError:
        return "Нужен номер темы: <code>/удалить 3</code>"
    cursor = conn.execute(
        "DELETE FROM topics WHERE id = ? AND user_id = ?", (topic_id, user_id)
    )
    conn.commit()
    return "Удалено" if cursor.rowcount else "Такой темы у вас нет"


def latency_text(conn: sqlite3.Connection) -> str:
    data = run_module.report(conn, 200)
    if not data.get("новостей"):
        return "Пока нечего мерить: новостей после запуска не было."
    lines = ["Задержки по последним {} новостям, секунды:".format(data["новостей"])]
    names = {
        "редакционная": "издание → лента",
        "до_отправки": "лента → сообщение",
        "до_полного": "сообщение → дополнение",
    }
    for key, label in names.items():
        cell = data.get(key)
        if not cell:
            continue
        lines.append(
            "{}: медиана {}, девяностый {}, максимум {}".format(
                label, cell["медиана"], cell["девяностый"], cell["максимум"]
            )
        )
    return "\n".join(lines)


def digest_text(conn: sqlite3.Connection, user_id: int, tail: str = "") -> str:
    """Сводка по требованию и настройка её часа.

        /сводка          — показать прямо сейчас
        /сводка 09:00    — присылать каждый день в это время
        /сводка нет      — выключить
    """
    from . import digest, web  # noqa: PLC0415 — импорт здесь разрывает круг

    хвост = (tail or "").strip().lower()
    if хвост in ("нет", "выкл", "off", "стоп"):
        store.set_digest(conn, user_id, "")
        return "Ежедневная сводка выключена. «/сводка» без слов покажет её разово."
    if ":" in хвост:
        store.set_digest(conn, user_id, хвост)
        return "Буду присылать сводку каждый день в {}.".format(хвост)
    data = digest.collect(conn, user_id)
    if digest.empty(data):
        return ("За сутки мы не видели ни правок, ни снятий, и по вашим темам ничего не "
                "приходило. Всего материалов в базе за это время: {}.".format(data["всего"]))
    return digest.text(data, web.base_url())


def login_link(conn: sqlite3.Connection, user_id: int) -> str:
    """Одноразовая ссылка в веб. Пароля нет — значит нечему утечь."""
    from . import pages, web  # noqa: PLC0415 — импорт здесь разрывает круг

    return pages.link_message("{}/вход?код={}".format(web.base_url(),
                                                      web.code_for(conn, user_id)))


def answer(conn: sqlite3.Connection, user_id: int, name: str, text: str) -> str:
    """Ответ на одно сообщение. Чистая функция — потому и тестируется легко."""
    body = (text or "").strip()
    command, _, tail = body.partition(" ")
    command = command.lower().lstrip("/").split("@")[0]
    if command in ("старт", "start", "помощь", "help"):
        ensure_user(conn, user_id, name)
        return HELP
    ensure_user(conn, user_id, name)
    if command in ("добавить", "add"):
        return add_topic(conn, user_id, tail)
    if command in ("темы", "topics"):
        return list_topics(conn, user_id)
    if command in ("удалить", "del", "delete"):
        return drop_topic(conn, user_id, tail)
    if command in ("задержка", "latency"):
        return latency_text(conn)
    if command in ("сводка", "digest"):
        return digest_text(conn, user_id, tail)
    if command in ("вход", "login", "веб", "web"):
        return login_link(conn, user_id)
    return "Не понимаю. " + HELP


async def press(bot: Any, session: Any, conn: sqlite3.Connection, budget: Any,
                query: dict[str, Any]) -> bool:
    """Нажатие кнопки под новостью. Ответ приходит вторым сообщением.

    «Часики» на кнопке гасятся сразу: телеграм ждёт ответа несколько секунд, а
    модель думает дольше. Растянуть один на другого — значит показать человеку
    ошибку там, где всё в порядке.
    """
    chat = (query.get("message") or {}).get("chat") or {}
    parsed = enrich.parse(str(query.get("data") or ""))
    if not chat.get("id") or parsed is None:
        await bot.ack(str(query.get("id") or ""))
        return False
    kind, item_id = parsed
    await bot.ack(str(query.get("id") or ""), "Спрашиваю модель…")
    try:
        text = await enrich.make(session, conn, item_id, kind, budget)
    except Exception as exc:  # noqa: BLE001 — кнопка не роняет бота [CORE-017]
        log.warning("кнопка «%s» для %s сорвалась: %s", kind, item_id, exc)
        text = "Не получилось. Попробуйте ещё раз чуть позже."
    await bot.send(int(chat["id"]), text, preview=False)
    return True


async def serve(bot: Any, conn: sqlite3.Connection, stop: Any, rounds: int = 0,
                session: Any = None, budget: Any = None) -> int:
    """Длинный опрос обновлений. Отдельная задача, сторожам не мешает."""
    handled = 0
    budget = budget if budget is not None else model.Budget()
    pending: set[Any] = set()
    while not stop.is_set():
        for update in await bot.updates():
            query = update.get("callback_query")
            if query:
                # Отдельной задачей: пока модель думает, бот отвечает другим.
                task = asyncio.ensure_future(press(bot, session, conn, budget, query))
                pending.add(task)
                task.add_done_callback(pending.discard)
                handled += 1
                continue
            message = update.get("message") or update.get("edited_message") or {}
            chat = message.get("chat") or {}
            user = message.get("from") or {}
            if not chat.get("id"):
                continue
            reply = answer(
                conn,
                int(chat["id"]),
                str(user.get("first_name") or ""),
                str(message.get("text") or ""),
            )
            await bot.send(int(chat["id"]), reply, preview=False)
            handled += 1
        rounds -= 1
        if rounds == 0:
            break
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return handled


__all__ = ("HELP", "add_topic", "answer", "drop_topic", "ensure_user", "latency_text",
           "list_topics", "login_link", "press", "serve")
