# Установка и деплой FPNews

## Локально

```bash
git clone git@github.com:lineSence/FPNews.git
cd FPNews
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполнить в `.env` как минимум `TELEGRAM_BOT_TOKEN`, при необходимости — параметры LLM и эмбеддингов. База создаётся сама в `data/fpnews.sqlite3`.

Проверка источников без записи в базу:

```bash
python -m news.probe
```

## Сервер

Ориентир: одно ядро, 500 МБ памяти. Каталог `/opt/fpnews`.

```bash
sudo mkdir -p /opt/fpnews
sudo chown $USER /opt/fpnews
git clone git@github.com:lineSence/FPNews.git /opt/fpnews
cd /opt/fpnews
python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Юнит systemd лежит в `deploy/fpnews.service` (`WorkingDirectory=/opt/fpnews`, `ExecStart=python -m news.run`, `MemoryMax=320M`):

```bash
sudo cp deploy/fpnews.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fpnews
journalctl -u fpnews -f
```

## Веб

По умолчанию веб слушает `127.0.0.1:6769`. Для публичного доступа: обратный прокси (Caddy) на домен, `FPNEWS_WEB_URL=https://<домен>`, `FPNEWS_WEB_SECURE=1`.

## Обновление

```bash
cd /opt/fpnews && git pull && .venv/bin/pip install -r requirements.txt && sudo systemctl restart fpnews
```
