# FPNews

Агрегатор петербургских новостей: собирает публикации семи изданий, ищет повторы и сюжеты, отдаёт ленту в Telegram и в веб-интерфейс.

Проект выделен из родительского репозитория FuckHR. Код FuckHR сохранён в ветке `legacy` — оттуда берём куски кода, если что-то нужно перенести. В `main` остаётся только FPNews.

## Что умеет

- Опрос источников: Meduza, Фонтанка, Интерфакс, Деловой Петербург, РИА, Мойка78, Бумага (коды: `meduza`, `fontanka`, `interfax`, `dp`, `ria`, `moika78`, `paper`).
- Дедупликация по simhash, склейка публикаций в сюжеты, отслеживание правок материалов (ревизии).
- Обогащение текста и эмбеддинги через внешний LLM-эндпоинт (опционально, с лимитом вызовов).
- Доставка в Telegram: сырое, дополнение, изменение, тоже_написали.
- Веб-интерфейс без JavaScript: вход по коду, лента, задержки, темы.

## Установка

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Подробности и деплой: `docs/install.md`, юнит systemd — `deploy/fpnews.service`.

## Команды

```bash
python -m news.run                 # цикл сбора и доставки
python -m news.run meduza fontanka # только указанные источники
python -m news.run -n 20           # ограничить число материалов
python -m news.run --diag          # диагностика шагов
python -m news.run --latency       # замеры задержек
python -m news.probe               # проверка источников (-n, -e, --fallback)
python -m news.web --вход          # веб-интерфейс
```

## Структура

```
news/        код проекта (fetch, article, dedup, story, enrich, embed, store, deliver, telegram, bot, web, pages, topics, watch, probe, recheck, run, sources, model)
diag.py      общая диагностика (используется news.run, news.probe, news.watch)
docs/        документация FPNews
tests/       тесты (pytest -q)
deploy/      systemd-юнит
wiki/rules/  правила проекта
```

## Данные

SQLite в `data/fpnews.sqlite3` (WAL). Схема описана в `docs/news-schema.md`.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest -q
```
