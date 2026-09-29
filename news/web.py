"""Веб: маленький HTTP-сервер на том же событийном цикле.

Зачем свой, а не фреймворк. Нам нужны пять страниц без единой строчки
JavaScript. `aiohttp` или `starlette` с `uvicorn` — это 30–60 МБ памяти и
десятки мегабайт зависимостей на сервере, где свободно около пятисот
`[CORE-025]`. `http.server` из стандартной библиотеки не годится по другой
причине: он синхронный и заблокировал бы сторожей `[NEWS-002]`.

Здесь `asyncio.start_server`, разбор запроса руками и ответ строкой. Это
примерно двести строк, которые полностью понятны и ничего не тянут.

Про безопасность. Сервер слушает `127.0.0.1`: снаружи будет Caddy с
сертификатом. Вход — одноразовый код из бота на пять минут, он сгорает при
первом использовании. Решения «кому что можно», защитные заголовки и уборка
просроченных сессий живут в `news/guard.py` — здесь только маршруты.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from . import bridge, discover, guard, pages, store

log = logging.getLogger("fpnews.web")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6769
# Запрос без тела больше этого — не наш: формы здесь по сотне байт.
MAX_BODY = 64 * 1024
COOKIE = "fpnews"
# Тема живёт в отдельной куке: она не секрет и переживает выход из сессии.
# Имя куки латиницей — русские буквы в имени пришлось бы кодировать, а читать
# такой заголовок в `curl` стало бы невозможно [CORE-025].
THEME_COOKIE = "fpnews_theme"
SESSION_DAYS = 30
THEME_DAYS = 365


def host() -> str:
    return (os.getenv("FPNEWS_WEB_HOST") or DEFAULT_HOST).strip()


def port() -> int:
    try:
        return int(os.getenv("FPNEWS_WEB_PORT") or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT


def base_url() -> str:
    """Адрес, который бот присылает в сообщении со ссылкой на вход."""
    return (os.getenv("FPNEWS_WEB_URL") or "http://{}:{}".format(host(), port())).rstrip("/")


@dataclass
class Request:
    method: str = "GET"
    path: str = "/"
    query: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    form: dict[str, str] = field(default_factory=dict)
    # Список пар тела формы: галочки «вид» приходят по нескольку штук, а
    # словарь оставил бы только последнюю.
    pairs: list[tuple[str, str]] = field(default_factory=list)
    # То же для строки запроса: «издание=a&издание=b» в фильтрах ленты.
    asked: list[tuple[str, str]] = field(default_factory=list)

    def all_of(self, name: str) -> list[str]:
        """Все значения поля формы. Для наборов галочек без JavaScript."""
        return [value for key, value in self.pairs if key == name]

    def all_asked(self, name: str) -> list[str]:
        """Все значения поля строки запроса."""
        return [value for key, value in self.asked if key == name]

    @property
    def cookies(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for chunk in (self.headers.get("cookie") or "").split(";"):
            name, _, value = chunk.strip().partition("=")
            if name:
                out[name] = value
        return out


@dataclass
class Response:
    body: str = ""
    status: str = "200 OK"
    kind: str = "text/html; charset=utf-8"
    cookie: str = ""
    location: str = ""
    # Имя файла для выгрузки. Пусто — обычная страница.
    filename: str = ""

    def raw(self) -> bytes:
        data = self.body.encode("utf-8")
        head = [
            "HTTP/1.1 {}".format(self.status),
            "Content-Type: {}".format(self.kind),
            "Content-Length: {}".format(len(data)),
            "Connection: close",
        ]
        head += guard.заголовки()
        # Значения, собранные из запроса, проходят через `guard.чисто`:
        # перевод строки внутри `Location` разрезал бы ответ [CORE-016].
        if self.location:
            head.append("Location: {}".format(guard.чисто(self.location)))
        if self.cookie:
            head.append("Set-Cookie: {}".format(guard.чисто(self.cookie)))
        if self.filename:
            # Имя в кавычках и в UTF-8: без этого браузер сохранит «лента»
            # набором вопросительных знаков.
            head.append('Content-Disposition: attachment; filename="{}"; '
                        "filename*=UTF-8''{}".format(
                            "export", urllib.parse.quote(self.filename)))
        return ("\r\n".join(head) + "\r\n\r\n").encode("utf-8") + data


def redirect(where: str, cookie: str = "") -> Response:
    """После формы — перенаправление, иначе F5 повторит действие."""
    return Response(status="303 See Other", kind="text/plain; charset=utf-8",
                    cookie=cookie, location=where)


def parse(head: str, body: str) -> Request:
    """Разбор запроса. Всё непонятное — пустое, а не исключение."""
    lines = head.split("\r\n")
    parts = lines[0].split(" ") if lines else []
    method = parts[0].upper() if parts else "GET"
    target = parts[1] if len(parts) > 1 else "/"
    path, _, raw_query = target.partition("?")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name:
            headers[name.strip().lower()] = value.strip()
    pairs = urllib.parse.parse_qsl(body, keep_blank_values=True)
    asked = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
    return Request(
        method=method,
        path=urllib.parse.unquote(path),
        query=dict(asked),
        asked=list(asked),
        headers=headers,
        form=dict(pairs),
        pairs=list(pairs),
    )


def new_session(conn: Any, user_id: int) -> str:
    token = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at) "
        "VALUES(?,?,?, datetime('now', '+{} days'))".format(SESSION_DAYS),
        (token, int(user_id), store.now()),
    )
    conn.commit()
    return token


def whoami(conn: Any, request: Request) -> int:
    """id пользователя по куке. 0 — не вошёл."""
    token = request.cookies.get(COOKIE) or ""
    if not token:
        return 0
    row = conn.execute(
        "SELECT user_id FROM sessions WHERE token = ? AND expires_at > datetime('now')",
        (token,),
    ).fetchone()
    return int(row["user_id"]) if row is not None else 0


def code_for(conn: Any, user_id: int) -> str:
    """Одноразовый код входа на пять минут. Старые коды человека гасятся."""
    conn.execute("DELETE FROM login_codes WHERE user_id = ?", (int(user_id),))
    code = secrets.token_urlsafe(9)
    conn.execute(
        "INSERT INTO login_codes(code, user_id, expires_at) "
        "VALUES(?,?, datetime('now', '+5 minutes'))",
        (code, int(user_id)),
    )
    conn.commit()
    return code


def redeem(conn: Any, code: str) -> int:
    """Погасить код и вернуть id. 0 — нет такого, просрочен или уже использован."""
    row = conn.execute(
        "SELECT user_id FROM login_codes WHERE code = ? AND expires_at > datetime('now')",
        (code or "",),
    ).fetchone()
    if row is None:
        return 0
    conn.execute("DELETE FROM login_codes WHERE code = ?", (code,))
    conn.commit()
    return int(row["user_id"])


def cookie_value(token: str) -> str:
    secure = "; Secure" if guard.за_проксёй() else ""
    return "{}={}; Path=/; HttpOnly; SameSite=Lax; Max-Age={}{}".format(
        COOKIE, token, SESSION_DAYS * 86400, secure
    )


def theme_cookie(theme: str) -> str:
    """Кука темы. Значение кодируем: «тёмная» в заголовке — не ASCII."""
    return "{}={}; Path=/; SameSite=Lax; Max-Age={}".format(
        THEME_COOKIE, urllib.parse.quote(theme), THEME_DAYS * 86400
    )


def theme_of(request: Request) -> str:
    """Тема из куки. Незнакомое значение — «система», а не ошибка."""
    value = urllib.parse.unquote(request.cookies.get(THEME_COOKIE) or "")
    return value if value in pages.THEMES else "система"


def safe_back(where: str) -> str:
    """Куда вернуться после переключения темы.

    Принимаем только свой путь: «//зло.рф» и «https://зло.рф» браузер считает
    чужим адресом, и открытый редирект из настройки оформления — подарок для
    поддельной страницы входа [CORE-016].
    """
    where = guard.чисто(where) or "/"
    if not where.startswith("/") or where.startswith("//") or "\\" in where:
        return "/"
    return where


def csrf(token: str) -> str:
    """Метка формы — часть ключа сессии. Чужая вкладка её не знает."""
    import hashlib  # noqa: PLC0415

    return hashlib.blake2b((token or "").encode(), digest_size=8).hexdigest()


# Разделы, куда возвращаемся после формы: список закрыт, чтобы адрес из
# формы не превратился в редирект куда попало.
_SECTIONS = ("/доступы", "/запросы", "/источники", "/сводка", "/телеграм", "/темы",
             "/подписки", "/хранение")


def _number(raw: Any) -> int:
    """Число из формы. Мусор — ноль: чужой запрос не должен ронять страницу."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return 0


def route(conn: Any, request: Request) -> Response:
    """Вся маршрутизация. Чистая функция — поэтому и проверяется тестами."""
    from . import access  # noqa: PLC0415 — нужен только здесь
    from . import bot as bot_module  # noqa: PLC0415 — импорт здесь разрывает круг

    token = request.cookies.get(COOKIE) or ""
    user_id = whoami(conn, request)
    theme = theme_of(request)
    if request.path == "/вход":
        # Код гасим всегда, даже когда попыток уже слишком много: иначе
        # чужой перебор закрывал бы вход и настоящему человеку [CORE-017].
        entering = redeem(conn, request.query.get("код", ""))
        if entering:
            guard.забыть_входы()
            guard.убрать_просроченное(conn)
            return redirect("/", cookie_value(new_session(conn, entering)))
        сколько = guard.неудачный_вход()
        if сколько >= guard.ПОПЫТОК_ВХОДА:
            log.warning("неудачных входов в окне: %s", сколько)
            return Response(pages.oops(
                "Слишком много попыток входа. Попросите у бота свежий код "
                "и попробуйте через несколько минут.", theme),
                status="429 Too Many Requests")
        return Response(pages.login(theme), status="401 Unauthorized")
    if not user_id:
        return Response(pages.login(theme), status="401 Unauthorized")
    if guard.отозван(conn, user_id):
        # Приглашение отозвали, а кука в браузере осталась. Гасим сессию,
        # а не просто отказываем: иначе она проживёт свои тридцать дней.
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (int(user_id),))
        conn.commit()
        return Response(pages.login(theme), status="401 Unauthorized")
    хозяин = access.владелец(conn, user_id)
    if not guard.пускать(conn, user_id, request.path, request.method):
        return Response(pages.oops(guard.ОТКАЗ, theme), status="403 Forbidden")
    if request.method == "POST":
        if request.form.get("метка") != csrf(token):
            # Причина почти всегда одна: страницу открыли до входа или сессия
            # сменилась. Пишем в журнал, чтобы «непонятный 400» перестал быть
            # непонятным, и объясняем человеку, что делать.
            log.warning("метка формы не совпала: %s (полей в теле: %s)",
                        request.path, len(request.pairs))
            return Response(pages.oops(
                "Форма устарела: страница была открыта до входа или сессия сменилась. "
                "Обновите страницу и повторите.", theme), status="400 Bad Request")
        section = "/" + request.path.strip("/").split("/")[0]
        if request.path == "/темы/добавить":
            bot_module.add_topic(conn, user_id, request.form.get("слова", ""),
                                 stop=request.form.get("стоп", ""),
                                 threshold=request.form.get("порог", ""))
        elif request.path == "/темы/удалить":
            bot_module.drop_topic(conn, user_id, request.form.get("номер", ""))
        elif request.path == "/темы/стоп":
            store.topic_stop_add(conn, _number(request.form.get("номер")), user_id,
                                 request.form.get("стоп", ""))
        elif request.path == "/темы/порог":
            store.topic_threshold_set(conn, _number(request.form.get("номер")), user_id,
                                       request.form.get("порог", ""))
        elif request.path == "/темы/слово":
            store.topic_words_add(conn, _number(request.form.get("номер")), user_id,
                                  request.form.get("слово", ""))
        elif request.path == "/подписки/добавить":
            store.follow_feed(conn, user_id, request.form.get("код", ""))
        elif request.path == "/подписки/убрать":
            store.unfollow_feed(conn, user_id, request.form.get("код", ""))
        elif request.path == "/выход":
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return redirect("/", "{}=; Path=/; Max-Age=0".format(COOKIE))
        elif request.path == "/запросы/добавить":
            store.add_query(
                conn, user_id, (request.form.get("запрос", "") or "").strip(),
                source=(request.form.get("источник", "") or "").strip(),
                only_original=bool(request.form.get("оригиналы")),
                notify=bool(request.form.get("уведомлять")),
            )
        elif request.path == "/запросы/удалить":
            store.drop_query(conn, _number(request.form.get("номер")), user_id)
        elif request.path == "/запросы/уведомления":
            store.toggle_notify(conn, _number(request.form.get("номер")), user_id)
        elif request.path == "/хранение/копии":
            выброшено = store.drop_old_snapshots(conn, request.form.get("дней", ""))
            log.info("выброшено копий страниц: %s", выброшено)
        elif request.path == "/хранение/сжать":
            log.info("сжатие базы освободило %s КБ", store.compact(conn))
        elif request.path == "/опросить":
            # Веб только будит сторожей и сразу отвечает: ходить в чужие двери
            # прямо из обработчика страницы нельзя [NEWS-002].
            bridge.попросить()
        elif request.path == "/проверка":
            bridge.проверить(user_id)
        elif request.path == "/источники/переключить":
            code = request.form.get("код", "")
            store.set_source(conn, code, enabled=not store.source_enabled(conn, code))
        elif request.path == "/источники/интервал":
            store.set_source(conn, request.form.get("код", ""),
                             every=_number(request.form.get("секунд")))
        elif request.path == "/источники/добавить":
            # Поиск ленты ходит в сеть по чужим адресам и занимает секунды:
            # страница отвечает сразу, а ходит отдельный контур [NEWS-002].
            discover.попросить((request.form.get("сайт", "") or "").strip(), user_id)
        elif request.path == "/источники/удалить":
            code = (request.form.get("код", "") or "").strip()
            if store.drop_feed(conn, code):
                # Сторожа убранной ленты останавливаем сразу: без этого он
                # продолжал бы опрашивать дверь до перезапуска службы.
                discover.погасить(code)
        elif request.path == "/телеграм/сохранить":
            store.set_kinds(conn, user_id, request.all_of("вид"))
            store.set_quiet(conn, user_id, request.form.get("с", ""), request.form.get("по", ""))
            store.set_delay(conn, user_id, request.form.get("задержка", "0"))
            store.set_target(conn, user_id, request.form.get("адресат", ""))
        elif request.path == "/сводка/время":
            store.set_digest(conn, user_id, request.form.get("время", ""))
        elif request.path == "/телеграм/издания":
            store.set_user_sources(conn, user_id, request.all_of("издание"))
        elif request.path == "/доступы/выдать":
            ключ, номер = access.выдать(
                conn, request.form.get("кому", ""),
                роль=(request.form.get("роль", "") or access.ЧИТАТЕЛЬ))
            # Единственный ответ формы, который не перенаправление: ключ
            # существует только в этом ответе, в базе лежит лишь отпечаток.
            return Response(pages.invite_page(номер, ключ, theme))
        elif request.path == "/доступы/отозвать":
            access.отозвать(conn, request.form.get("номер", ""))
        elif request.path == "/телеграм/тема":
            store.set_topic_delivery(conn, _number(request.form.get("номер")), user_id,
                                     enabled=bool(request.form.get("отдавать")),
                                     sources=request.all_of("издание"))
        else:
            return Response(pages.oops("Такой формы нет.", theme), status="404 Not Found")
        return redirect(section if section in _SECTIONS else "/")
    if request.path == "/тема":
        want = request.query.get("вид", "")
        chosen = want if want in pages.THEMES else "система"
        return redirect(safe_back(request.query.get("откуда", "/")), theme_cookie(chosen))
    if request.path == "/":
        return Response(pages.home(conn, user_id, csrf(token), theme, хозяин))
    if request.path == "/доступы":
        return Response(pages.access_page(conn, csrf(token), theme))
    if request.path == "/темы":
        return Response(pages.topics_page(conn, user_id, csrf(token), theme))
    if request.path == "/подписки":
        return Response(pages.subs_page(conn, user_id, csrf(token), theme))
    if request.path == "/задержки":
        return Response(pages.latency(conn, theme))
    if request.path == "/лента":
        return Response(pages.stream_page(conn, user_id, request.query, theme,
                                          request.all_asked))
    if request.path == "/новости":
        return Response(pages.feed(conn, user_id, theme=theme))
    if request.path == "/поиск":
        return Response(pages.search_page(conn, request.query, theme))
    if request.path == "/выгрузка":
        from . import export  # noqa: PLC0415 — нужен только здесь

        вид = request.query.get("формат", "csv")
        тело, тип, имя = export.make(conn, request.query, вид, request.all_asked)
        return Response(тело, kind=тип, filename=имя)
    if request.path == "/хранение":
        return Response(pages.storage_page(conn, csrf(token), theme, хозяин))
    if request.path == "/диагностика":
        return Response(pages.diagnostics_page(theme))
    if request.path == "/замеры":
        return Response(pages.measures_page(conn, theme))
    if request.path == "/досье":
        from . import export  # noqa: PLC0415 — нужен только здесь

        тело, имя = export.dossier(conn, request.query, request.all_asked)
        if not тело:
            return Response(pages.oops("Не из чего собрать досье: нет ни сущности, "
                                       "ни слов запроса.", theme), status="404 Not Found")
        return Response(тело, kind="text/markdown; charset=utf-8", filename=имя)
    if request.path == "/состояние":
        return Response(pages.state_page(conn, theme))
    if request.path == "/сводка":
        return Response(pages.digest_page(conn, user_id, csrf(token), theme))
    if request.path == "/сущности":
        return Response(pages.entities_page(conn, request.query, theme))
    if request.path == "/всплески":
        return Response(pages.bursts_page(conn, theme))
    if request.path == "/сущность":
        карточка = pages.entity_page(conn, request.query.get("id", ""), theme)
        if карточка is None:
            return Response(pages.oops("Такой сущности нет.", theme), status="404 Not Found")
        return Response(карточка)
    if request.path == "/правки":
        return Response(pages.changes_page(conn, theme))
    if request.path == "/источники":
        return Response(pages.sources_page(conn, csrf(token), theme, хозяин, user_id))
    if request.path == "/телеграм":
        return Response(pages.telegram_page(conn, user_id, csrf(token), theme))
    if request.path == "/запросы":
        return Response(pages.queries_page(conn, user_id, csrf(token), theme))
    if request.path == "/копия":
        saved = pages.copy_page(conn, request.query.get("id", ""))
        if saved is None:
            return Response(pages.oops("Такой копии нет.", theme), status="404 Not Found")
        # Отдаём текстом, а не разметкой. Это чужой HTML со скриптами и
        # счётчиками; показать его как страницу — впустить чужой код в свой
        # источник и отдать ему куку сессии [CORE-016].
        return Response(saved, kind="text/plain; charset=utf-8")
    if request.path == "/материал":
        card = pages.item_page(conn, request.query.get("id", ""), theme)
        if card is None:
            return Response(pages.oops("Такого материала нет.", theme), status="404 Not Found")
        return Response(card)
    if request.path == "/сюжет":
        plot = pages.story_page(conn, request.query.get("id", ""), theme)
        if plot is None:
            return Response(pages.oops("Сюжета нет: других изданий мы не видели.", theme),
                            status="404 Not Found")
        return Response(plot)
    return Response(pages.oops("Такой страницы нет.", theme), status="404 Not Found")


async def read_body(reader: Any, head: str) -> str:
    """Тело запроса: по длине или кусками.

    Кусками (`Transfer-Encoding: chunked`) тело приходит, когда впереди
    стоит обратный прокси вроде Caddy. Раньше мы такое тело просто не читали,
    форма приезжала пустой, метка не совпадала — и человек видел «форма
    устарела» на каждой второй отправке [CORE-017].
    """
    length = 0
    chunked = False
    for line in head.split("\r\n"):
        name, _, value = line.partition(":")
        name = name.strip().lower()
        if name == "content-length":
            try:
                length = min(int(value.strip() or 0), MAX_BODY)
            except ValueError:
                length = 0
        elif name == "transfer-encoding" and "chunked" in value.lower():
            chunked = True
    if chunked:
        out: list[bytes] = []
        total = 0
        while True:
            line = (await reader.readline()).strip().split(b";")[0]
            try:
                size = int(line or b"0", 16)
            except ValueError:
                break
            if size <= 0:
                break
            total += size
            if total > MAX_BODY:
                break
            out.append(await reader.readexactly(size))
            await reader.readexactly(2)  # хвостовые \r\n куска
        return b"".join(out).decode("utf-8", "replace")
    if length:
        return (await reader.readexactly(length)).decode("utf-8", "replace")
    return ""


async def handle(conn: Any, reader: Any, writer: Any) -> None:
    request = None
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        head = raw.decode("utf-8", "replace").rstrip("\r\n")
        request = parse(head, await read_body(reader, head))
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        # Браузер открыл соединение про запас и закрыл, не спросив ничего.
        # Это не ошибка запроса, и отвечать здесь нечему.
        writer.close()
        return
    except (ValueError, asyncio.LimitOverrunError) as exc:
        log.warning("запрос не разобран: %s", exc)
        response = Response("Запрос не разобран.", status="400 Bad Request",
                            kind="text/plain; charset=utf-8")
        request = None
    if request is not None:
        try:
            response = route(conn, request)
        except Exception as exc:  # noqa: BLE001 — веб не роняет сбор новостей [CORE-017]
            # Раньше сбой страницы попадал в тот же `except`, что и разбор
            # запроса, и человек получал пустой 400 вместо объяснения. Теперь
            # ошибка страницы — это 500 и запись в журнал с адресом.
            log.warning("страница %s не отдалась: %s: %s", request.path,
                        type(exc).__name__, exc, exc_info=True)
            response = Response(pages.oops("Что-то сломалось. Подробности в журнале службы."),
                                status="500 Internal Server Error")
    try:
        writer.write(response.raw())
        await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


async def serve(conn: Any, stop: Any) -> None:
    """Слушает, пока не попросят остановиться. Отказ порта — не падение."""
    if (os.getenv("FPNEWS_WEB") or "1") != "1":
        return
    maker: Callable[..., Any] = lambda r, w: handle(conn, r, w)  # noqa: E731
    try:
        server = await asyncio.start_server(maker, host(), port())
    except OSError as exc:
        log.warning("веб не поднялся на %s:%s — %s", host(), port(), exc)
        return
    log.info("веб слушает %s", base_url())
    async with server:
        await stop.wait()


async def _standalone(path: str) -> None:
    stop = asyncio.Event()
    conn = store.connect(path)
    try:
        await serve(conn, stop)
    finally:
        conn.close()


def main(argv: Any = None) -> int:
    """Веб отдельно от сторожей — чтобы смотреть интерфейс, не трогая сбор.

        python -m news.web                 # поднять на 127.0.0.1:6769
        python -m news.web --вход 12345    # ссылка для входа без бота
    """
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Веб-интерфейс новостей")
    parser.add_argument("--db", default=str(store.DEFAULT_PATH), help="файл базы")
    parser.add_argument("--вход", dest="login", type=int, default=0,
                        help="выдать ссылку входа для этого пользователя Telegram")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.login:
        conn = store.connect(args.db)
        from . import bot as bot_module  # noqa: PLC0415

        bot_module.ensure_user(conn, args.login, "проверка")
        print("{}/вход?код={}".format(base_url(), code_for(conn, args.login)))
        conn.close()
        return 0
    asyncio.run(_standalone(args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ("COOKIE", "DEFAULT_HOST", "DEFAULT_PORT", "Request", "Response",
           "base_url", "code_for", "cookie_value", "csrf", "handle", "host", "new_session",
           "parse", "main", "port", "read_body", "redeem", "redirect", "route", "safe_back", "serve",
           "theme_cookie", "theme_of", "whoami")
