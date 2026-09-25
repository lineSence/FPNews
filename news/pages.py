"""Разметка страниц: чистые функции, возвращающие строку.

Отдельно от сервера по той же причине, по какой `bot.answer` отделён от опроса
Telegram: так страницу можно проверить тестом, не поднимая сокета.

Ни JavaScript, ни шаблонизатора. Формы обычные, переходы обычные, стиль — три
десятка строк в `<style>`. Страница обязана открываться с телефона в метро,
поэтому вес всей разметки меньше одного изображения `[CORE-025]`.

Всё, что приходит со стороны (заголовки изданий, слова тем), экранируется
здесь и только здесь.
"""

from __future__ import annotations

import html
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
    from .deliver import LABEL  # noqa: PLC0415 — названия изданий живут там

    items = [
        '<li><a href="{url}" rel="noreferrer">{title}</a><br>'
        '<span class=тихо>{source} · {when} · {kind}</span></li>'.format(
            url=html.escape(str(row["url"])),
            title=html.escape(str(row["title"] or "без заголовка")),
            source=LABEL.get(str(row["source"]), str(row["source"])),
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
    for key, label in names.items():
        cell = data.get(key)
        if not cell:
            continue
        rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                label, cell["штук"], cell["медиана"], cell["девяностый"], cell["максимум"]
            )
        )
    table = (
        "<table><tr><th>участок</th><th>штук</th><th>медиана</th>"
        "<th>девяностый</th><th>максимум</th></tr>{}</table>"
    ).format("".join(rows))
    return page("Задержки", nav() + table +
                "<p class=тихо>Секунды. Среднего здесь нет намеренно: его портит "
                "одна залипшая новость. Цифры одного дня — ещё не измерение.</p>")


def link_message(url: str) -> str:
    """Сообщение бота со ссылкой на вход."""
    return ("Ссылка для входа (пять минут, один раз):\n{}\n\n"
            "Если открываете с другой машины — сначала проброс порта:\n"
            "<code>ssh -N -L 6769:127.0.0.1:6769 пользователь@сервер</code>").format(url)


__all__ = ("STYLE", "feed", "home", "latency", "link_message", "login", "nav", "oops", "page")
