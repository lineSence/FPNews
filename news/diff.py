"""Сравнение двух редакций материала: что убрали, что дописали.

Отпечаток в `item_revisions` отвечает на вопрос «изменилось ли», и до шага 10
этого хватало для досылки. Для анализа этого мало: ценность представляет не
факт правки, а исчезнувшая формулировка — её никто, кроме нас, не сохранил
`[NEWS-008]`.

Три решения.

1. **Сравниваем по словам, а не по буквам.** Побуквенный диф на русском тексте
   даёт кашу из окончаний; пословный читается человеком и стоит столько же.
2. **Считаем на чистых функциях без базы.** Страница дифа обязана проверяться
   тестом без сокета и без SQLite, как и вся разметка `[CODE-003]`.
3. **Всё, что пришло со стороны, экранируется здесь же.** Диф собирает HTML, и
   единственный способ не забыть про экранирование — делать его в одном месте.
"""

from __future__ import annotations

import difflib
import html
import re
from typing import Any

# Слово вместе с идущими за ним пробелами: так склейка кусков не съедает
# разделители и текст остаётся читаемым.
WORD = re.compile(r"\S+\s*")
# Потолок на размер сравнения: две страницы по 200 КБ на одном ядре считать
# нельзя, а материалов такой длины у нас не бывает [CORE-025].
MAX_CHARS = 60_000


def words(text: str) -> list[str]:
    return WORD.findall(text or "")


def parts(old: str, new: str) -> list[tuple[str, str]]:
    """Диф как список пар («равно» | «убрано» | «добавлено», текст)."""
    left, right = words((old or "")[:MAX_CHARS]), words((new or "")[:MAX_CHARS])
    matcher = difflib.SequenceMatcher(None, left, right, autojunk=False)
    out: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            out.append(("равно", "".join(left[i1:i2])))
            continue
        if tag in ("replace", "delete"):
            out.append(("убрано", "".join(left[i1:i2])))
        if tag in ("replace", "insert"):
            out.append(("добавлено", "".join(right[j1:j2])))
    return [(kind, text) for kind, text in out if text]


def markup(old: str, new: str) -> str:
    """Диф в HTML: убранное в `<del>`, дописанное в `<ins>`."""
    chunks: list[str] = []
    for kind, text in parts(old, new):
        safe = html.escape(text)
        if kind == "убрано":
            chunks.append("<del>{}</del>".format(safe))
        elif kind == "добавлено":
            chunks.append("<ins>{}</ins>".format(safe))
        else:
            chunks.append(safe)
    return "".join(chunks)


def summary(old: str, new: str) -> dict[str, Any]:
    """Сухая сводка правки: сколько слов ушло, сколько пришло, знаков ±."""
    removed = added = 0
    for kind, text in parts(old, new):
        if kind == "убрано":
            removed += len(words(text))
        elif kind == "добавлено":
            added += len(words(text))
    return {
        "убрано_слов": removed,
        "добавлено_слов": added,
        "знаков": len(new or "") - len(old or ""),
        "менялось": bool(removed or added),
    }


def phrase(old: str, new: str) -> str:
    """Однострочное описание правки для списка: «−12 слов, +3 слова»."""
    stats = summary(old, new)
    if not stats["менялось"]:
        return "без изменений"
    bits = []
    if stats["убрано_слов"]:
        bits.append("−{} сл.".format(stats["убрано_слов"]))
    if stats["добавлено_слов"]:
        bits.append("+{} сл.".format(stats["добавлено_слов"]))
    bits.append("{}{} зн.".format("+" if stats["знаков"] > 0 else "", stats["знаков"]))
    return ", ".join(bits)


__all__ = ("MAX_CHARS", "markup", "parts", "phrase", "summary", "words")
