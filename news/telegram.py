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
from typing import Any

import httpx

log = logging.getLogger("fpnews.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
# Телеграм режет сообщения на 4096 символах; оставляем запас на разметку.
MAX_TEXT = 3900


def token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


class Bot:
    """Обёртка над двумя методами API. Без токена молча ничего не делает."""

    def __init__(self, session: httpx.AsyncClient, bot_token: str = "") -> None:
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
            log.warning("телеграм %s не ответил: %s", method, exc)
            return None
        if not data.get("ok"):
            log.warning("телеграм %s отказал: %s", method, str(data)[:200])
            return None
        return data

    async def send(self, chat_id: int, text: str, preview: bool = True) -> bool:
        """Одно сообщение. Разметка HTML, ссылка обязательна [NEWS-007]."""
        data = await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:MAX_TEXT],
            parse_mode="HTML",
            link_preview_options={"is_disabled": not preview},
        )
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


__all__ = ("API", "Bot", "MAX_TEXT", "token")
