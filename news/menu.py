"""Меню бота: всё управление кнопками, без синтаксиса команд.

Обычному человеку не нужно помнить команды и их аргументы. Экран меню —
это текст и ряды кнопок; нажатие перерисовывает то же самое сообщение,
поэтому чат не превращается в простыню. Данные нажатия телеграм
ограничивает шестьюдесятью четырьмя байтами, поэтому они короткие и
самодостаточные: экран и всё нужное для действия вшиты в кнопку, и
старые сообщения не ломаются после перезапуска службы.

Текст, который нельзя выбрать кнопкой — слова темы, адрес сайта, имя для
приглашения, — спрашиваем диалогом: ближайший ответ человека и есть ответ
на вопрос. Состояние диалога живёт в памяти процесса десять минут: этого
хватает, а перезапуск службы просто задаёт вопрос заново. Команды при
этом остаются: кто привык печатать, печатает по-прежнему [CORE-024].

Каталог изданий общий, а выбор из него — личный: читатель настраивает
только своё. Владельческие экраны проверяют роль ещё раз, а не полагаются
на то, что кнопку было не видно [CORE-016].
"""

from __future__ import annotations

import html
import logging
import sqlite3
import time
from typing import Any

from . import access, bridge, store, telegram
from . import sources  # noqa: PLC0415 — реестр нужен почти каждому экрану

log = logging.getLogger("fpnews.menu")

# Диалог живёт в памяти десять минут. Дольше хранить незачем: ответ на
# десятидневной вопрос — это уже не ответ, а неожиданность.
ЖИЗНЬ = 600.0
_диалоги: dict[int, tuple[str, str, float]] = {}

_ОПИСАНИЕ_РЕЖИМА = {
    "всё": "присылаю всё из этого издания",
    "тишина": "молчу: из издания не приходит ничего, даже по темам",
    "темы": "присылаю только то, что поймают ваши темы и запросы",
}
_МЕТКА_РЕЖИМА = {"всё": "всё", "тишина": "тишина", "темы": "по темам"}
_ТИХИЕ = (("нет", 0), ("23:00–08:00", 23), ("22:00–09:00", 22))
_ЗАДЕРЖКИ = (0, 10, 30, 60)
_ПОРОГИ = (("мягкий", 0.5), ("обычный", 1.0), ("строгий", 2.0))
_ЧАСЫ_СВОДКИ = ("07:00", "09:00", "19:00")


def _кнопки_назад(*куда: tuple[str, str]) -> list[list[tuple[str, str]]]:
    """Нижний ряд: путь назад. «Главное меню» — всегда, остальное — рядом."""
    ряд = list(куда)
    ряд.append(("⬅ Главное меню", "m:root"))
    return [ряд]


def _издания_рядом(conn: sqlite3.Connection, user_id: int
                   ) -> list[tuple[str, str, str]]:
    """Издания одним списком: код, название, состояние сбора."""
    каталог = sources.registry(conn)
    return sorted(
        ((код, источник.label, store.source_mode(conn, user_id, код))
         for код, источник in каталог.items()),
        key=lambda запись: запись[1].lower(),
    )


# --- Экраны -------------------------------------------------------------

def _главное(conn: sqlite3.Connection, user_id: int
             ) -> tuple[str, list[list[tuple[str, str]]]]:
    ряды = [
        [("📰 Мои издания", "m:izd"), ("🎯 Мои темы", "m:temy")],
        [("🌅 Сводка", "m:svod"), ("✉ Сообщения в бот", "m:otdacha")],
        [("🔎 Запросы", "m:zap"), ("➕ Найти издание", "m:nayti")],
        [("🌐 Веб-интерфейс", "m:vhod")],
    ]
    if access.владелец(conn, user_id):
        ряды += [
            [("⚙ Каталог изданий", "m:kat"), ("🔑 Доступы", "m:dost")],
            [("📊 Состояние", "m:sost"), ("🗄 Хранение", "m:khr")],
        ]
    return ("Главное меню. Нажимайте кнопки — печатать ничего не нужно.", ряды)


def _издания(conn: sqlite3.Connection, user_id: int
             ) -> tuple[str, list[list[tuple[str, str]]]]:
    записи = _издания_рядом(conn, user_id)
    все_сразу = any(row["code"] == "" for row in store.feed_subs_of(conn, user_id))
    строки = [
        "— <b>{}</b>: {}".format(html.escape(label), _ОПИСАНИЕ_РЕЖИМА[режим])
        for _, label, режим in записи
    ]
    текст = ("Что я приношу из каждого издания. Кнопка открывает издание, "
             "там — три состояния.\n\n" + "\n".join(строки))
    if все_сразу:
        текст += "\n\nВключено «все издания сразу»."
    ряды = [[("{} · {}".format(label, _МЕТКА_РЕЖИМА[режим]),
              "m:izd:{}".format(код))
             for код, label, режим in записи[i:i + 2]]
            for i in range(0, len(записи), 2)]
    ряды.append([("Все издания сразу", "m:vse")])
    ряды += _кнопки_назад()
    return текст, ряды


def _издание(conn: sqlite3.Connection, user_id: int, код: str
             ) -> tuple[str, list[list[tuple[str, str]]]]:
    источник = sources.registry(conn).get(код)
    if источник is None:
        return "Такого издания в каталоге нет — возможно, его удалили.", []
    режим = store.source_mode(conn, user_id, код)
    текст = "<b>{}</b>\nСейчас: {}.\n\nКак поступить с этим изданием?".format(
        html.escape(источник.label), _ОПИСАНИЕ_РЕЖИМА[режим])
    ряды = [
        [("Всё из издания", "m:mode:{}:всё".format(код))],
        [("Тишина", "m:mode:{}:тишина".format(код))],
        [("Только по темам", "m:mode:{}:темы".format(код))],
    ]
    ряды += _кнопки_назад(("⬅ Издания", "m:izd"))
    return текст, ряды


def _все_сразу(conn: sqlite3.Connection, user_id: int
               ) -> tuple[str, list[list[tuple[str, str]]]]:
    включено = any(row["code"] == "" for row in store.feed_subs_of(conn, user_id))
    текст = ("«Все издания сразу» — подписка целиком на каждое издание каталога. "
             "Отдельные подписки при этом остаются своими.")
    if включено:
        текст += "\n\nСейчас: включено."
        ряды = [[("Снять «все сразу»", "m:vse:0")]]
    else:
        текст += "\n\nСейчас: выключено."
        ряды = [[("Взять все издания", "m:vse:1")]]
    ряды += _кнопки_назад(("⬅ Издания", "m:izd"))
    return текст, ряды


def _темы(conn: sqlite3.Connection, user_id: int
          ) -> tuple[str, list[list[tuple[str, str]]]]:
    темы = store.topics_of(conn, user_id)
    if not темы:
        текст = ("Тем пока нет. Тема — это слова, на которые я обращаю внимание: "
                 "«дрон» найдёт и «дроны», и «дронов».")
    else:
        текст = "Ваши темы. Кнопка открывает настройки темы.\n\n" + "\n".join(
            "— <b>{}</b>: {}{}".format(
                html.escape(тема["title"]), тема["words"],
                " (выключена)" if not тема["enabled"] else "")
            for тема in темы)
    ряды = []
    for тема in темы:
        ряды.append([("{}. {}".format(тема["id"], тема["title"]),
                      "m:tema:{}".format(тема["id"]))])
    ряды.append([("➕ Новая тема", "m:tnovy")])
    ряды += _кнопки_назад()
    return текст, ряды


def _тема(conn: sqlite3.Connection, user_id: int, номер: str
          ) -> tuple[str, list[list[tuple[str, str]]]]:
    своя = next((т for т in store.topics_of(conn, user_id)
                 if т["id"] == int(номер)), None)
    if своя is None:
        return "Такой темы у вас нет.", []
    текст = "\n".join(filter(None, [
        "<b>{}</b>".format(html.escape(своя["title"])),
        "Слова: {}".format(своя["words"]),
        "Стоп-слова: {}".format(своя["stopwords"] or "нет"),
        "Порог: {}".format(своя["threshold"]),
        "" if своя["enabled"] else "Отдача выключена.",
    ]))
    ряды = [
        [("➕ Слова", "m:tslova:{}".format(номер)),
         ("Стоп-слова", "m:tstop:{}".format(номер))],
    ]
    порог = float(своя["threshold"] or 1)
    ряды.append([
        ("{}{}".format(название, " ✓" if значение == порог else ""),
         "m:tporog:{}:{}".format(номер, значение))
        for название, значение in _ПОРОГИ
    ])
    ряды.append([
        ("Выключить" if своя["enabled"] else "Включить", "m:tvkl:{}".format(номер)),
        ("Удалить", "m:tudal:{}".format(номер)),
    ])
    ряды += _кнопки_назад(("⬅ Мои темы", "m:temy"))
    return текст, ряды


def _подтверждение_темы(conn: sqlite3.Connection, user_id: int, номер: str
                        ) -> tuple[str, list[list[tuple[str, str]]]]:
    своя = next((т for т in store.topics_of(conn, user_id)
                 if т["id"] == int(номер)), None)
    if своя is None:
        return "Такой темы у вас нет.", []
    текст = ("Удалить тему «{}»? Восстановить нельзя — слова придётся "
             "набрать заново.".format(html.escape(своя["title"])))
    ряды = [[("Удалить", "m:tudal:{}:1".format(номер)),
             ("Отмена", "m:tema:{}".format(номер))]]
    return текст, ряды


def _сводка(conn: sqlite3.Connection, user_id: int
            ) -> tuple[str, list[list[tuple[str, str]]]]:
    строка = conn.execute("SELECT digest_at FROM users WHERE id = ?",
                          (int(user_id),)).fetchone()
    когда = (строка["digest_at"] or "").strip() if строка else ""
    текст = ("Сводка — письмо раз в день: что произошло по вашим темам и в "
             "изданиях, которые вы читаете.\n\nСейчас: {}.".format(
                 "каждый день в {}".format(когда) if когда else "выключена"))
    ряды = [[("Показать сейчас", "m:svodnow")]]
    ряды.append([(час, "m:svodcas:{}".format(час)) for час in _ЧАСЫ_СВОДКИ]
                + [("Выключить", "m:svodoff")])
    ряды += _кнопки_назад()
    return текст, ряды


def _отдача(conn: sqlite3.Connection, user_id: int
            ) -> tuple[str, list[list[tuple[str, str]]]]:
    выбранные = store.kinds_of(conn, user_id)
    строка = conn.execute("SELECT quiet_from, quiet_to FROM users WHERE id = ?",
                          (int(user_id),)).fetchone()
    с = (строка["quiet_from"] or "").strip() if строка else ""
    по = (строка["quiet_to"] or "").strip() if строка else ""
    тишина = "{}–{}".format(с, по) if с and по else "нет"
    задержка = store.delay_of(conn, user_id)
    текст = "\n".join([
        "<b>Виды сообщений</b>",
        "сырое — первое сообщение по заголовку; дополнение — когда приехал текст;",
        "изменение — правка или снятие; тоже_написали — перепечатка;",
        "запрос — находка по сохранённому запросу; лента — по подписке на издание.",
        "",
        "<b>Тихие часы:</b> {}. <b>Задержка отдачи:</b> {} мин (ноль — сразу).".format(
            тишина, задержка),
    ])
    ряды = []
    виды = list(store.KINDS)
    ряды += [[("{}{}".format(вид, " ✓" if вид in выбранные else ""),
               "m:vid:{}".format(вид))
              for вид in виды[i:i + 2]] for i in range(0, len(виды), 2)]
    ряды.append([("{}{}".format(название, " ✓" if тишина == название else ""),
                  "m:tiho:{}".format(номер)) for название, номер in _ТИХИЕ])
    ряды.append([("{} мин{}".format(мин, " ✓" if мин == задержка else ""),
                  "m:zader:{}".format(мин)) for мин in _ЗАДЕРЖКИ])
    ряды += _кнопки_назад()
    return текст, ряды


def _запросы(conn: sqlite3.Connection, user_id: int
             ) -> tuple[str, list[list[tuple[str, str]]]]:
    сохранённые = store.queries(conn, user_id)
    текст = ("Сохранённый запрос — подписка на вопрос, а не на тему: уведомляю, "
             "когда в выбранных изданиях появляется новое по этим словам.")
    if сохранённые:
        текст += "\n\n" + "\n".join(
            "— «{}»: {}".format(
                html.escape(строка["title"] or строка["query"]),
                "уведомлять" if строка["notify"] else "копить молча")
            for строка in сохранённые)
    ряды = []
    for строка in сохранённые:
        номер = int(строка["id"])
        ряды.append([
            ("{}: {}".format(номер, "молчит" if строка["notify"] else "уведомлять"),
             "m:znot:{}".format(номер)),
            ("Убрать", "m:zdel:{}".format(номер)),
        ])
    ряды.append([("➕ Новый запрос", "m:znew")])
    ряды += _кнопки_назад()
    return текст, ряды


def _каталог(conn: sqlite3.Connection, user_id: int
             ) -> tuple[str, list[list[tuple[str, str]]]]:
    текст = ("Каталог общий: издания видны всем, а выбор из них — личный. "
             "Сбор и интервалы настраивает владелец.")
    ряды = []
    строки = []
    каталог = sources.registry(conn)
    for код, источник in sorted(каталог.items(), key=lambda пара: пара[1].label.lower()):
        идёт = store.source_enabled(conn, код)
        люди = len(store.feed_subscribers(conn, код))
        строки.append("— <b>{}</b>: сбор {}, {}; подписчиков: {}".format(
            html.escape(источник.label),
            "идёт" if идёт else "стоит",
            store.source_every(conn, код) // 60,
            люди))
        ряды.append([("{} · {}".format(источник.label, "идёт" if идёт else "стоит"),
                      "m:kizd:{}".format(код))])
    if строки:
        текст += "\n\n" + "\n".join(строки)
    ряды += _кнопки_назад()
    return текст, ряды


def _издание_каталога(conn: sqlite3.Connection, user_id: int, код: str
                      ) -> tuple[str, list[list[tuple[str, str]]]]:
    источник = sources.registry(conn).get(код)
    if источник is None:
        return "Такого издания в каталоге нет.", []
    идёт = store.source_enabled(conn, код)
    люди = store.feed_subscribers(conn, код)
    текст = "\n".join([
        "<b>{}</b>".format(html.escape(источник.label)),
        "Сбор: {}. Интервал: {} мин. Подписчиков: {} чел.".format(
            "идёт" if идёт else "стоит", store.source_every(conn, код) // 60,
            len(люди)),
    ])
    ряды = [
        [("Остановить сбор" if идёт else "Включить сбор", "m:kreg:{}".format(код))],
        [("Интервал", "m:kint:{}".format(код))],
        [("Удалить издание", "m:kdel:{}".format(код))],
    ]
    ряды += _кнопки_назад(("⬅ Каталог", "m:kat"))
    return текст, ряды


def _интервал(conn: sqlite3.Connection, user_id: int, код: str
              ) -> tuple[str, list[list[tuple[str, str]]]]:
    источник = sources.registry(conn).get(код)
    if источник is None:
        return "Такого издания в каталоге нет.", []
    сейчас = store.source_every(conn, код) // 60
    текст = "Как часто опрашивать «{}»? Сейчас: {} мин.".format(
        html.escape(источник.label), сейчас)
    ряды = [[("{} мин{}".format(мин, " ✓" if мин == сейчас else ""),
              "m:ksek:{}:{}".format(код, мин * 60)) for мин in (5, 15, 30, 60)]]
    ряды += _кнопки_назад(("⬅ Издание", "m:kizd:{}".format(код)))
    return текст, ряды


def _доступы(conn: sqlite3.Connection, user_id: int
             ) -> tuple[str, list[list[tuple[str, str]]]]:
    список = access.приглашения(conn)
    текст = ("Приглашение — это ключ: передайте его человеку, он пришлёт боту "
             "«/ключ …» и войдёт. Ключ показывается один раз, в базе лежит "
             "только его отпечаток.")
    if список:
        текст += "\n\n" + "\n".join(
            "{}. {} — {}{}".format(
                строка["номер"], строка["кому"] or "без пометки",
                строка["состояние"],
                " ({})".format(строка["имя"]) if строка["имя"] else "")
            for строка in список[:10])
    ряды = [[("Выдать читателю", "m:dvyd:читатель"),
             ("Выдать владельцу", "m:dvyd:владелец")]]
    ряды += [[("Отозвать №{}".format(строка["номер"]),
               "m:dotz:{}".format(строка["номер"]))
              for строка in список if строка["состояние"] == "ждёт"]][:5]
    ряды += _кнопки_назад()
    return текст, ряды


def _состояние(conn: sqlite3.Connection, user_id: int
               ) -> tuple[str, list[list[tuple[str, str]]]]:
    from . import run as run_module  # noqa: PLC0415 — тяжёлый модуль, нужен тут

    цифры = store.summary(conn)
    объём = store.sizes(conn)
    замер = run_module.report(conn, 200)
    текст = "\n".join([
        "Материалов в базе: {}; за сутки — {}. Правок за сутки: {}; снято: {}.".format(
            цифры["материалов"], цифры["за_сутки"], цифры["правок"], цифры["снято"]),
        "База: {} КБ; копии страниц: {} КБ.".format(
            объём["база_кб"], объём["копии_кб"]),
        "",
        _замер_строкой(замер),
    ])
    ряды = [
        [("Опросить сейчас", "m:opros"), ("Проверка связи", "m:proverka")],
    ]
    ряды += _кнопки_назад()
    return текст, ряды


def _замер_строкой(замер: dict[str, Any]) -> str:
    """Задержки словами: медиана и девяностый процентиль, а не среднее."""
    if not замер.get("новостей"):
        return "Пока нечего мерить: новостей после запуска не было."
    имена = {"редакционная": "издание → лента",
             "до_отправки": "лента → сообщение",
             "до_полного": "сообщение → дополнение"}
    строки = ["Задержки по последним {} новостям, секунды:".format(замер["новостей"])]
    for ключ, название in имена.items():
        ячейка = замер.get(ключ)
        if not ячейка:
            continue
        строки.append("{}: медиана {}, девяностый {}".format(
            название, ячейка["медиана"], ячейка["девяностый"]))
    return "\n".join(строки)


def _хранение(conn: sqlite3.Connection, user_id: int
              ) -> tuple[str, list[list[tuple[str, str]]]]:
    объём = store.sizes(conn)
    текст = ("База: {} КБ. Копии страниц: {} КБ, упакованы из {} КБ.\n\n"
             "Выброс старых копий — сразу и навсегда. Сжатие возвращает "
             "место на диске, но занимает время.".format(
                 объём["база_кб"], объём["копии_кб"], объём["копии_исходно_кб"]))
    ряды = [[("Копии старше 30 дней", "m:kopii:30"),
             ("старше 90 дней", "m:kopii:90")],
            [("Сжать базу", "m:szhat")]]
    ряды += _кнопки_назад()
    return текст, ряды


_ЭКРАНЫ = {
    "root": _главное,
    "izd": lambda conn, user_id: _издания(conn, user_id),
    "vse": lambda conn, user_id: _все_сразу(conn, user_id),
    "temy": lambda conn, user_id: _темы(conn, user_id),
    "svod": lambda conn, user_id: _сводка(conn, user_id),
    "otdacha": lambda conn, user_id: _отдача(conn, user_id),
    "zap": lambda conn, user_id: _запросы(conn, user_id),
    "kat": lambda conn, user_id: _каталог(conn, user_id),
    "dost": lambda conn, user_id: _доступы(conn, user_id),
    "sost": lambda conn, user_id: _состояние(conn, user_id),
    "khr": lambda conn, user_id: _хранение(conn, user_id),
}


def _экран(conn: sqlite3.Connection, user_id: int, имя: str, аргумент: str
           ) -> tuple[str, list[list[tuple[str, str]]]]:
    """Нарисовать экран. Экраны с аргументом расписаны отдельно."""
    if имя == "izd" and аргумент:
        return _издание(conn, user_id, аргумент)
    if имя == "tema":
        return _тема(conn, user_id, аргумент)
    if имя == "tudal":
        return _подтверждение_темы(conn, user_id, аргумент)
    if имя == "kizd":
        return _издание_каталога(conn, user_id, аргумент)
    if имя == "kint":
        return _интервал(conn, user_id, аргумент)
    рисовалка = _ЭКРАНЫ.get(имя)
    if рисовалка is None:
        return _главное(conn, user_id)
    return рисовалка(conn, user_id)


# --- Действия ------------------------------------------------------------

def _спросить(user_id: int, шаг: str, данные: str, вопрос: str
              ) -> tuple[str, str, str, str | None]:
    """Открыть диалог: следующий ответ человека — ответ на этот вопрос."""
    _диалоги[user_id] = (шаг, данные, time.monotonic() + ЖИЗНЬ)
    return вопрос, "вопрос", "", None


def _действие(conn: sqlite3.Connection, user_id: int, data: str
              ) -> tuple[str, str, str, str | None]:
    """Кнопка → действие. Возвращает (заметка, экран, аргумент, послать).

    Заметка — одна строка о результате; экран рисуется под ней. «Послать» —
    сообщение поверх меню (сводка, ссылка, ключ). Экран «вопрос» — открытый
    диалог: заметка спрашивает, клавиатура — только отмена.
    """
    части = data.split(":")
    хвост = части[1] if len(части) > 1 else ""
    арг = части[2] if len(части) > 2 else ""

    if хвост == "mode":
        код = арг
        режим = части[3] if len(части) > 3 else ""
        if store.set_source_mode(conn, user_id, код, режим, sources.registry(conn)):
            return "Принято.", "izd", код, None
        return "Такого издания нет.", "izd", "", None
    if хвост == "vse":
        if арг == "1":
            store.follow_feed(conn, user_id, "")
            return "Подписка на все издания оформлена.", "vse", "", None
        store.unfollow_feed(conn, user_id, "")
        return "Подписка «все сразу» снята.", "vse", "", None
    if хвост == "tporog":
        store.topic_threshold_set(conn, int(арг), user_id, части[3])
        return "Порог обновлён.", "tema", арг, None
    if хвост == "tvkl":
        тема = next((т for т in store.topics_of(conn, user_id)
                     if т["id"] == int(арг)), None)
        if тема is None:
            return "Такой темы у вас нет.", "temy", "", None
        store.set_topic_delivery(conn, int(арг), user_id, enabled=not тема["enabled"])
        return "Отдача темы {}.".format(
            "выключена" if тема["enabled"] else "включена"), "tema", арг, None
    if хвост == "tnovy":
        return _спросить(user_id, "тема", "",
                         "Пришлите слова новой темы через запятую. Фраза ищется "
                         "целиком; слово с весом — «важное*3».")
    if хвост == "tslova":
        return _спросить(user_id, "тема_слова", арг,
                         "Пришлите слова, которые добавить к теме, через запятую.")
    if хвост == "tstop":
        return _спросить(user_id, "тема_стоп", арг,
                         "Пришлите стоп-слова через запятую: с ними тема не "
                         "срабатывает на лишнее.")
    if хвост == "tudal":
        if len(части) > 3 and части[3] == "1":
            from . import bot as bot_module  # noqa: PLC0415

            текст = bot_module.drop_topic(conn, user_id, арг)
            return текст, "temy", "", None
        return "Прежде чем удалять — проверьте.", "tudal", арг, None
    if хвост == "svodnow":
        from . import digest, web  # noqa: PLC0415 — как в bot.digest_text

        данные_сводки = digest.collect(conn, user_id)
        тело = ("За сутки мы не видели ни правок, ни снятий, и по вашим темам "
                "ничего не приходило. Всего материалов в базе за это время: {}."
                .format(данные_сводки["всего"]))
        if not digest.empty(данные_сводки):
            тело = digest.text(данные_сводки, web.base_url())
        return "Сводка — сообщением выше.", "svod", "", тело
    if хвост == "svodcas":
        # «09:00» содержит двоеточие — сам аргумент распадается на части.
        время = ":".join(части[2:])
        store.set_digest(conn, user_id, время)
        return "Буду присылать сводку каждый день в {}.".format(время), "svod", "", None
    if хвост == "svodoff":
        store.set_digest(conn, user_id, "")
        return "Ежедневная сводка выключена.", "svod", "", None
    if хвост == "vid":
        выбранные = store.kinds_of(conn, user_id)
        store.set_kinds(conn, user_id,
                       выбранные - {арг} if арг in выбранные else выбранные | {арг})
        return "Принято.", "otdacha", "", None
    if хвост == "tiho":
        for название, номер in _ТИХИЕ:
            if арг == str(номер):
                if номер:
                    store.set_quiet(conn, user_id,
                                    "23:00" if номер == 23 else "22:00",
                                    "08:00" if номер == 23 else "09:00")
                else:
                    store.set_quiet(conn, user_id, "", "")
        return "Принято.", "otdacha", "", None
    if хвост == "zader":
        store.set_delay(conn, user_id, арг)
        return "Задержка отдачи: {} мин.".format(int(арг)), "otdacha", "", None
    if хвост == "znot":
        for строка in store.queries(conn, user_id):
            if int(строка["id"]) == int(арг):
                store.toggle_notify(conn, int(арг), user_id)
                break
        return "Принято.", "zap", "", None
    if хвост == "zdel":
        store.drop_query(conn, int(арг), user_id)
        return "Запрос убран.", "zap", "", None
    if хвост == "znew":
        return _спросить(user_id, "запрос", "",
                         "Что искать? Пришлите слово или фразу — уведомлю о новых "
                         "находках.")
    if хвост == "nayti":
        return _спросить(user_id, "источник", "",
                         "Пришлите адрес сайта или ленты: example.com или "
                         "https://example.com/feed")
    if хвост == "otmena":
        _диалоги.pop(user_id, None)
        return "Отменено.", "root", "", None
    if хвост == "vhod":
        from . import bot as bot_module  # noqa: PLC0415

        return "Ссылка — сообщением выше.", "root", "", bot_module.login_link(conn, user_id)

    if хвост in ("root", "izd", "vse", "temy", "tema", "tudal",
                 "svod", "otdacha", "zap"):
        # Просто экраны: у izd, tema и tudal аргумент — номер или код.
        return "", хвост, арг, None

    # Дальше — экраны и действия владельца: кнопку мог нажать и читатель,
    # поэтому роль проверяем снова, а не полагаемся на то, что кнопки не было.
    if хвост in ("kat", "kizd", "kint", "kreg", "kdel", "ksek",
                 "dost", "dvyd", "dotz", "sost", "opros", "proverka",
                 "khr", "kopii", "szhat"):
        if not access.владелец(conn, user_id):
            return "Это доступно только владельцу.", "root", "", None
        if хвост == "kreg":
            # Подтверждение для издания с подписчиками рисует `_подтверждения`:
            # сюда нажатие доходит только с «точно» или без чужих людей.
            код = арг
            стал = not store.source_enabled(conn, код)
            store.set_source(conn, код, enabled=стал)
            return ("Сбор {}.".format("остановлен" if not стал else "запущен"),
                    "kizd", код, None)
        if хвост == "kdel":
            код = арг
            точно = len(части) > 3 and части[3] == "1"
            люди = store.feed_subscribers(conn, код)
            if люди and not точно:
                return "Сперва подтвердите.", "kdel_вопрос", код, None
            from . import discover  # noqa: PLC0415

            if store.drop_feed(conn, код):
                discover.погасить(код)
                return "Издание удалено из каталога.", "kat", "", None
            return "Такого издания нет.", "kat", "", None
        if хвост == "ksek":
            # «m:ksek:код:600»: код — вторая часть, секунды — третья.
            store.set_source(conn, части[2], every=int(части[3]))
            return "Интервал обновлён.", "kint", части[2], None
        if хвост == "dvyd":
            return _спросить(user_id, "приглашение", арг,
                             "Как записать человека? Пришлите имя — оно видно "
                             "только вам на странице «Доступы».")
        if хвост == "dotz":
            return ("Приглашение отозвано." if access.отозвать(conn, арг)
                    else "Такого приглашения нет.", "dost", "", None)
        if хвост == "opros":
            скольких = bridge.попросить()
            return ("Разбуждено сторожей: {}. Ноль — в этом процессе сторожей "
                    "нет, опрос идёт только при живой службе.".format(скольких),
                    "sost", "", None)
        if хвост == "proverka":
            ok = bridge.проверить(user_id)
            return ("Проверка попросила бота написать сюда." if ok else
                    "Проверка уже идёт или очередь полна — подождите.", "sost", "", None)
        if хвост == "kopii":
            выброшено = store.drop_old_snapshots(conn, арг)
            return "Выброшено копий: {}.".format(выброшено), "khr", "", None
        if хвост == "szhat":
            освободило = store.compact(conn)
            return "Сжатие освободило {} КБ.".format(освободило), "khr", "", None
        # kat, kizd, kint, dost, sost, khr — просто экраны
        return "", хвост, арг, None
    return "", "root", "", None


def _подтверждения(conn: sqlite3.Connection, user_id: int, data: str
                   ) -> tuple[str, list[list[tuple[str, str]]]] | None:
    """Экраны-подтверждения: то, что нельзя сделать одним нажатием."""
    части = data.split(":")
    if части[1] == "kdel" and len(части) == 3:
        код = части[2]
        источник = sources.registry(conn).get(код)
        люди = store.feed_subscribers(conn, код)
        текст = ("На этом издании {} чел. Удаление отключает его для всех: "
                 "записи в архиве останутся, но новое приходить перестанет."
                 .format(len(люди)))
        if источник is not None:
            текст = "<b>{}</b>\n{}".format(html.escape(источник.label), текст)
        return текст, [[("Всё равно удалить", "m:kdel:{}:1".format(код)),
                        ("Отмена", "m:kizd:{}".format(код))]]
    if (части[1] == "kreg" and len(части) == 3
            and store.source_enabled(conn, части[2])
            and store.feed_subscribers(conn, части[2])):
        код = части[2]
        люди = store.feed_subscribers(conn, код)
        текст = ("Это издание получают {} чел. Выключить его можно, но "
                 "их выбор — тоже факт, и прятать его от вас мы не будем."
                 .format(len(люди)))
        ряды = [[("Всё равно остановить", "m:kreg:{}:1".format(код)),
                 ("Отмена", "m:kizd:{}".format(код))]]
        return текст, ряды
    return None


async def press(bot: Any, conn: sqlite3.Connection, user_id: int, chat_id: int,
                message_id: int, data: str) -> bool:
    """Нажатие кнопки меню: действие и перерисовка этого же сообщения."""
    if not access.известен(conn, user_id):
        return False
    вопрос = _подтверждения(conn, user_id, str(data or ""))
    if вопрос is not None:
        текст, ряды = вопрос
    else:
        заметка, экран, аргумент, послать = _действие(conn, user_id, str(data or ""))
        if экран == "вопрос":
            # Диалог, открытый после предупреждения, кнопок подтверждения
            # не требует: вопрос задаёт сама заметка.
            текст, ряды = заметка, [[("Отмена", "m:otmena")]]
        else:
            текст, ряды = _экран(conn, user_id, экран, аргумент)
            if заметка:
                текст = "{}\n\n{}".format(заметка, текст)
        if послать:
            await bot.send(chat_id, послать, preview=False)
    if not await bot.edit(chat_id, message_id, текст,
                          keyboard=telegram.inline(ряды)):
        await bot.send(chat_id, текст, preview=False, keyboard=telegram.inline(ряды))
    return True


def сообщение(conn: sqlite3.Connection, user_id: int, text: str
              ) -> tuple[str, list[list[tuple[str, str]]] | None] | None:
    """Ответ на обычное сообщение, если его забирает меню: /меню или диалог.

    None — сообщение уходит прежним путём, командам из `bot.answer`.
    """
    if not access.известен(conn, user_id):
        return None
    тело = (text or "").strip()
    if тело.startswith("/"):
        команда = тело[1:].split(" ")[0].lower().split("@")[0]
        if команда in ("меню", "menu"):
            _диалоги.pop(user_id, None)
            return _экран(conn, user_id, "root", "")
        if команда in ("отмена", "cancel") and user_id in _диалоги:
            _диалоги.pop(user_id, None)
            текст, ряды = _экран(conn, user_id, "root", "")
            return "Отменено.\n\n" + текст, ряды
        # Другая команда отменяет диалог и уходит прежним путём.
        _диалоги.pop(user_id, None)
        return None
    открытый = _диалоги.get(user_id)
    if открытый is None:
        return None
    _диалоги.pop(user_id, None)
    if time.monotonic() > открытый[2]:
        текст, ряды = _экран(conn, user_id, "root", "")
        return "Вопрос устарел — начните заново из /меню.\n\n" + текст, ряды
    шаг, данные = открытый[0], открытый[1]
    заметка, экран, аргумент = _ответ_диалога(conn, user_id, шаг, данные, тело)
    текст, ряды = _экран(conn, user_id, экран, аргумент)
    return "{}\n\n{}".format(заметка, текст), ряды


def _ответ_диалога(conn: sqlite3.Connection, user_id: int, шаг: str, данные: str,
                   тело: str) -> tuple[str, str, str]:
    """Ответ человека на вопрос меню. Возвращает (заметка, экран, аргумент)."""
    if шаг == "тема":
        from . import bot as bot_module  # noqa: PLC0415

        return bot_module.add_topic(conn, user_id, тело), "temy", ""
    if шаг == "тема_слова":
        store.topic_words_add(conn, int(данные), user_id, тело)
        return "Слова добавлены.", "tema", данные
    if шаг == "тема_стоп":
        store.topic_stop_add(conn, int(данные), user_id, тело)
        return "Стоп-слова записаны.", "tema", данные
    if шаг == "запрос":
        store.add_query(conn, user_id, тело, notify=True)
        return "Запрос «{}» сохранён: уведомлю о новых находках.".format(
            html.escape(тело[:60])), "zap", ""
    if шаг == "источник":
        from . import discover  # noqa: PLC0415

        ответ = "Ищу ленту на {} — о результате напишу сюда.".format(html.escape(тело))
        if not discover.попросить(тело, user_id):
            ответ = "Очередь поиска переполнена — попробуйте через пару минут."
        return ответ, "izd", ""
    if шаг == "приглашение":
        ключ, номер = access.выдать(conn, тело, роль=данные)
        return ("Приглашение №{} на {} дней.\n\n"
                "Передайте человеку эти две строки:\n\n"
                "<code>{}</code>\n"
                "Отправьте боту: <code>/ключ {}</code>\n\n"
                "Ключ показан один раз — в базе лежит только его отпечаток."
                .format(номер, access.DEFAULT_DAYS, ключ, ключ)), "dost", ""
    log.warning("неизвестный шаг диалога: %s", шаг)
    return "Не понимаю вопрос — начните заново из /меню.", "root", ""


__all__ = ("ЖИЗНЬ", "сообщение", "press")
