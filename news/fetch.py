"""Поход к двери источника: условный запрос, сжатие, живое соединение.

Три вещи, ради которых модуль вообще существует.

1. **Условный запрос.** Медуза отдаёт `Last-Modified`, и с ним ответ «ничего
   нового» стоит 304 и ноль байт вместо 300 КБ. Сохранять валидаторы между
   опросами — обязанность вызывающего: он держит `Door`.
2. **Живое соединение.** Мы ходим к одному хосту каждые десять секунд;
   рукопожатие TLS стоит 100–300 мс, то есть больше, чем сам ответ. Клиент
   создаётся один раз и переиспользуется.
3. **Уважение к чужой защите `[NEWS-006]`.** Ответ 403, 429 или страница
   проверки не повод долбиться чаще: интервал удваивается до первого успеха.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

UA = "Mozilla/5.0 (compatible; FPNewsBot/0.1; +https://mousehousespb.online)"
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate", "Connection": "keep-alive"}
# Потолок роста интервала при отказах: дальше уже не вежливость, а простой.
MAX_BACKOFF = 8


def client(timeout: float = 20.0) -> httpx.AsyncClient:
    """Один клиент на всю жизнь процесса: соединения остаются открытыми."""
    limits = httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120.0)
    return httpx.AsyncClient(headers=HEADERS, timeout=timeout, limits=limits, follow_redirects=True)


@dataclass
class Poll:
    """Итог одного опроса. `body` пуст при 304 и при отказе."""

    status: int
    seconds: float
    size: int
    body: str = ""
    conditional: bool = False  # ответ 304: дверь сказала «ничего нового»
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 or self.conditional

    @property
    def refused(self) -> bool:
        """Защита сайта, а не наша ошибка: 403, 429 и прочие «отойди»."""
        return self.status in (403, 409, 429, 503)


@dataclass
class Door:
    """Состояние двери между опросами: валидаторы и рост интервала."""

    etag: str = ""
    modified: str = ""
    backoff: int = 1
    seen: set[str] = field(default_factory=set)

    def headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        if self.etag:
            out["If-None-Match"] = self.etag
        if self.modified:
            out["If-Modified-Since"] = self.modified
        return out

    def note(self, poll: Poll) -> None:
        if poll.refused or poll.error:
            self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        else:
            self.backoff = 1


async def poll(session: httpx.AsyncClient, url: str, door: Door) -> Poll:
    """Один заход к двери. Не бросает: сторож не имеет права падать."""
    started = time.monotonic()
    try:
        response = await session.get(url, headers=door.headers())
    except Exception as exc:  # noqa: BLE001 — чужая сеть [CORE-017]
        result = Poll(status=0, seconds=time.monotonic() - started, size=0, error=str(exc)[:200])
        door.note(result)
        return result
    spent = time.monotonic() - started
    if response.status_code == 304:
        result = Poll(status=304, seconds=spent, size=0, conditional=True)
        door.note(result)
        return result
    if response.status_code == 200:
        door.etag = response.headers.get("etag", "")
        door.modified = response.headers.get("last-modified", "")
        body = response.text
        # Считаем байты, которые реально прошли по сети: `content` уже
        # распакован, и по нему трафик получается впятеро больше настоящего.
        wire = getattr(response, "num_bytes_downloaded", 0) or len(response.content)
        result = Poll(status=200, seconds=spent, size=wire, body=body)
        door.note(result)
        return result
    wire = getattr(response, "num_bytes_downloaded", 0) or len(response.content)
    result = Poll(status=response.status_code, seconds=spent, size=wire)
    door.note(result)
    return result
