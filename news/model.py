"""Шлюз моделей: один асинхронный вызов чата через локальный LiteLLM.

Здесь нет ни одного вызова из горячего пути `[NEWS-002]`. Сюда приходят только
по кнопке под уже отправленным сообщением, то есть тогда, когда человек сам
согласился подождать несколько секунд.

Три решения, ради которых файл существует.

1. **Каскад, а не повторы.** У задачи список моделей. На 429 и 404 повторять
   ту же модель бессмысленно — квота за пять секунд не вернётся, а имя модели
   не появится. Кандидат выбывает, спрашиваем следующего `[CORE-016]`.
2. **Бюджет на сутки.** Кнопки нажимает живой человек, и заклинивший палец не
   должен стоить месячной квоты. Счётчик общий на процесс, сбрасывается
   календарным днём.
3. **Молчание вместо падения.** Модель не ответила — возвращается пустой
   ответ с причиной, бот пишет «не получилось», сторожа продолжают работу
   `[CORE-017]`.

Асинхронный клиент здесь не свой: он приходит снаружи, тот же самый, каким
ходим к источникам. Второе соединение на одноядерном сервере с полугигабайтом
памяти не нужно `[CORE-025]`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

log = logging.getLogger("fpnews.model")

# Локальный LiteLLM на том же сервере: по сети наружу ходит он, не мы.
DEFAULT_URL = "http://127.0.0.1:4000/v1"
DEFAULT_MODELS = "gemini-flash, groq-llama, local"
# Повтор осмыслен только там, где меняется состояние сервиса, а не запрос.
RETRY_STATUSES = frozenset({408, 425, 500, 502, 503, 504, 529})
MAX_ERROR = 300
MAX_CANDIDATES = 3


@dataclass(frozen=True)
class Answer:
    """Что вернула модель. Пустой `text` — не ответила, причина в `error`."""

    text: str = ""
    model: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


@dataclass
class Budget:
    """Потолок вызовов на сутки и список выбывших моделей.

    Выбывшие живут до конца суток вместе со счётчиком: квота у провайдеров
    тоже считается по дням, а таймеры на каждую модель — лишние строки
    `[CORE-025]`.
    """

    max_calls: int = 0
    calls: int = 0
    failures: int = 0
    cached: int = 0
    day: str = ""
    dropped: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.max_calls:
            self.max_calls = int(os.getenv("FPNEWS_LLM_MAX_CALLS") or 200)
        self.day = str(date.today())

    def _roll(self) -> None:
        today = str(date.today())
        if today != self.day:
            self.day, self.calls, self.failures, self.cached = today, 0, 0, 0
            self.dropped.clear()

    def take(self) -> bool:
        """Занять вызов. False — на сегодня хватит."""
        self._roll()
        if self.calls >= self.max_calls:
            return False
        self.calls += 1
        return True

    def drop(self, model: str, why: str) -> None:
        self.dropped[model] = why
        log.info("модель %s выбыла до конца суток: %s", model, why)

    def alive(self, models: Sequence[str]) -> list[str]:
        self._roll()
        return [name for name in models if name not in self.dropped]


def models() -> list[str]:
    """Кандидаты по порядку: сначала быстрый и бесплатный, последним местный."""
    raw = os.getenv("FPNEWS_LLM_MODELS") or DEFAULT_MODELS
    out: list[str] = []
    for chunk in raw.replace(";", ",").split(","):
        name = chunk.strip()
        if name and name not in out:
            out.append(name)
    return out[:MAX_CANDIDATES]


def _reason(payload: Any, fallback: str) -> str:
    """Человеческая причина отказа: она в теле ответа, а не в коде статуса."""
    if isinstance(payload, dict):
        value = payload.get("error") or payload.get("detail") or payload.get("message")
        if isinstance(value, dict):
            value = value.get("message") or value.get("type") or value.get("code")
        if isinstance(value, str) and value.strip():
            return value.strip()[:MAX_ERROR]
    return (fallback or "непонятный ответ").strip()[:MAX_ERROR]


async def ask(session: Any, prompt: str, text: str, budget: Budget,
              temperature: float = 0.2, timeout: float = 45.0) -> Answer:
    """Спросить первую отвечающую модель из каскада.

    `prompt` — что делать, `text` — материал. Разделены не для красоты: так
    видно в логе, какая часть запроса наша, а какая пришла с чужого сайта.
    """
    base = (os.getenv("FPNEWS_LLM_URL") or DEFAULT_URL).rstrip("/")
    key = (os.getenv("FPNEWS_LLM_KEY") or "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer {}".format(key)
    candidates = budget.alive(models())
    if not candidates:
        return Answer(error="все модели выбыли на сегодня")
    last = ""
    for name in candidates:
        if not budget.take():
            return Answer(error="дневной бюджет вызовов исчерпан")
        try:
            response = await session.post(
                base + "/chat/completions",
                json={
                    "model": name,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": text},
                    ],
                    "temperature": temperature,
                },
                headers=headers,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 — чужая сеть [CORE-017]
            budget.failures += 1
            last = "{}: сеть молчит ({})".format(name, type(exc).__name__)
            log.warning(last)
            continue
        if response.status_code >= 400:
            try:
                payload: Any = response.json()
            except ValueError:
                payload = None
            last = "{}: {} {}".format(name, response.status_code,
                                      _reason(payload, response.text))
            budget.failures += 1
            if response.status_code not in RETRY_STATUSES:
                # 429 и 404 за секунды не чинятся: кандидат выбывает [CORE-016].
                budget.drop(name, "код {}".format(response.status_code))
            log.warning("модель не ответила — %s", last)
            continue
        try:
            payload = response.json()
            answer = (payload["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError, ValueError):
            budget.failures += 1
            last = "{}: ответ без текста".format(name)
            continue
        if not answer:
            budget.failures += 1
            last = "{}: пустой ответ".format(name)
            continue
        log.info("модель %s ответила, знаков %s", name, len(answer))
        return Answer(text=answer, model=name)
    return Answer(error=last or "ни одна модель не ответила")


__all__ = ("Answer", "Budget", "DEFAULT_MODELS", "DEFAULT_URL", "MAX_CANDIDATES",
           "RETRY_STATUSES", "ask", "models")
