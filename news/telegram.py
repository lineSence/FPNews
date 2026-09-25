"""Telegram: тонкий клиент Bot API на httpx.

Без aiogram намеренно. Нам нужны две операции — отправить сообщение и забрать
обновления, — а библиотека потянула бы десятки мегабайт памяти на сервере, где
её свободно около пятисот `[CORE-025]`.

Сеть здесь чужая и ненадёжная, поэтому ни один вызов не бросает исключение
наружу: не доставленное сообщение — это запись в лог и повтор на следующем
круге, а не падение сторожа `[CORE-017]`.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

log = logging.getLogger("fpnews.telegram")


def hush() -> None:
    """Затыкает httpx: он пишет в лог полный адрес запроса.

    У Telegram токен — часть адреса, поэтому обычный INFO-лог httpx означает
    токен в journald открытым текстом, а журнал читают, пересылают и кладут в
    отчёты. Секрет не должен попадать в лог ни при каких настройках
    подробности [CORE-012].
    """
    for name in ("httpx", "httpcore", "hpack"):
        logging.getLogger(name).setLevel(logging.WARNING)

API = "https://api.telegram.org/bot{token}/{method}"
# На случай, если в тексте ошибки окажется чужой или старый токен.
TOKEN_RE = re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}")
# Телеграм режет сообщения на 4096 символах; оставляем запас на разметку.
MAX_TEXT = 3900


def token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def safe(value: object) -> str:
    """Текст без токена: ошибки httpx содержат адрес запроса целиком."""
    text = str(value or "")
    secret = token()
    if secret:
        text = text.replace(secret, "…")
    return TOKEN_RE.sub("bot…", text)


class Bot:
    """Обёртка над двумя методами API. Без токена молча ничего не делает."""

    def __init__(self, session: httpx.AsyncClient, bot_token: str = "") -> None:
        hush()
        self.session = session
        self.token = bot_token or token()
        self.offset = 0

    @property
    def ready(self) -> bool:
        return bool(self.token)

    async def call(self, method: str, **payload: Any) -> dict[str, Any] | None:
        if not self.ready:
            log.debug("нет TELEGRAM_BOT_TOKEN, метод %s пропущен", method)
            return None
        try:
            response = await self.session.post(
                API.format(token=self.token, method=method), json=payload, timeout=30.0
            )
            data = response.json()
        except Exception as exc:  # noqa: BLE001 — чужая сеть [CORE-017]
            log.warning("телеграм %s не ответил: %s", method, safe(exc))
            return None
        if not data.get("ok"):
            log.warning("телеграм %s отказал: %s", method, safe(data)[:200])
            return None
        return data

    async def send(self, chat_id: int, text: str, preview: bool = True,
                   keyboard: dict[str, Any] | None = None) -> bool:
        """Одно сообщение. Разметка HTML, ссылка обязательна [NEWS-007]."""
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MAX_TEXT],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": not preview},
        }
        if keyboard:
            payload["reply_markup"] = keyboard
        data = await self.call("sendMessage", **payload)
        return data is not None

    async def ack(self, callback_id: str, text: str = "") -> bool:
        """Погасить «часики» на кнопке. Без этого телеграм крутит их минуту.

        Ответ обязателен в течение нескольких секунд, а модель думает дольше,
        поэтому гасим сразу, а результат присылаем отдельным сообщением.
        """
        data = await self.call("answerCallbackQuery", callback_query_id=callback_id,
                               text=text[:200])
        return data is not None

    async def updates(self, timeout: int = 25) -> list[dict[str, Any]]:
        """Длинный опрос: соединение висит, пока не придёт сообщение."""
        data = await self.call("getUpdates", offset=self.offset, timeout=timeout)
        if not data:
            return []
        result = data.get("result") or []
        if result:
            self.offset = int(result[-1]["update_id"]) + 1
        return result


__all__ = ("API", "Bot", "MAX_