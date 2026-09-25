# Установка на сервер и проверка через SSH-туннель

Сервер: один процессор, гигабайт памяти, из которого свободно около 500 МБ,
рядом уже работают другие сервисы. Отсюда весь стиль установки: без Docker,
без Redis, без Postgres, одна виртуальная среда и один процесс под systemd.

## 1. Что поставить в систему

```bash
sudo apt update
sudo apt install -y python3 python3-venv git sqlite3
python3 -V   # нужен 3.11 или новее
```

## 2. Пользователь и код

Отдельный пользователь без входа в систему — чтобы сервис не ходил под root.

```bash
sudo useradd --system --home /opt/fpnews --shell /usr/sbin/nologin fpnews
sudo mkdir -p /opt/fpnews && sudo chown fpnews: /opt/fpnews

sudo -u fpnews git clone https://github.com/lineSence/FPNews.git /opt/fpnews
cd /opt/fpnews
sudo -u fpnews python3 -m venv .venv
sudo -u fpnews .venv/bin/pip install -U pip
sudo -u fpnews .venv/bin/pip install -r requirements-news.txt
```

Репозиторий приватный, поэтому клонирование спросит доступ. Проще всего —
ключ развёртывания: `ssh-keygen -t ed25519 -f ~/.ssh/fpnews_deploy`, публичную
часть добавить в настройках репозитория (Settings → Deploy keys, только
чтение), и клонировать по SSH-адресу.

## 3. Секреты

```bash
sudo -u fpnews tee /opt/fpnews/.env >/dev/null <<'ENV'
TELEGRAM_BOT_TOKEN=123456:AA...
ENV
sudo chmod 600 /opt/fpnews/.env
```

Токен нигде больше не хранится и в репозиторий не попадает `[CORE-012]`.
Бота заводит @BotFather; домен для будущего входа в веб привязывается
командой `/setdomain` → `mousehousespb.online`.

## 4. Проверка до запуска службы

Всё это безопасно гонять руками — ни одно из действий ничего не рассылает.

```bash
cd /opt/fpnews
sudo -u fpnews .venv/bin/python -m news.probe                 # обе двери, один заход
sudo -u fpnews .venv/bin/python -m news.probe fontanka -n 20 -e 15
```

Второй прогон — главный: двадцать заходов раз в пятнадцать секунд показывают,
как ведёт себя ddos-guard именно с вашего адреса. Смотреть на `отказов` и
`коды` в итоговой сводке. Если появятся 403 — интервал увеличится сам
`[NEWS-006]`, а в `docs/news-sources.md` надо будет записать новую цифру.

## 5. Служба

```bash
sudo cp /opt/fpnews/deploy/fpnews.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fpnews
systemctl status fpnews
journalctl -u fpnews -f
```

В журнале при старте видно: «холодный старт, подобрано N — не считаем и не
шлём». Это правильно: лента на момент запуска — не новости.

Обновление после изменений в репозитории:

```bash
cd /opt/fpnews && sudo -u fpnews git pull && sudo systemctl restart fpnews
```

## 6. Проверка через SSH-туннель

Телеграму туннель не нужен: бот сам ходит наружу. Туннель нужен, чтобы
смотреть на внутренности сервера, не открывая ни одного порта в интернет.

**Порт под веб-интерфейс (появится на шаге 7).** Сервис будет слушать только
`127.0.0.1:8765`, а вы пробрасываете его к себе:

```bash
ssh -N -L 8765:127.0.0.1:8765 root@ВАШ_СЕРВЕР
# в браузере: http://127.0.0.1:8765
```

**Пока веба нет — три полезные команды по SSH:**

```bash
# как быстро доходят новости
ssh root@ВАШ_СЕРВЕР 'cd /opt/fpnews && .venv/bin/python -m news.run --latency'

# что вообще собралось за последний час
ssh root@ВАШ_СЕРВЕР "sqlite3 /opt/fpnews/data/fpnews.sqlite3 \
  \"SELECT source, datetime(listed_at), substr(title,1,60) FROM items \
    WHERE listed_at > datetime('now','-1 hour') ORDER BY id DESC LIMIT 20\""

# живой журнал
ssh root@ВАШ_СЕРВЕР 'journalctl -u fpnews -f'
```

**Копия базы к себе.** Копировать файл на ходу нельзя: рядом лежит журнал WAL,
и получится битый снимок. Правильно так:

```bash
ssh root@ВАШ_СЕРВЕР "sqlite3 /opt/fpnews/data/fpnews.sqlite3 \
  \".backup '/tmp/fpnews-copy.sqlite3'\""
scp root@ВАШ_СЕРВЕР:/tmp/fpnews-copy.sqlite3 .
```

## 7. Сценарий первой проверки целиком

1. `systemctl status fpnews` — служба работает, в журнале холодный старт.
2. В Telegram: `/старт`, затем `/добавить` со словами, которые точно встретятся
   сегодня («петербург, суд, метро»).
3. Ждать. Медуза публикует несколько материалов в час, Фонтанка чаще.
4. Пришло сообщение — посмотреть `/задержка`: там медиана и девяностый
   процентиль по трём участкам пути.
5. Через сутки повторить: цифры одного дня — ещё не измерение `[CORE-019]`.

## 8. Если что-то не так

| Симптом | Где смотреть |
|---|---|
| Служба перезапускается | `journalctl -u fpnews -n 100`; при упоминании памяти поднять `MemoryMax` |
| Новостей нет совсем | `news.probe` — жива ли дверь; в журнале коды ответов |
| Сообщения не приходят | токен в `.env`, в журнале «TELEGRAM_BOT_TOKEN не задан» |
| Фонтанка отдаёт 403 | защита заметила частоту; увеличить `interval` источника в `news/sources.py` |
| База растёт | `sqlite3 ... "SELECT COUNT(*) FROM items"`; чистка старого появится вместе с обогащением |
