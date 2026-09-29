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

И две границы, поставленные потому, что ответ пишет не мы.

* **Потолок на тело.** Раньше ответ читался целиком: `response.text` без
  единого ограничения. Взломанная или просто сломавшаяся дверь, отдающая
  поток на сотни мегабайт, забрала бы всю память сервера, где её всего
  полгигабайта `[CORE-025]`. Теперь тело читается кусками и обрывается на
  потолке, а сторож честно говорит, что ответ обрезан `[NEWS-001]`.
* **Свой хост.** Переходы мы выполняем, но только внутри того же хоста.
  Домен издания могут продать — с `paperpaper.ru` это уже случилось, там
  теперь сайт про микрозаймы. Молча пойти по такому переходу и разобрать
  чужую страницу как ленту издания хуже, чем остаться без ответа.
"""

from __future__ import annotations

import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx

UA = "Mozilla/5.0 (compatible; FPNewsBot/0.1; +https://mousehousespb.online)"
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate", "Connection": "keep-alive"}
# Потолок роста интервала при отказах: дальше уже не вежливость, а простой.
MAX_BACKOFF = 8
# Потолок на тело ответа. Самая тяжёлая из измеренных дверей — лента
# Фонтанки, 545 КБ; два мегабайта дают запас вчетверо и всё равно не дают
# чужой двери съесть память.
MAX_BODY = 2 * 1024 * 1024
# Больше трёх переходов не делает ни одна нормальная дверь.
MAX_REDIRECTS = 3
# Сколько адресов помним между заходами. Нужно ровно на один вопрос — «это
# было в прошлый раз?», и полной истории для него не требуется.
MAX_SEEN = 5000


def client(timeout: float = 20.0) -> httpx.AsyncClient:
    """Один клиент на всю жизнь процесса: соединения остаются открытыми."""
    limits = httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120.0)
    return httpx.AsyncClient(headers=HEADERS, timeout=timeout, limits=limits,
                             follow_redirects=True, max_redirects=MAX_REDIRECTS)


def свой_хост(было: str, стало: str) -> bool:
    """Тот же хост, что и просили. Пустой адрес считаем своим."""
    куда = urllib.parse.urlsplit(str(стало or "")).netloc.lower()
    откуда = urllib.parse.urlsplit(str(было or "")).netloc.lower()
    return not куда or not откуда or куда == откуда


class Окно(set):
    """Множество адресов с потолком: помним последние, а не все за месяц.

    Сторож живёт неделями, а лента источника за это время отдаёт десятки
    тысяч адресов. Множество отвечает на один вопрос — «видели ли мы это в
    прошлый заход», и хранить больше нескольких лент подряд незачем
    `[CORE-025]`. Порядок добавления в множестве Python не определён, поэтому
    он хранится рядом, в очереди.
    """

    def __init__(self, откуда: Any = ()) -> None:
        super().__init__()
        self._порядок: deque[str] = deque()
        self.update(откуда)

    def add(self, value: Any) -> None:
        if value in self:
            return
        super().add(value)
        self._порядок.append(value)
        while len(self._порядок) > MAX_SEEN:
            super().discard(self._порядок.popleft())

    def update(self, *наборы: Any) -> None:  # type: ignore[override]
        for набор in наборы:
            for значение in набор:
                self.add(значение)


@dataclass
class Poll:
    """Итог одного опроса. `body` пуст при 304 и при отказе."""

    status: int
    seconds: float
    size: int
    body: str = ""
    conditional: bool = False  # ответ 304: дверь сказала «ничего нового»
    error: str = ""
    url: str = ""  # финальный адрес после переходов: редирект бывает только по делу

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
    seen: set[str] = field(default_factory=Окно)

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
        async with session.stream("GET", url, headers=door.headers()) as response:
            spent = time.monotonic() - started
            if not свой_хост(url, str(response.url)):
                # Не ошибка сети и не отказ издания: дверь ведёт не туда,
                # куда мы стучались, и разбирать этот ответ как ленту нельзя.
                result = Poll(status=response.status_code, seconds=spent, size=0,
                              url=str(response.url),
                              error="дверь увела на чужой хост: {}".format(
                                  urllib.parse.urlsplit(str(response.url)).netloc))
                door.note(result)
                return result
            if response.status_code == 304:
                result = Poll(status=304, seconds=spent, size=0, conditional=True,
                              url=str(response.url))
                door.note(result)
                return result
            куски: list[bytes] = []
            всего, обрыв = 0, False
            async for кусок in response.aiter_bytes():
                всего += len(кусок)
                if всего > MAX_BODY:
                    обрыв = True
                    break
                куски.append(кусок)
            # Считаем байты, которые реально прошли по сети: распакованное
            # тело даёт трафик впятеро больше настоящего.
            wire = getattr(response, "num_bytes_downloaded", 0) or всего
            if response.status_code != 200:
                result = Poll(status=response.status_code, seconds=spent, size=wire,
                              url=str(response.url))
                door.note(result)
                return result
            door.etag = response.headers.get("etag", "")
            door.modified = response.headers.get("last-modified", "")
            body = b"".join(куски).decode(response.encoding or "utf-8", "replace")
            result = Poll(
                status=200, seconds=spent, size=wire, body=body,
            url=str(response.url),
                error="ответ обрезан потолком {} Б".format(MAX_BODY) if обрыв else "")
    except Exception as exc:  # noqa: BLE001 — чужая сеть [CORE-017]
        result = Poll(status=0, seconds=time.monotonic() - started, size=0,
                      error=str(exc)[:200] or type(exc).__name__)
        door.note(result)
        return result
    door.note(result)
    return result


__all__ = ("HEADERS", "MAX_BACKOFF", "MAX_BODY", "MAX_REDIRECTS", "MAX_SEEN", "UA",
           "Door", "Poll", "Окно", "client", "poll", "свой_хост")
