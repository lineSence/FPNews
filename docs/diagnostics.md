# Диагностика

Общий модуль `diag.py` в корне: замеры шагов и печать сводки. Используется в `news/run.py`, `news/probe.py`, `news/watch.py`.

```bash
python -m news.run --diag       # печать шагов цикла
python -m news.run --latency    # замеры задержек источников
DIAG_RUN=1 python -m news.run   # то же через переменную окружения
python -m news.probe -e         # показать ошибки разбора источников
python -m news.probe --fallback # проверить запасные способы разбора
```

Что смотреть при разборах:

- источник ничего не вернул — проверить `news/sources.py` и фикстуру в тестах;
- материал без текста — разбор в `news/article.py`;
- дубли не склеиваются — порог `FPNEWS_STORY_THRESHOLD` и simhash в `news/dedup.py`;
- не уходит в Telegram — `TELEGRAM_BOT_TOKEN`, таблица `deliveries`;
- веб не открывается — `FPNEWS_WEB`, `FPNEWS_WEB_HOST`, `FPNEWS_WEB_PORT`.

Отсутствие данных отмечаем как отсутствие, а не как ноль [NEWS-001].
