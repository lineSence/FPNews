"""Бот: подписки на темы и меню управления. Один бот на всех, выдача личная.

Всё управление живёт в меню кнопок — его открывает команда /меню, и
печатать аргументы не нужно: экраны, три состояния издания, диалоги для
текста [NEWS-018]. Команды нарочно короткие и русские — ими пользуются
с телефона те, кто привык печатать:

    /старт          — завести себя
    /темы           — список своих тем
    /добавить дроны, беспилотники, бпла
    /удалить 3
    /задержка       — как быстро доходят новости
    /сводка 09:00   — ежедневная сводка вместо потока

Что своё у каждого — темы и подписка `[NEWS-005]`.

Бот закрыт. Незнакомому человеку он отвечает одной фразой и не заводит
учётку: войти можно только по личному приглашению владельца, командой
`/ключ <ключ>`. Почему ключ отправляют боту, а не открывают ссылкой:
ссылка с секретом внутри оседает в истории браузера, в журнале прокси и в
заголовке `Referer` — то есть в трёх местах, которых мы не видим `[CORE-016]`.
Сообщение в переписке с ботом остаётся у двоих.
"""

from __future__ import annotations

import asyncio
import html
import logging
import sqlite3
from typing import Any

from . import access, enrich, model
from . import run as run_module
from . import store, topics

log = logging.getLogger("fpnews.bot")

HELP = (
    "Я приношу новости выбранных изданий по вашим темам.\n\n"
    "Всё управление — кнопками: <b>/меню</b>. Печатать команды не нужно: "
    "издания, темы, сводка, запросы, доступы — всё экранами и кнопками.\n\n"
    "Команды для тех, кто привык печатать: <b>/темы</b>, <b>/подписки</b>, "
    "<b>/сводка</b>, <b>/лента</b>, <b>/источник</b>, <b>/вход</b>. "
    "Тема ловит слова в любой форме: «дрон» найдёт и «дроны», и «дронов».\n\n"
    "Под каждой новостью кнопки — «в тему / не в тему» и три действия: "
    "выжимка, цитата, оценка. Модель работает только по нажатию и только "
    "на текст этой новости."
)

# Один и тот же ответ незнакомому — и на «/старт», и на неподошедший ключ, и
# на исчерпанные попытки. Разные ответы подсказывали бы перебирающему, что он
# угадал наполовину.
ЗАКРЫТО = (
    "Это закрытая система наблюдения за новостями.\n\n"
    "Есть ключ доступа — пришлите его одной строкой: <code>/ключ ВАШ_КЛЮЧ</code>\n"
    "Ключа нет — попросите у того, кто дал вам этого бота.\n\n"
    "Ваш номер для запроса доступа: <code>{}</code>"
)
ПРИНЯТО = (
    "Ключ принят, доступ открыт (роль: {роль}).\n\n"
    "Ключ погашен: второй раз по нему не войти. Начните с <b>/помощь</b>, "
    "а веб-интерфейс откроет команда <b>/вход</b>."
)


def ensure_user(conn: sqlite3.Connection, user_id: int, name: str = "") -> None:
    """Завести или обновить человека. Вызывается только после приглашения."""
    conn.execute(
        "INSERT INTO users(id, name, role, created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        (user_id, name, access.ЧИТАТЕЛЬ, store.now()),
    )
    conn.commit()


def add_topic(conn: sqlite3.Connection, user_id: int, raw: str, *,
               stop: str = "", threshold: Any = None) -> str:
    profile = topics.parse_profile(raw)
    if not profile:
        return "Нужны слова: <code>/добавить дроны, бпла</code>"
    стоп = topics.parse_words(stop)
    try:
        порог = float(str(threshold or "").strip().replace(",", "."))
    except ValueError:
        порог = 1.0
    title = profile[0][0]
    words = ", ".join(
        word if weight == 1.0 else "{}*{}".format(word, int(weight) if weight == int(weight) else weight)
        for word, weight in profile
    )
    conn.execute(
        "INSERT INTO topics(user_id, title, words, stopwords, threshold, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user_id, title, words, ", ".join(стоп), max(0.0, порог), store.now()),
    )
    conn.commit()
    answer = "Тема «{}» добавлена: {}".format(title, words)
    if стоп:
        answer += "; стоп-слова: {}".format(", ".join(стоп))
    return answer


def list_topics(conn: sqlite3.Connection, user_id: int) -> str:
    rows = conn.execute(
        "SELECT id, title, words, stopwords, threshold, enabled "
        "FROM topics WHERE user_id = ? ORDER BY id",
        (user_id,),
    ).fetchall()
    if not rows:
        return "Тем пока нет. <code>/добавить дроны, бпла</code>"
    lines = [
        "{}. <b>{}</b> — {}{}{}{}".format(
            row["id"], row["title"], row["words"],
            "; стоп: {}".format(row["stopwords"]) if row["stopwords"] else "",
            "; порог {}".format(row["threshold"]) if row["threshold"] and float(row["threshold"]) != 1 else "",
            "" if row["enabled"] else " (выключена)",
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


def подписки(conn: sqlite3.Connection, user_id: int) -> str:
    """На какие издания человек подписан целиком."""
    from . import sources  # noqa: PLC0415 — нужен только здесь

    subs = store.feed_subs_of(conn, user_id)
    if not subs:
        return ("Подписок на издания целиком нет. <code>/подписаться meduza</code> — "
                "всё из издания; <code>/подписаться всё</code> — из всех.")
    метки = {code: source.label for code, source in sources.registry(conn).items()}
    lines = []
    for line in subs:
        code = str(line["code"] or "")
        name = "все издания целиком" if not code else метки.get(code, code)
        lines.append("— {}".format(name))
    return "Подписки на издания целиком:\n" + "\n".join(lines)


def подписаться(conn: sqlite3.Connection, user_id: int, tail: str) -> str:
    """Оформить подписку на издание целиком."""
    from . import sources  # noqa: PLC0415 — нужен только здесь

    code = tail.strip().lower()
    if code in ("все", "всё", "all"):
        code = ""
    if code and code not in sources.registry(conn):
        return ("Такого издания нет: {}. Возможны: <code>{}</code>".format(
            html.escape(code), ", ".join(sorted(sources.registry(conn))[:8]))
        )
    if not store.follow_feed(conn, user_id, code):
        return "Вы уже на это подписаны."
    name = "все издания" if not code else sources.registry(conn)[code].label
    return ("Подписка оформлена: {}. Что появилось в базе раньше подписки, "
            "не придёт — только новое.".format(name))


def отписаться(conn: sqlite3.Connection, user_id: int, tail: str) -> str:
    code = tail.strip().lower()
    if code in ("все", "всё", "all"):
        code = ""
    if not store.unfollow_feed(conn, user_id, code):
        return "Такой подписки нет: <code>/подписки</code> покажет ваши."
    return "Подписка снята."


def лента(conn: sqlite3.Connection, user_id: int, tail: str) -> str:
    """Последние материалы издания прямо в ответ — предпросмотр перед подпиской."""
    from . import sources  # noqa: PLC0415 — нужен только здесь

    parts = tail.split()
    code = parts[0].lower() if parts else ""
    if not code or code not in sources.registry(conn):
        return ("Нужно издание: <code>/лента meduza 5</code>. Возможны: {}".format(
            ", ".join(sorted(sources.registry(conn))[:8])))
    count = 5
    if len(parts) > 1:
        try:
            count = max(1, min(int(parts[1]), 10))
        except ValueError:
            count = 5
    rows = conn.execute(
        "SELECT title, url FROM items WHERE source = ? AND cold = 0 "
        "ORDER BY listed_at DESC LIMIT ?",
        (code, count),
    ).fetchall()
    if not rows:
        return "Материалов из этого издания в базе пока нет."
    lines = ["Последние из {}:".format(sources.registry(conn)[code].label)]
    lines.extend(
        "— <a href=\"{url}\">{title}</a>".format(
            url=row["url"], title=html.escape(row["title"] or "без заголовка")
        )
        for row in rows
    )
    return "\n".join(lines)


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


def принять_ключ(conn: sqlite3.Connection, user_id: int, name: str, tail: str) -> str:
    """Разбор «/ключ …» от незнакомого человека.

    Сам ключ в журнал не пишем ни в каком виде: строка из сообщения — это
    действующий секрет, а журналы читают и копируют шире, чем базу [NEWS-006].
    """
    if not access.попытка(user_id):
        log.warning("перебор ключа: %s исчерпал попытки", user_id)
        return ЗАКРЫТО.format(user_id)
    роль = access.принять(conn, (tail or "").strip(), user_id, name)
    if not роль:
        log.info("ключ не подошёл: %s", user_id)
        return ЗАКРЫТО.format(user_id)
    access.забыть_попытки(user_id)
    log.info("доступ открыт: %s, роль %s", user_id, роль)
    return ПРИНЯТО.format(роль=роль)


def пригласить(conn: sqlite3.Connection, user_id: int, tail: str) -> str:
    """«/пригласить Петя» — новый ключ. Только владельцу."""
    if not access.владелец(conn, user_id):
        return "Приглашения выдаёт только владелец."
    ключ, номер = access.выдать(conn, (tail or "").strip())
    return (
        "Приглашение №{номер} на {дней} дней.\n"
        "Передайте человеку эти две строки:\n\n"
        "<code>{ключ}</code>\n"
        "Отправьте боту: <code>/ключ {ключ}</code>\n\n"
        "Ключ показан один раз — в базе лежит только его отпечаток. "
        "Отозвать можно на странице «Доступы»."
    ).format(номер=номер, дней=access.DEFAULT_DAYS, ключ=ключ)


def доступы(conn: sqlite3.Connection, user_id: int) -> str:
    """Короткий список приглашений в бот: чтобы проверить с телефона."""
    if not access.владелец(conn, user_id):
        return "Список доступов виден только владельцу."
    строки = access.приглашения(conn)
    if not строки:
        return "Приглашений пока нет. <code>/пригласить Имя</code>"
    return "\n".join(
        "{}. {} — {}{}".format(
            row["номер"], row["кому"] or "без пометки", row["состояние"],
            " ({})".format(row["имя"]) if row["имя"] else "",
        )
        for row in строки[:20]
    )


def источник(conn: sqlite3.Connection, user_id: int, tail: str) -> str:
    """«/источник сайт» — найти ленту и подключить издание.

    Просить может любой приглашённый: найденная лента входит в общий
    каталог, но никому не приходит, пока человек не выбрал её сам
    [CORE-016]. Выключить или убрать издание — по-прежнему только владелец.
    """
    import html  # noqa: PLC0415

    from . import discover  # noqa: PLC0415 — импорт здесь разрывает круг

    сайт = (tail or "").strip()
    if not сайт:
        return "Укажите сайт: <code>/источник example.com</code> — или адрес ленты целиком."
    if not discover.попросить(сайт, user_id):
        return "Очередь поиска переполнена — подождите пару минут и попробуйте снова."
    # Ответ уходит с разметкой HTML: слово человека — данные, не разметка.
    return "Ищу ленту на {} — о результате напишу сюда.".format(html.escape(сайт))


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
    if not access.известен(conn, user_id):
        # Владелец мог ещё не появиться в базе: первый раз его назначает
        # переменная окружения, а не ключ — выдавать приглашение некому.
        access.бутстрап(conn)
    if not access.известен(conn, user_id):
        if command in ("ключ", "key"):
            return принять_ключ(conn, user_id, name, tail)
        return ЗАКРЫТО.format(user_id)
    ensure_user(conn, user_id, name)
    if command in ("старт", "start", "помощь", "help"):
        return HELP
    if command in ("ключ", "key"):
        return "Доступ у вас уже есть. Ключ больше не нужен."
    if command in ("пригласить", "invite"):
        return пригласить(conn, user_id, tail)
    if command in ("доступы", "access"):
        return доступы(conn, user_id)
    if command in ("источник", "feed"):
        return источник(conn, user_id, tail)
    if command in ("подписаться", "follow"):
        return подписаться(conn, user_id, tail)
    if command in ("отписаться", "unfollow"):
        return отписаться(conn, user_id, tail)
    if command in ("подписки", "subs"):
        return подписки(conn, user_id)
    if command == "лента":
        return лента(conn, user_id, tail)
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

    Кнопки меню (данные `m:…`) — отдельный путь: там модель не нужна,
    перерисовкой экрана занимается модуль меню.
    """
    chat = (query.get("message") or {}).get("chat") or {}
    data = str(query.get("data") or "")
    if data.startswith("m:"):
        from . import menu  # noqa: PLC0415 — импорт здесь разрывает круг

        await bot.ack(str(query.get("id") or ""))
        if chat.get("id"):
            сообщение = query.get("message") or {}
            try:
                await menu.press(bot, conn, int(chat["id"]), int(chat["id"]),
                                 int(сообщение.get("message_id") or 0), data)
            except Exception as exc:  # noqa: BLE001 — кнопка не роняет бота
                log.warning("кнопка меню «%s» сорвалась: %s", data, exc)
        return True
    if data.startswith("f:"):
        # «в тему / не в тему»: отзыв без модели, поэтому ответ мгновенный.
        parts = data.split(":")
        ok = False
        if chat.get("id") and len(parts) == 4:
            try:
                ok = store.topic_feedback_add(conn, int(parts[1]), int(parts[2]),
                                              parts[3], int(chat["id"]))
            except ValueError:
                ok = False
        await bot.ack(str(query.get("id") or ""), "Учтено" if ok else "Отзыв не записан")
        return ok
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
    from . import menu  # noqa: PLC0415 — импорт здесь разрывает круг

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
            # Сначала меню: /меню и открытые диалоги забирают сообщение
            # целиком, остальное уходит прежним командам.
            экран = menu.сообщение(
                conn, int(chat["id"]), str(message.get("text") or ""))
            if экран is None:
                reply, keyboard = answer(
                    conn,
                    int(chat["id"]),
                    str(user.get("first_name") or ""),
                    str(message.get("text") or ""),
                ), None
            else:
                reply, keyboard = экран
            await bot.send(int(chat["id"]), reply, preview=False, keyboard=keyboard)
            handled += 1
        rounds -= 1
        if rounds == 0:
            break
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return handled


__all__ = ("HELP", "ЗАКРЫТО", "add_topic", "answer", "drop_topic", "ensure_user",
           "latency_text", "list_topics", "login_link", "press", "serve",
           "доступы", "источник", "пригласить", "принять_ключ")
