"""Разметка страниц: чистые функции, возвращающие строку.

Отдельно от сервера по той же причине, по какой `bot.answer` отделён от опроса
Telegram: так страницу можно проверить тестом, не поднимая сокета.

Ни JavaScript, ни шаблонизатора. Формы обычные, переходы обычные, стиль — три
десятка строк в `<style>`. Страница обязана открываться с телефона в метро,
поэтому вес всей разметки меньше одного изображения `[CORE-025]`.

Всё, что приходит со стороны (заголовки изданий, слова тем), экранируется
здесь и только здесь.
Страницы поиска и материала чужой текст целиком не показывают: только
заголовок, лид и ссылка на оригинал `[NEWS-007]`.
"""

from __future__ import annotations

import html
import urllib.parse
from typing import Any

STYLE = """
:root { color-scheme: light dark }
body { font: 16px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 46rem;
       padding: 1rem }
h1 { font-size: 1.3rem } h2 { font-size: 1.05rem; margin-top: 1.6rem }
nav a { margin-right: 1rem }
form.строка { display: flex; gap: .5rem; margin: .8rem 0 }
input[type=text] { flex: 1; padding: .5rem; font-size: 1rem }
button { padding: .5rem .9rem; font-size: 1rem; cursor: pointer }
ul { padding-left: 1.1rem } li { margin: .35rem 0 }
.тихо { opacity: .65; font-size: .9rem }
table { border-collapse: collapse; width: 100% }
td, th { text-align: left; padding: .35rem .5rem; border-bottom: 1px solid #8884 }
"""

# Строк на странице выдачи: больше не читают, а одно ядро считает дольше.
PER_PAGE = 50


def page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        "<title>{title}</title><style>{style}</style></head><body>"
        "<h1>{title}</h1>{body}</body></html>"
    ).format(title=html.escape(title), style=STYLE, body=body)


def login() -> str:
    return page(
        "Новости",
        "<p>Чтобы войти, напишите боту команду <b>/вход</b> — он пришлёт ссылку.</p>"
        "<p class=тихо>Ссылка живёт пять минут и работает один раз. "
        "Пароля нет: его нечему утечь.</p>",
    )


def oops(text: str) -> str:
    return page("Не получилось", "<p>{}</p><p><a href=/>На главную</a></p>".format(
        html.escape(text)))


def nav() -> str:
    return ('<nav><a href=/>Темы</a><a href=/новости>Последние</a>'
            '<a href="/поиск">Поиск</a>'
            '<a href="/задержки">Задержки</a></nav>')


def home(conn: Any, user_id: int, mark: str) -> str:
    rows = conn.execute(
        "SELECT id, title, words FROM topics WHERE user_id = ? ORDER BY id", (user_id,)
    ).fetchall()
    items = [
        "<li><b>{}</b> — {} "
        '<form class=строка method=post action="/темы/удалить" style="display:inline">'
        '<input type=hidden name=метка value="{}">'
        '<input type=hidden name=номер value="{}">'
        "<button>убрать</button></form></li>".format(
            html.escape(row["title"]), html.escape(row["words"]), mark, row["id"]
        )
        for row in rows
    ]
    listing = "<ul>{}</ul>".format("".join(items)) if items else (
        "<p class=тихо>Тем пока нет. Добавьте первую — например, «дроны, бпла».</p>")
    form = (
        '<form class=строка method=post action="/темы/добавить">'
        '<input type=hidden name=метка value="{}">'
        '<input type=text name=слова placeholder="дроны, бпла, беспилотник" required>'
        "<button>Добавить</button></form>"
    ).format(mark)
    exit_form = (
        '<form method=post action="/выход">'
        '<input type=hidden name=метка value="{}"><button>Выйти</button></form>'
    ).format(mark)
    return page("Мои темы", nav() + listing + form +
                "<p class=тихо>Слово ищется в любой форме: «дрон» найдёт «дроны» и "
                "«дронов». Фраза в кавычках — целиком.</p>" + exit_form)


def feed(conn: Any, user_id: int, limit: int = 30) -> str:
    rows = conn.execute(
        "SELECT i.title, i.url, i.source, d.sent_at, d.kind FROM deliveries d "
        "JOIN items i ON i.id = d.item_id WHERE d.user_id = ? "
        "ORDER BY d.id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    if not rows:
        return page("Последние", nav() + "<p class=тихо>Вам ещё ничего не приходило.</p>")
    items = [
        '<li><a href="{url}" rel="noreferrer">{title}</a><br>'
        '<span class=тихо>{source} · {when} · {kind}</span></li>'.format(
            url=html.escape(str(row["url"])),
            title=html.escape(str(row["title"] or "без заголовка")),
            source=label(row["source"]),
            when=str(row["sent_at"])[11:16],
            kind=html.escape(str(row["kind"])),
        )
        for row in rows
    ]
    return page("Последние", nav() + "<ul>{}</ul>".format("".join(items)))


def latency(conn: Any) -> str:
    """Те же цифры, что в `/задержка` у бота: медиана, девяностый, максимум."""
    from .run import report  # noqa: PLC0415

    data = report(conn, 200)
    if not data.get("новостей"):
        return page("Задержки", nav() + "<p class=тихо>Мерить пока нечего.</p>")
    names = {
        "редакционная": "издание → наша лента",
        "до_отправки": "лента → сообщение",
        "до_полного": "сообщение → дополнение",
    }
    rows = []
    for key, label_text in names.items():
        cell = data.get(key)
        if not cell:
            continue
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                label_text, cell["штук"], cell["медиана"], cell["девяностый"], cell["максимум"]
            )
        )
    table = (
        "<table><tr><th>участок</th><th>штук</th><th>медиана</th>"
        "<th>девяностый</th><th>максимум</th></tr>{}</table>"
    ).format("".join(rows))
    return page("Задержки", nav() + table +
                "<p class=тихо>Секунды. Среднего здесь нет намеренно: его портит "
                "одна залипшая новость. Цифры одного дня — ещё не измерение.</p>")


def label(code: Any) -> str:
    """Человеческое название издания по коду источника."""
    from .deliver import LABEL  # noqa: PLC0415 — названия изданий живут там

    return html.escape(LABEL.get(str(code), str(code or "—")))


def when(value: Any) -> str:
    """Время без секунд. Нет времени — прочерк, а не ноль [NEWS-001]."""
    text = str(value or "")
    return html.escape(text[:16].replace("T", " ")) if text else "—"


def lag(seconds: Any) -> str:
    """Отставание словами. Неизвестное — прочерк [NEWS-001]."""
    if seconds is None:
        return "—"
    minutes = int(float(seconds) // 60)
    if minutes < 1:
        return "меньше минуты"
    if minutes < 60:
        return "{} мин".format(minutes)
    return "{} ч {} мин".format(minutes // 60, minutes % 60)


def _search_link(query: dict[str, str], **changes: Any) -> str:
    """Адрес той же выдачи с изменёнными параметрами."""
    merged = {key: value for key, value in query.items() if str(value or "")}
    for key, value in changes.items():
        if str(value or ""):
            merged[key] = value
        else:
            merged.pop(key, None)
    return html.escape("/поиск?" + urllib.parse.urlencode(merged))


def _search_form(query: dict[str, str]) -> str:
    def value(name: str) -> str:
        return html.escape(str(query.get(name) or ""), quote=True)

    def checked(name: str) -> str:
        return " checked" if str(query.get(name) or "") == "1" else ""

    return (
        '<form class=строка method=get action="/поиск">'
        '<input type=text name=q value="{q}" placeholder="мост, набережная" required>'
        "<button>Найти</button></form>"
        '<form class=тихо method=get action="/поиск">'
        '<input type=hidden name=q value="{q}">'
        'издание <input name=источник value="{источник}" size=8> '
        'с <input name=с value="{с}" size=10 placeholder="2026-09-01"> '
        'по <input name=по value="{по}" size=10> '
        '<label><input type=checkbox name=оригиналы value=1{о}> только оригиналы</label> '
        '<label><input type=checkbox name=правки value=1{п}> только с правками</label> '
        "<button>Уточнить</button></form>"
    ).format(q=value("q"), источник=value("источник"), с=value("с"), по=value("по"),
             о=checked("оригиналы"), п=checked("правки"))


def _found_row(row: dict[str, Any]) -> str:
    marks = []
    if row["перепечатка_из"]:
        marks.append('<a href="/сюжет?id={}">сюжет</a>'.format(int(row["id"])))
    if row["правок"]:
        marks.append("правок: {}".format(int(row["правок"])))
    tail = (" · " + " · ".join(marks)) if marks else ""
    return (
        '<li><a href="/материал?id={id}">{title}</a><br>'
        '<span class=тихо>{source} · {when} · '
        '<a href="{url}" rel="noreferrer">оригинал</a>{tail}</span></li>'
    ).format(
        id=int(row["id"]),
        title=html.escape(str(row["заголовок"] or "без заголовка")),
        source=label(row["источник"]),
        when=when(row["опубликовано"] or row["замечено"]),
        url=html.escape(str(row["url"] or "")),
        tail=tail,
    )


def search_page(conn: Any, query: dict[str, str]) -> str:
    """Выдача поиска с формой и постраничностью.

    Без непустого запроса выборки нет вовсе: «показать всё» на одном ядре
    стоит дороже, чем пользы [CORE-025].
    """
    from . import search as search_module  # noqa: PLC0415 — разметка не тянет поиск всегда

    words = str(query.get("q") or "").strip()
    form = _search_form(query)
    if not words:
        return page("Поиск", nav() + form +
                    "<p class=тихо>Введите слово или несколько. Слово от четырёх букв "
                    "ищется вместе с окончаниями.</p>")
    try:
        number = max(1, int(str(query.get("стр") or 1)))
    except ValueError:
        number = 1
    topic = str(query.get("тема") or "").strip()
    rows = search_module.search(
        conn,
        words,
        source=str(query.get("источник") or "").strip(),
        since=str(query.get("с") or "").strip(),
        until=str(query.get("по") or "").strip(),
        topic_id=int(topic) if topic.isdigit() else None,
        only_original=str(query.get("оригиналы") or "") == "1",
        only_revised=str(query.get("правки") or "") == "1",
        limit=PER_PAGE + 1,
        offset=(number - 1) * PER_PAGE,
    )
    more = len(rows) > PER_PAGE
    rows = rows[:PER_PAGE]
    if not rows:
        return page("Поиск", nav() + form +
                    "<p class=тихо>Ничего не найдено. Это не значит, что события не было: "
                    "в архиве лежит только то, что мы успели забрать.</p>")
    listing = "<ul>{}</ul>".format("".join(_found_row(row) for row in rows))
    steps = []
    if number > 1:
        steps.append('<a href="{}">назад</a>'.format(_search_link(query, стр=number - 1)))
    if more:
        steps.append('<a href="{}">дальше</a>'.format(_search_link(query, стр=number + 1)))
    paging = "<p>{}</p>".format(" · ".join(steps)) if steps else ""
    return page("Поиск", nav() + form + listing + paging)


def item_page(conn: Any, raw_id: Any) -> str | None:
    """Карточка материала. `None` — такого материала нет.

    Тело чужой статьи здесь не показывается: только лид и ссылка
    на источник [NEWS-007].
    """
    from . import search as search_module  # noqa: PLC0415

    try:
        item_id = int(str(raw_id))
    except (TypeError, ValueError):
        return None
    card = search_module.item(conn, item_id)
    if card is None:
        return None
    head = (
        "<p><b>{title}</b></p>"
        "<p class=тихо>{source} · опубликовано {published} · замечено {listed} · "
        'перечитано раз: {checks}</p>'
        '<p><a href="{url}" rel="noreferrer">Читать в оригинале</a></p>'
    ).format(
        title=html.escape(str(card["заголовок"] or "без заголовка")),
        source=label(card["источник"]),
        published=when(card["опубликовано"]),
        listed=when(card["замечено"]),
        checks=int(card["перечитаний"]),
        url=html.escape(str(card["url"] or "")),
    )
    lead = "<p>{}</p>".format(html.escape(str(card["лид"]))) if card["лид"] else ""
    if card["правки"]:
        rows = "".join(
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                when(rev["когда"]),
                html.escape(str(rev["заголовок"] or "—")),
                int(rev["длина"]),
            )
            for rev in card["правки"]
        )
        revisions = (
            "<h2>Правки</h2><table><tr><th>когда</th><th>заголовок</th>"
            "<th>длина</th></tr>{}</table>"
            "<p class=тихо>Тексты ревизий пока не хранятся, поэтому видно только "
            "время, заголовок и длину. Дифф — следующий шаг.</p>"
        ).format(rows)
    else:
        revisions = "<p class=тихо>Правок не замечено.</p>"
    if card["сюжет"]:
        plot = '<p><a href="/сюжет?id={}">Сюжет: {} изданий</a></p>'.format(
            int(card["id"]), len(card["сюжет"]["участники"]))
    else:
        plot = "<p class=тихо>Других изданий по этому событию мы не видели.</p>"
    return page("Материал", nav() + head + lead + plot + revisions)


def story_page(conn: Any, raw_id: Any) -> str | None:
    """Сюжет: кто первым и насколько отстали остальные."""
    from . import search as search_module  # noqa: PLC0415

    try:
        item_id = int(str(raw_id))
    except (TypeError, ValueError):
        return None
    plot = search_module.story(conn, item_id)
    if plot is None:
        return None
    rows = "".join(
        '<tr><td>{source}</td><td>{when}{guess}</td><td>{lag}</td>'
        '<td><a href="/материал?id={id}">{title}</a></td>'
        '<td><a href="{url}" rel="noreferrer">оригинал</a></td></tr>'.format(
            source=label(member["источник"]),
            when=when(member["когда"]),
            guess=" <span class=тихо>(по обнаружению)</span>" if member["по_обнаружению"] else "",
            lag=lag(member["отставание"]),
            id=int(member["id"]),
            title=html.escape(str(member["заголовок"] or "без заголовка")),
            url=html.escape(str(member["url"] or "")),
        )
        for member in plot["участники"]
    )
    table = (
        "<table><tr><th>издание</th><th>когда</th><th>отставание</th>"
        "<th>материал</th><th>источник</th></tr>{}</table>"
    ).format(rows)
    return page(
        "Сюжет",
        nav() + "<p>Первым опубликовало <b>{}</b>.</p>".format(label(plot["первый"])) +
        table +
        "<p class=тихо>Отставание считается от первой публикации. Если издание не "
        "сообщило время, взят момент обнаружения — это отмечено в строке.</p>",
    )


def link_message(url: str) -> str:
    """Сообщение бота со ссылкой на вход."""
    return ("Ссылка для входа (пять минут, один раз):\n{}\n\n"
            "Если открываете с другой машины — сначала проброс порта:\n"
            "<code>ssh -N -L 6769:127.0.0.1:6769 пользователь@сервер</code>").format(url)


__all__ = ("PER_PAGE", "STYLE", "feed", "home", "item_page", "label", "lag", "latency",
           "link_message", "login", "nav", "oops", "page", "search_page", "story_page",
           "when")
