"""Веб: маленький HTTP-сервер на том же событийном цикле.

Зачем свой, а не фреймворк. Нам нужны пять страниц без единой строчки
JavaScript. `aiohttp` или `starlette` с `uvicorn` — это 30–60 МБ памяти и
десятки мегабайт зависимостей на сервере, где свободно около пятисот
`[CORE-025]`. `http.server` из стандартной библиотеки не годится по другой
причине: он синхронный и заблокировал бы сторожей `[NEWS-002]`.

Здесь `asyncio.start_server`, разбор запроса руками и ответ строкой. Это
примерно двести строк, которые полностью понятны и ничего не тянут.

Про безопасность. Сервер слушает `127.0.0.1` и наружу не смотрит: снаружи
будет Caddy с сертификатом, а он же добавит HTTPS и ограничение частоты. До
этого момента интерфейс доступен только на самой машине или через
`ssh -N -L 6769:127.0.0.1:6769`.

Вход — одноразовый код из бота, а не Telegram Login Widget. Причина простая:
виджету нужен публичный домен, привязанный к боту, а проверять интерфейс надо
на `localhost` до всякого домена. Код живёт пять минут, сгорает при первом
использовании и не даёт ничего, кроме сессии того же человека, который его
запросил.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable

from . import pages, store

log = logging.getLogger("fpnews.web")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6769
# Запрос без тела больше этого — не наш: формы здесь по сотне байт.
MAX_BODY = 64 * 1024
COOKIE = "fpnews"
SESSION_DAYS = 30


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

    def raw(self) -> bytes:
        data = self.body.encode("utf-8")
        head = [
            "HTTP/1.1 {}".format(self.status),
            "Content-Type: {}".format(self.kind),
            "Content-Length: {}".format(len(data)),
            "X-Content-Type-Options: nosniff",
            "Referrer-Policy: same-origin",
            "Connection: close",
        ]
        if self.location:
            head.append("Location: {}".format(self.location))
        if self.cookie:
            head.append("Set-Cookie: {}".format(self.cookie))
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
    return Request(
        method=method,
        path=urllib.parse.unquote(path),
        query=dict(urllib.parse.parse_qsl(raw_query, keep_blank_values=True)),
        headers=headers,
        form=dict(urllib.parse.parse_qsl(body, keep_blank_values=True)),
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
    secure = "; Secure" if (os.getenv("FPNEWS_WEB_SECURE") or "") == "1" else ""
    return "{}={}; Path=/; HttpOnly; SameSite=Lax; Max-Age={}{}".format(
        COOKIE, token, SESSION_DAYS * 86400, secure
    )


def csrf(token: str) -> str:
    """Метка формы — часть ключа сессии. Чужая вкладка её не знает."""
    import hashlib  # noqa: PLC0415

    return hashlib.blake2b((token or "").encode(), digest_size=8).hexdigest()


def route(conn: Any, request: Request) -> Response:
    """Вся маршрутизация. Чистая функция — поэтому и проверяется тестами."""
    from . import bot as bot_module  # noqa: PLC0415 — импорт здесь разрывает круг

    token = request.cookies.get(COOKIE) or ""
    user_id = whoami(conn, request)
    if request.path == "/вход":
        entering = redeem(conn, request.query.get("код", ""))
        if not entering:
            return Response(pages.login(), status="401 Unauthorized")
        return redirect("/", cookie_value(new_session(conn, entering)))
    if not user_id:
        return Response(pages.login(), status="401 Unauthorized")
    if request.method == "POST":
        if request.form.get("метка") != csrf(token):
            return Response(pages.oops("Форма устарела. Обновите страницу."),
                            status="400 Bad Request")
        if request.path == "/темы/добавить":
            bot_module.add_topic(conn, user_id, request.form.get("слова", ""))
        elif request.path == "/темы/удалить":
            bot_module.drop_topic(conn, user_id, request.form.get("номер", ""))
        elif request.path == "/выход":
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return redirect("/", "{}=; Path=/; Max-Age=0".format(COOKIE))
        return redirect("/")
    if request.path == "/":
        return Response(pages.home(conn, user_id, csrf(token)))
    if request.path == "/задержки":
        return Response(pages.latency(conn))
    if request.path == "/новости":
        return Response(pages.feed(conn, user_id))
    return Response(pages.oops("Такой страницы нет."), status="404 Not Found")


async def handle(conn: Any, reader: Any, writer: Any) -> None:
    try:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
        head = raw.decode("utf-8", "replace").rstrip("\r\n")
        length = 0
        for line in head.split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = min(int(line.split(":")[1].strip() or 0), MAX_BODY)
        body = ""
        if length:
            body = (await reader.readexactly(length)).decode("utf-8", "replace")
        response = route(conn, parse(head, body))
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ValueError):
        response = Response("", status="400 Bad Request")
    except Exception as exc:  # noqa: BLE001 — веб не роняет сбор новостей [CORE-017]
        log.warning("страница не отдалась: %s", exc)
        response = Response(pages.oops("Что-то сломалось."), status="500 Internal Server Error")
    try:
        writer.write(response.raw())
        await writer.drain()
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
    log.info("веб слушает http://%s:%s", host(), port())
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


__all__ = ("COOKIE", "DEFAULT_HOST", "DEFAULT_PORT", "Request", "Response", "base_url",
           "code_for", "cookie_value", "csrf", "handle", "host", "new_session", "parse",
           "main", "port", "redeem", "redirect", "route", "serve", "whoami")
