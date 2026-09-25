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

from . import store

# Оформление: одна таблица переменных и её тёмное переопределение. Тема
# приходит из куки, поэтому переключатель работает без единой строчки
# JavaScript, а «как в системе» просто не ставит класс.
THEMES = ("светлая", "тёмная", "система")

STYLE = """
:root { color-scheme: light dark;
  --bg:#faf8f4; --panel:#fff; --ink:#1b1a17; --mut:#6f6a60; --line:#e3ddd2; --hair:#efeae0;
  --side:#15202b; --side-ink:#c6d0da; --side-mut:#75869a; --side-on:#1e2c3a;
  --acc:#a63d2f; --lnk:#2f6fd0; --ok:#2f7a3d; --bad:#a32020;
  --del-bg:#f8dedb; --del-ink:#8a1f1f; --ins-bg:#dfeedd; --ins-ink:#14532d; --bar:#d8d1c4 }
html.тёмная { color-scheme: dark;
  --bg:#12161c; --panel:#171d25; --ink:#dfe6ee; --mut:#8b97a6; --line:#242d38; --hair:#1e262f;
  --side:#0d1117; --side-ink:#aeb9c6; --side-mut:#6a7787; --side-on:#1a2430;
  --acc:#e2765f; --lnk:#6fa8ff; --ok:#4fb266; --bad:#e05c5c;
  --del-bg:#3d1d1d; --del-ink:#f0a5a5; --ins-bg:#16341f; --ins-ink:#8ee0a4; --bar:#2c3846 }
* { box-sizing: border-box }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.6 system-ui, -apple-system, sans-serif }
.каркас { display:grid; grid-template-columns:232px 1fr; min-height:100vh }
aside { background:var(--side); color:var(--side-ink); padding:18px 0 14px;
        display:flex; flex-direction:column }
.марка { font:600 20px/1.2 Georgia, serif; color:#fff; padding:0 18px 2px }
.марка small { display:block; font:italic 12px/1.5 Georgia, serif; color:var(--side-mut) }
aside h4 { margin:18px 18px 4px; font-size:10.5px; text-transform:uppercase;
           letter-spacing:.1em; color:var(--side-mut) }
aside a { display:block; padding:6px 18px; color:var(--side-ink); text-decoration:none;
          font-size:14px; border-left:3px solid transparent }
aside a.тут { background:var(--side-on); border-left-color:var(--acc); color:#fff;
              font-weight:600 }
.оформление { margin-top:auto; padding:12px 18px 0; border-top:1px solid #ffffff14 }
.оформление span { font-size:10.5px; text-transform:uppercase; letter-spacing:.1em;
                   color:var(--side-mut); display:block; margin-bottom:6px }
.оформление a { display:inline-block; padding:2px 8px; font-size:12px;
                border:1px solid #ffffff2b; border-radius:4px; margin:0 4px 4px 0;
                border-left-width:1px }
.оформление a.тут { background:#ffffff1a; color:#fff }
main { padding:22px 30px 40px; max-width:1180px }
h1 { font:400 25px/1.2 Georgia, serif; margin:0 0 14px }
h2 { font-size:11.5px; text-transform:uppercase; letter-spacing:.1em; color:var(--acc);
     margin:22px 0 10px; padding-bottom:5px; border-bottom:1px solid var(--line) }
a { color:var(--lnk) }
p { margin:.6rem 0 }
form.строка { display:flex; gap:.5rem; margin:.8rem 0; flex-wrap:wrap; align-items:center }
input[type=text], input[type=number], select { background:var(--panel); color:var(--ink);
     border:1px solid var(--line); border-radius:5px; padding:5px 8px; font:inherit;
     font-size:14px }
button { background:var(--acc); color:#fff; border:0; border-radius:5px; padding:6px 13px;
         font:inherit; font-size:13.5px; cursor:pointer }
button.тихо { background:var(--panel); color:var(--ink); border:1px solid var(--line) }
ul { padding-left:1.1rem } li { margin:.35rem 0 }
.тихо { color:var(--mut); font-size:13px }
.панель { background:var(--panel); border:1px solid var(--line); border-radius:7px;
          padding:12px 14px; margin:12px 0 }
table { border-collapse:collapse; width:100% }
td, th { text-align:left; padding:6px 8px; border-bottom:1px solid var(--hair);
         vertical-align:top }
th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--mut) }
del { background:var(--del-bg); color:var(--del-ink) }
ins { background:var(--ins-bg); color:var(--ins-ink); text-decoration:none }
.снято { color:var(--bad); font-weight:600 }
.метка { font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:var(--mut) }
code, .ровно { font-family:ui-monospace, Menlo, Consolas, monospace; font-size:12.5px }
"""

# Разделы бокового меню: адрес, название, группа.
MENU = (
    ("Наблюдение", (
        ("/", "Темы"),
        ("/новости", "Последние"),
        ("/поиск", "Поиск по архиву"),
        ("/правки", "Правки и снятия"),
        ("/задержки", "Задержки"),
    )),
    ("Настройка", (
        ("/источники", "Источники"),
        ("/телеграм", "Отдача в Telegram"),
        ("/запросы", "Сохранённые запросы"),
    )),
)

# Строк на странице выдачи: больше не читают, а одно ядро считает дольше.
PER_PAGE = 50


def theme_class(theme: str) -> str:
    """Класс корня по выбранной теме. «Система» — без класса [CORE-025]."""
    return "тёмная" if str(theme) == "тёмная" else ""


def sidebar(active: str, theme: str) -> str:
    """Боковое меню и переключатель оформления. Один и тот же на всех страницах."""
    blocks = []
    for group, links in MENU:
        blocks.append("<h4>{}</h4>".format(html.escape(group)))
        for href, title in links:
            here = " class=тут" if href == active else ""
            blocks.append('<a href="{}"{}>{}</a>'.format(href, here, html.escape(title)))
    switch = "".join(
        '<a href="/тема?вид={vid}&откуда={back}"{here}>{name}</a>'.format(
            vid=urllib.parse.quote(name),
            back=urllib.parse.quote(active or "/"),
            here=" class=тут" if name == theme else "",
            name=html.escape(name.capitalize()),
        )
        for name in THEMES
    )
    return (
        '<aside><div class=марка>FPNews<small>центр управления</small></div>{links}'
        '<div class=оформление><span>Оформление</span>{switch}</div></aside>'
    ).format(links="".join(blocks), switch=switch)


def page(title: str, body: str, theme: str = "система", active: str = "") -> str:
    """Страница целиком: каркас с меню, выбранная тема, содержимое."""
    return (
        '<!doctype html><html lang=ru class="{cls}"><head><meta charset=utf-8>'
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        "<title>{title}</title><style>{style}</style></head><body>"
        '<div class=каркас>{side}<main><h1>{title}</h1>{body}</main></div></body></html>'
    ).format(cls=theme_class(theme), title=html.escape(title), style=STYLE,
             side=sidebar(active, theme), body=body)


def login(theme: str = "система") -> str:
    return page(
        "Новости",
        "<p>Чтобы войти, напишите боту команду <b>/вход</b> — он пришлёт ссылку.</p>"
        "<p class=тихо>Ссылка живёт пять минут и работает один раз. "
        "Пароля нет: его нечему утечь.</p>",
        theme,
    )


def oops(text: str, theme: str = "система") -> str:
    return page("Не получилось", "<p>{}</p><p><a href=/>На главную</a></p>".format(
        html.escape(text)), theme)


def home(conn: Any, user_id: int, mark: str, theme: str = "система") -> str:
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
    return page("Мои темы", listing + form +
                "<p class=тихо>Слово ищется в любой форме: «дрон» найдёт «дроны» и "
                "«дронов». Фраза в кавычках — целиком.</p>" + exit_form,
                theme, "/")


def feed(conn: Any, user_id: int, limit: int = 30, theme: str = "система") -> str:
    rows = conn.execute(
        "SELECT i.title, i.url, i.source, d.sent_at, d.kind FROM deliveries d "
        "JOIN items i ON i.id = d.item_id WHERE d.user_id = ? "
        "ORDER BY d.id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    if not rows:
        return page("Последние", "<p class=тихо>Вам ещё ничего не приходило.</p>",
                    theme, "/новости")
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
    return page("Последние", "<ul>{}</ul>".format("".join(items)), theme, "/новости")


def latency(conn: Any, theme: str = "система") -> str:
    """Те же цифры, что в `/задержка` у бота: медиана, девяностый, максимум."""
    from .run import report  # noqa: PLC0415

    data = report(conn, 200)
    if not data.get("новостей"):
        return page("Задержки", "<p class=тихо>Мерить пока нечего.</p>", theme, "/задержки")
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
    return page("Задержки", table +
                "<p class=тихо>Секунды. Среднего здесь нет намеренно: его портит "
                "одна залипшая новость. Цифры одного дня — ещё не измерение.</p>",
                theme, "/задержки")


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


def search_page(conn: Any, query: dict[str, str], theme: str = "система") -> str:
    """Выдача поиска с формой и постраничностью.

    Без непустого запроса выборки нет вовсе: «показать всё» на одном ядре
    стоит дороже, чем пользы [CORE-025].
    """
    from . import search as search_module  # noqa: PLC0415 — разметка не тянет поиск всегда

    words = str(query.get("q") or "").strip()
    form = _search_form(query)
    if not words:
        return page("Поиск", form +
                    "<p class=тихо>Введите слово или несколько. Слово от четырёх букв "
                    "ищется вместе с окончаниями.</p>", theme, "/поиск")
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
        return page("Поиск", form +
                    "<p class=тихо>Ничего не найдено. Это не значит, что события не было: "
                    "в архиве лежит только то, что мы успели забрать.</p>", theme, "/поиск")
    listing = "<ul>{}</ul>".format("".join(_found_row(row) for row in rows))
    steps = []
    if number > 1:
        steps.append('<a href="{}">назад</a>'.format(_search_link(query, стр=number - 1)))
    if more:
        steps.append('<a href="{}">дальше</a>'.format(_search_link(query, стр=number + 1)))
    paging = "<p>{}</p>".format(" · ".join(steps)) if steps else ""
    return page("Поиск", form + listing + paging, theme, "/поиск")


def item_page(conn: Any, raw_id: Any, theme: str = "система") -> str | None:
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
    gone = ""
    if card.get("снято"):
        gone = (
            '<p class=снято>Снят с публикации — ответ {code}, замечено {when}.</p>'
            '<p class=тихо>Время указано по нашему наблюдению, а не по моменту снятия: '
            'между ними интервал перечитывания [NEWS-001].</p>'
        ).format(code=int(card.get("код_снятия") or 0), when=when(card.get("снято")))
    revisions = _revision_block(card)
    copies = _copies_block(card)
    if card["сюжет"]:
        plot = '<p><a href="/сюжет?id={}">Сюжет: {} изданий</a></p>'.format(
            int(card["id"]), len(card["сюжет"]["участники"]))
    else:
        plot = "<p class=тихо>Других изданий по этому событию мы не видели.</p>"
    return page("Материал", head + gone + lead + plot + revisions + copies, theme, "/поиск")


def _revision_block(card: dict[str, Any]) -> str:
    """История правок: заголовок «было → стало» и диф текста [NEWS-008]."""
    from . import diff as diff_module  # noqa: PLC0415 — разметка не тянет диф всегда

    revisions = card.get("правки") or []
    if not revisions:
        return "<h2>Правки</h2><p class=тихо>Правок не замечено.</p>"
    blocks = []
    before_title = str(card.get("заголовок") or "")
    before_text = ""
    for rev in revisions:
        title = str(rev.get("заголовок") or "")
        text = str(rev.get("текст") or "")
        head = when(rev.get("когда"))
        if title and before_title and title != before_title:
            head += " · заголовок: {}".format(diff_module.markup(before_title, title))
        body = ""
        if text and before_text:
            body = "<p>{}</p><p class=тихо>{}</p>".format(
                diff_module.markup(before_text, text),
                html.escape(diff_module.phrase(before_text, text)),
            )
        elif text and not before_text:
            body = "<p class=тихо>Первая сохранённая редакция, {} знаков.</p>".format(
                len(text))
        else:
            body = "<p class=тихо>Текст этой редакции не сохранён: правка старше шага 10.</p>"
        blocks.append('<div class=панель><div class=метка>{}</div>{}</div>'.format(head, body))
        before_title = title or before_title
        before_text = text or before_text
    return "<h2>Правки</h2>" + "".join(blocks)


def _copies_block(card: dict[str, Any]) -> str:
    """Список доказательных копий страницы: когда, отпечаток, размер."""
    copies = card.get("копии") or []
    if not copies:
        return ""
    rows = "".join(
        '<tr><td>{when}</td><td class=ровно>{hash}</td><td>{size} КБ</td>'
        '<td><a href="/копия?id={id}">открыть</a></td></tr>'.format(
            when=when(copy["когда"]),
            hash=html.escape(str(copy["отпечаток"])[:16]),
            size=round(int(copy["размер"]) / 1024, 1),
            id=int(copy["id"]),
        )
        for copy in copies
    )
    return (
        "<h2>Копии страницы</h2><table><tr><th>когда</th><th>sha256</th>"
        "<th>размер</th><th></th></tr>{}</table>"
        "<p class=тихо>Копия снята при разборе и при каждом перечитывании. Она и есть "
        "основание утверждать, что текст был именно таким [NEWS-007].</p>"
    ).format(rows)


def changes_page(conn: Any, theme: str = "система", limit: int = 50) -> str:
    """Журнал правок и снятий: что изменилось в городе за последние дни."""
    from . import diff as diff_module  # noqa: PLC0415

    снятые = store.gone(conn, limit)
    строки = []
    for row in снятые:
        строки.append(
            '<div class=панель><div class=метка><span class=снято>снято с публикации</span> · '
            '{source} · ответ {code} · замечено {when}</div>'
            '<p><a href="/материал?id={id}">{title}</a></p>'
            '<p class=тихо>Опубликовано {pub}. Копия страницы сохранена.</p></div>'.format(
                source=label(row["source"]), code=int(row["gone_code"] or 0),
                when=when(row["gone_at"]), id=int(row["id"]),
                title=html.escape(str(row["title"] or "без заголовка")),
                pub=when(row["published_at"] or row["listed_at"]),
            )
        )
    правки = conn.execute(
        "SELECT r.item_id, r.seen_at, r.title, r.text, i.source, i.title AS now_title "
        "FROM item_revisions r JOIN items i ON i.id = r.item_id "
        "ORDER BY r.id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    seen: set[int] = set()
    for rev in правки:
        item_id = int(rev["item_id"])
        if item_id in seen:
            continue
        seen.add(item_id)
        было = str(rev["title"] or "")
        стало = str(rev["now_title"] or "")
        head = "правка · {} · {}".format(label(rev["source"]), when(rev["seen_at"]))
        title = (diff_module.markup(было, стало) if было and стало and было != стало
                 else html.escape(стало or было or "без заголовка"))
        строки.append(
            '<div class=панель><div class=метка>{head}</div>'
            '<p><a href="/материал?id={id}">{title}</a></p></div>'.format(
                head=head, id=item_id, title=title)
        )
    if not строки:
        строки = ["<p class=тихо>Ни правок, ни снятий мы пока не видели. "
                  "Это не значит, что их не было [NEWS-001].</p>"]
    return page("Правки и снятия", "".join(строки), theme, "/правки")


def sources_page(conn: Any, mark: str, theme: str = "система") -> str:
    """Источники: что опрашиваем, как часто, что выключено."""
    from . import sources as sources_module  # noqa: PLC0415

    state = store.source_states(conn)
    rows = []
    for code, source in sorted(sources_module.BY_CODE.items()):
        current = state.get(code, {})
        enabled = current.get("включён", True)
        every = int(current.get("интервал") or 0)
        last = conn.execute(
            "SELECT COUNT(*) AS всего, MAX(listed_at) AS последний FROM items WHERE source = ?",
            (code,),
        ).fetchone()
        rows.append(
            "<tr><td>{name}</td><td>{count}</td><td>{last}</td>"
            "<td><form class=строка method=post action=\"/источники/интервал\">"
            '<input type=hidden name=метка value="{mark}">'
            '<input type=hidden name=код value="{code}">'
            '<input type=number name=секунд value="{every}" min=0 step=30 size=5>'
            "<button class=тихо>Сохранить</button></form></td>"
            "<td><form method=post action=\"/источники/переключить\">"
            '<input type=hidden name=метка value="{mark}">'
            '<input type=hidden name=код value="{code}">'
            "<button>{action}</button></form></td></tr>".format(
                name=label(code), count=int(last["всего"] or 0),
                last=when(last["последний"]), mark=mark, code=html.escape(code),
                every=every or int(source.interval),
                action="выключить" if enabled else "включить",
            )
        )
    table = (
        "<table><tr><th>издание</th><th>материалов</th><th>последний</th>"
        "<th>интервал, с</th><th></th></tr>{}</table>"
    ).format("".join(rows))
    return page("Источники", table +
                "<p class=тихо>Выключенное издание не опрашивается вовсе — это настройка "
                "сбора, а не фильтр выдачи. Интервал 0 означает «как задано в коде».</p>",
                theme, "/источники")


def telegram_page(conn: Any, user_id: int, mark: str, theme: str = "система") -> str:
    """Что уходит в бот: виды сообщений и тихие часы."""
    chosen = store.kinds_of(conn, user_id)
    row = conn.execute(
        "SELECT quiet_from, quiet_to FROM users WHERE id = ?", (int(user_id),)
    ).fetchone()
    boxes = "".join(
        '<label><input type=checkbox name=вид value="{kind}"{on}> {kind}</label> '.format(
            kind=html.escape(kind), on=" checked" if kind in chosen else "")
        for kind in store.KINDS
    )
    form = (
        '<form method=post action="/телеграм/сохранить">'
        '<input type=hidden name=метка value="{mark}">'
        "<div class=панель>{boxes}</div>"
        '<div class=панель>тихие часы с '
        '<input type=text name=с value="{since}" size=5 placeholder="23:00"> по '
        '<input type=text name=по value="{until}" size=5 placeholder="08:00"></div>'
        "<button>Сохранить</button></form>"
    ).format(mark=mark, boxes=boxes,
             since=html.escape(str(row["quiet_from"] if row else "") or "", quote=True),
             until=html.escape(str(row["quiet_to"] if row else "") or "", quote=True))
    return page("Отдача в Telegram", form +
                "<p class=тихо>Виды: «сырое» — первое сообщение по заголовку, «дополнение» — "
                "когда приехал текст, «изменение» — правка или снятие, «тоже_написали» — "
                "перепечатка, «запрос» — находка по сохранённому запросу.</p>",
                theme, "/телеграм")


def queries_page(conn: Any, user_id: int, mark: str, theme: str = "система") -> str:
    """Сохранённые запросы: подписка на вопрос, а не на тему."""
    saved = store.queries(conn, user_id)
    rows = []
    for query in saved:
        rows.append(
            "<tr><td><a href=\"/поиск?q={ссылка}\">{title}</a>"
            '<div class=тихо>{filters}</div></td>'
            "<td><form method=post action=\"/запросы/уведомления\">"
            '<input type=hidden name=метка value="{mark}">'
            '<input type=hidden name=номер value="{id}">'
            "<button class=тихо>{switch}</button></form></td>"
            "<td><form method=post action=\"/запросы/удалить\">"
            '<input type=hidden name=метка value="{mark}">'
            '<input type=hidden name=номер value="{id}">'
            "<button class=тихо>убрать</button></form></td></tr>".format(
                ссылка=urllib.parse.quote(str(query["query"])),
                title=html.escape(str(query["title"] or query["query"])),
                filters=" · ".join(filter(None, [
                    "издание: " + label(query["source"]) if query["source"] else "",
                    "только оригиналы" if query["only_original"] else "",
                    "только с правками" if query["only_revised"] else "",
                    "уведомлять в бот" if query["notify"] else "копить молча",
                ])),
                mark=mark, id=int(query["id"]),
                switch="не уведомлять" if query["notify"] else "уведомлять",
            )
        )
    table = ("<table><tr><th>запрос</th><th></th><th></th></tr>{}</table>".format("".join(rows))
             if rows else
             "<p class=тихо>Сохранённых запросов нет. Сохраните первый — например, «тариф».</p>")
    form = (
        '<form class=строка method=post action="/запросы/добавить">'
        '<input type=hidden name=метка value="{mark}">'
        '<input type=text name=запрос placeholder="тариф, подрядчик" required>'
        '<input type=text name=источник placeholder="издание, необязательно" size=14>'
        '<label><input type=checkbox name=оригиналы value=1> только оригиналы</label>'
        '<label><input type=checkbox name=уведомлять value=1 checked> уведомлять в бот</label>'
        "<button>Сохранить</button></form>"
    ).format(mark=mark)
    return page("Сохранённые запросы", table + form +
                "<p class=тихо>Подписка считает новым только то, что появилось после её "
                "сохранения: архив до этого момента — не новость [NEWS-004].</p>",
                theme, "/запросы")


def copy_page(conn: Any, raw_id: Any) -> str | None:
    """Сохранённая копия страницы как есть. `None` — такой копии нет."""
    try:
        snapshot_id = int(str(raw_id))
    except (TypeError, ValueError):
        return None
    return store.snapshot_page(conn, snapshot_id) or None


def story_page(conn: Any, raw_id: Any, theme: str = "система") -> str | None:
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
        "<p>Первым опубликовало <b>{}</b>.</p>".format(label(plot["первый"])) +
        table +
        "<p class=тихо>Отставание считается от первой публикации. Если издание не "
        "сообщило время, взят момент обнаружения — это отмечено в строке.</p>",
        theme,
    )


def link_message(url: str) -> str:
    """Сообщение бота со ссылкой на вход."""
    return ("Ссылка для входа (пять минут, один раз):\n{}\n\n"
            "Если открываете с другой машины — сначала проброс порта:\n"
            "<code>ssh -N -L 6769:127.0.0.1:6769 пользователь@сервер</code>").format(url)


__all__ = ("MENU", "PER_PAGE", "STYLE", "THEMES", "changes_page", "copy_page", "feed",
           "home", "item_page", "label", "lag", "latency", "link_message", "login", "oops",
           "page", "queries_page", "search_page", "sidebar", "sources_page", "story_page",
           "telegram_page", "theme_class", "when")
