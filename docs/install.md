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

## 2. Пользователь и папки

Раскладка такая: дом пользователя `/opt/fpnews`, код в подпапке `app`, а база
и секреты рядом с ним, но **вне репозитория** — тогда `git pull` их не трогает.

```bash
sudo useradd --system --home /opt/fpnews --shell /usr/sbin/nologin fpnews
sudo mkdir -p /opt/fpnews/.ssh /opt/fpnews/data
sudo chown -R fpnews: /opt/fpnews
sudo chmod 700 /opt/fpnews/.ssh
```

Каждая команда `sudo -u fpnews` дальше идёт с `env HOME=/opt/fpnews`: без этого
git и ssh полезут в `/root` и получат отказ по правам.

## 3. Доступ к приватному репозиторию

Ключ развёртывания: он даёт доступ только к этому репозиторию и только на
чтение — в отличие от личного токена, который открывает всё сразу.

```bash
sudo -u fpnews env HOME=/opt/fpnews ssh-keygen -t ed25519 -N '' \
  -f /opt/fpnews/.ssh/id_ed25519 -C 'fpnews@vps'
sudo cat /opt/fpnews/.ssh/id_ed25519.pub
```

Показанную строку добавить на <https://github.com/lineSence/FPNews/settings/keys>
→ Add deploy key, галочку «Allow write access» **не** ставить. Затем:

```bash
sudo -u fpnews env HOME=/opt/fpnews \
  git clone git@github.com:lineSence/FPNews.git /opt/fpnews/app
```

Первый раз ssh спросит про отпечаток github.com — ответить `yes`.

## 4. Среда и секреты

```bash
cd /opt/fpnews/app
sudo -u fpnews env HOME=/opt/fpnews python3 -m venv .venv
sudo -u fpnews .venv/bin/pip install -U pip
sudo -u fpnews .venv/bin/pip install -r requirements-news.txt

printf 'TELEGRAM_BOT_TOKEN=%s\n' 'сюда_токен' | sudo -u fpnews tee /opt/fpnews/.env
sudo chmod 600 /opt/fpnews/.env
```

Токен нигде больше не хранится и в репозиторий не попадает `[CORE-012]`.
Бота заводит @BotFather; домен для будущего входа в веб привязывается
командой `/setdomain` → `mousehousespb.online`.

## 5. Проверка до запуска службы

Всё это безопасно гонять руками — ни одно из действий ничего не рассылает.

```bash
cd /opt/fpnews/app
sudo -u fpnews .venv/bin/python -m news.probe                 # обе двери, один заход
sudo -u fpnews .venv/bin/python -m news.probe fontanka -n 20 -e 15
```

Второй прогон — главный: двадцать заходов раз в пятнадцать секунд показывают,
как ведёт себя ddos-guard именно с вашего адреса. Смотреть на `отказов` и
`коды` в итоговой сводке. Если появятся 403 — интервал увеличится сам
`[NEWS-006]`, а в `docs/news-sources.md` надо будет записать новую цифру.

## 6. Служба

```bash
sudo cp /opt/fpnews/app/deploy/fpnews.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fpnews
systemctl status fpnews
journalctl -u fpnews -f
```

В журнале при старте видно: «холодный старт, подобрано N — не считаем и не
шлём». Это правильно: лента на момент запуска — не новости.

Обновление после изменений в репозитории:

```bash
sudo -u fpnews env HOME=/opt/fpnews git -C /opt/fpnews/app pull
sudo systemctl restart fpnews
```

## 7. Проверка через SSH-туннель

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
ssh root@ВАШ_СЕРВЕР 'cd /opt/fpnews && PYTHONPATH=app app/.venv/bin/python -m news.run --latency'

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

## 8. Сценарий первой проверки целиком

1. `systemctl status fpnews` — служба работает, в журнале холодный старт.
2. В Telegram: `/старт`, затем `/добавить` со словами, которые точно встретятся
   сегодня («петербург, суд, метро»).
3. Ждать. Медуза публикует несколько материалов в час, Фонтанка чаще.
4. Пришло сообщение — посмотреть `/задержка`: там медиана и девяностый
   процентиль по трём участкам пути.
5. Через сутки повторить: цифры одного дня — ещё не измерение `[CORE-019]`.

## 9. Если что-то не так

| Симптом | Где смотреть |
|---|---|
| Служба перезапускается | `journalctl -u fpnews -n 100`; при упоминании памяти поднять `MemoryMax` |
| Новостей нет совсем | `news.probe` — жива ли дверь; в журнале коды ответов |
| Сообщения не приходят | токен в `.env`, в журнале «TELEGRAM_BOT_TOKEN не задан» |
| Фонтанка отдаёт 403 | защита заметила частоту; увеличить `interval` источника в `news/sources.py` |
| `Permission denied` при `sudo -u fpnews` | забыт `env HOME=/opt/fpnews` |
| База растёт | `sqlite3 ... "SELECT COUNT(*) FROM items"`; чистка старого появится вместе с обогащением |
