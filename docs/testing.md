# Тесты

```bash
pip install -r requirements-dev.txt
pytest -q
pyflakes news tests diag.py
```

Настройки в `pytest.ini`: `testpaths=tests`, тихий вывод. `tests/conftest.py` только добавляет корень проекта в `sys.path`.

Правила:

- Тесты не ходят в сеть. Ответы источников берём из `tests/fixtures/news/` (`meduza-rss.xml`, `fontanka-24hours.html`) или из подменённых функций.
- База — временный файл через `tmp_path`, не `data/fpnews.sqlite3`.
- Один тест проверяет одно поведение, имя теста описывает ожидание.
- Новый источник — новая фикстура и тест разбора в `tests/test_news_sources.py`.
