"""Сущности: кто, что и сколько упомянуто в материале.

Только правила, никакой модели. Извлечение стоит в пути обработки новости,
сразу после разбора текста, а туда модель не пускают [NEWS-002]. Регулярки
на русском тексте дают меньше, чем разбор языка, зато стоят микросекунды и
работают одинаково в три часа ночи и при отсутствии квоты [CORE-025].

Что берём и почему именно это:

- **организации** — кавычки («Метрострой») и правовые формы (ООО, АО, ПАО,
  ГУП). Это самая надёжная зацепка в русском новостном тексте;
- **люди** — «Имя Фамилия» с заглавных и «А. Иванов». Порог намеренно
  строгий: ложный человек засоряет карточку сильнее, чем пропущенный;
- **деньги** — число с «рубл», «млн», «млрд». Сумма — это факт, вокруг
  которого потом строится вопрос;
- **места** — улица, проспект, набережная, площадь, шоссе, переулок, мост.

Извлечение — это наблюдение, а не вывод [NEWS-008]. Мы записываем «в тексте
встретилось», а не «эта компания замешана». Отсутствие сущности в материале
означает, что правило её не увидело, а не что её там нет [NEWS-001].
"""

from __future__ import annotations

import re
from typing import Any

KINDS = ("организация", "человек", "деньги", "место")

# Слово с заглавной: русское или латинское, от двух букв.
CAP = r"[А-ЯЁA-Z][а-яёa-z\-]{1,}"

FORMS = r"(?:ООО|ОАО|ЗАО|ПАО|АО|ГУП|СПб ГУП|ФГУП|НКО|АНО|ИП)"
IN_QUOTES = re.compile(r"[«\"]([^«»\"]{2,60})[»\"]")
WITH_FORM = re.compile(r"\b{form}\s+[«\"]?({cap}(?:\s+{cap}){{0,3}})".format(form=FORMS, cap=CAP))
# Пары ищем с перекрытием: в «Вице-губернатор Иван Петров» обычный поиск
# съел бы первую пару и потерял настоящую.
PERSON = re.compile(r"(?=\b({cap})\s+({cap})\b)".format(cap=CAP))
# Фамилию узнаём по окончанию. Правило грубое, зато не записывает в люди
# «Ремонт Литейного»: ложный человек в карточке хуже пропущенного [NEWS-008].
SURNAME = re.compile(r"(ов|ев|ёв|ин|ын|ский|цкий|ской|ова|ева|ина|ына|ская|цкая|ко|ук|юк|"
                     r"ян|дзе|швили|ич|ук|енко|ук)$", re.IGNORECASE)
INITIALS = re.compile(r"\b([А-ЯЁ]\.\s?[А-ЯЁ]?\.?)\s?({cap})\b".format(cap=CAP))
MONEY = re.compile(
    r"\b(\d[\d\s.,]{0,15}?)\s*(тыс\.?|млн|млрд|миллион\w*|миллиард\w*)?\s*"
    r"(руб\w*|₽|долл\w*|евро)\b", re.IGNORECASE)
PLACE_WORDS = r"(?:улиц\w+|проспект\w*|набережн\w+|площад\w+|шоссе|переул\w+|мост\w*|бульвар\w*)"
# Регистр важен: с `IGNORECASE` «набережной Фонтанки закончат» приклеивало
# глагол к названию.
PLACE = re.compile(r"\b(?:{place}|{place_cap})\s+({cap})".format(
    place=PLACE_WORDS, place_cap=PLACE_WORDS.capitalize(), cap=CAP))
PLACE_BEFORE = re.compile(r"\b({cap})\s+(?:{place})".format(cap=CAP, place=PLACE_WORDS))

# Слова, которые пишутся с заглавной в начале предложения и человеком не
# являются. Список короткий: он закрывает почти весь ложный улов.
STOP = {
    "россии", "россия", "петербурга", "петербург", "москвы", "москва", "санкт",
    "смольного", "смольный", "город", "города", "по", "в", "на", "как", "что",
    "это", "при", "для", "после", "также", "однако", "фонтанка", "интерфакс",
    "риа", "новости", "деловой", "бумага", "медуза", "мойка",
}
# Потолок на материал: длинная лента комментариев не должна превращаться в
# сотню сущностей и раздувать базу.
MAX_PER_ITEM = 40
MAX_CHARS = 60_000


def normalize(name: str) -> str:
    """Ключ для склейки написаний: регистр, ё и лишние пробелы не считаются."""
    return re.sub(r"\s+", " ", str(name or "").lower().replace("ё", "е")).strip(" .,;:«»\"")


def _ok(name: str) -> bool:
    clean = normalize(name)
    return bool(clean) and len(clean) >= 3 and clean not in STOP and not clean.isdigit()


def organisations(text: str) -> list[str]:
    found = [match.group(1).strip() for match in WITH_FORM.finditer(text)]
    found += [match.group(1).strip() for match in IN_QUOTES.finditer(text)]
    return [name for name in found if _ok(name)]


def people(text: str) -> list[str]:
    """Люди: «Имя Фамилия» и «А. Фамилия». Фамилия — по окончанию."""
    found = []
    for match in PERSON.finditer(text):
        first, second = match.group(1), match.group(2)
        if normalize(first) in STOP or normalize(second) in STOP:
            continue
        if re.match(PLACE_WORDS, second, re.IGNORECASE):
            continue
        if not SURNAME.search(second):
            continue
        found.append("{} {}".format(first, second))
    for match in INITIALS.finditer(text):
        фамилия = match.group(2)
        if SURNAME.search(фамилия):
            found.append("{} {}".format(match.group(1).strip(), фамилия))
    return [name for name in found if _ok(name)]


def money(text: str) -> list[str]:
    out = []
    for match in MONEY.finditer(text):
        число = re.sub(r"[\s]+", " ", match.group(1)).strip(" .,")
        размер = (match.group(2) or "").strip()
        валюта = match.group(3).strip()
        if not число:
            continue
        out.append(" ".join(part for part in (число, размер, валюта) if part))
    return out


def places(text: str) -> list[str]:
    """Места: «набережная Фонтанки», «Литейный мост». Без глаголов вокруг."""
    found = [re.sub(r"\s+", " ", match.group(0)).strip() for match in PLACE.finditer(text)]
    found += [re.sub(r"\s+", " ", match.group(0)).strip() for match in PLACE_BEFORE.finditer(text)]
    return [name for name in found if _ok(name)]


def extract(title: str, body: str = "") -> list[dict[str, Any]]:
    """Все сущности материала: вид, написание, где встретилось, сколько раз.

    Заголовок и текст считаются вместе, но «в заголовке» отмечается отдельно:
    упоминание в заголовке — совсем другой вес, и решать это должен человек,
    а не мы за него.
    """
    head = str(title or "")[:MAX_CHARS]
    text = (head + "\n" + str(body or ""))[:MAX_CHARS]
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, names in (
        ("организация", organisations(text)),
        ("человек", people(text)),
        ("деньги", money(text)),
        ("место", places(text)),
    ):
        for name in names:
            key = (kind, normalize(name))
            row = out.get(key)
            if row is None:
                if len(out) >= MAX_PER_ITEM:
                    continue
                out[key] = {"вид": kind, "имя": name.strip(), "ключ": key[1],
                            "в_заголовке": normalize(name) in normalize(head), "раз": 1}
            else:
                row["раз"] += 1
    return list(out.values())


def save(conn: Any, item_id: int, title: str, body: str = "") -> int:
    """Записать сущности материала. Повторный вызов не удваивает упоминания."""
    from . import store  # noqa: PLC0415

    rows = extract(title, body)
    conn.execute("DELETE FROM mentions WHERE item_id = ?", (int(item_id),))
    for row in rows:
        conn.execute(
            "INSERT OR IGNORE INTO entities(kind, name, norm, created_at) VALUES(?,?,?,?)",
            (row["вид"], row["имя"], row["ключ"], store.now()),
        )
        found = conn.execute(
            "SELECT id FROM entities WHERE kind = ? AND norm = ?", (row["вид"], row["ключ"])
        ).fetchone()
        if found is None:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO mentions(item_id, entity_id, in_title, times) VALUES(?,?,?,?)",
            (int(item_id), int(found["id"]), 1 if row["в_заголовке"] else 0, int(row["раз"])),
        )
    conn.commit()
    return len(rows)


__all__ = ("KINDS", "MAX_PER_ITEM", "extract", "money", "normalize", "organisations",
           "people", "places", "save")
