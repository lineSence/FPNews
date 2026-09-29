"""Норма всплеска: медиана и MAD за четыре недели, а не среднее.

Проверяем ровно то, ради чего менялся расчёт: что один громкий день в
прошлом не прячет такой же день сегодня и что у новой сущности норма не
выдумывается [NEWS-001], [CORE-019].
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from news import entities, store


def _conn(tmp_path: Path):
    return store.connect(tmp_path / "db.sqlite3")


def _день(сколько_назад: int) -> str:
    return (datetime.now().astimezone() - timedelta(days=сколько_назад)).isoformat(
        timespec="seconds")


def _упомянуть(conn, имя: str, дней_назад: int, сколько: int = 1,
               издания: tuple[str, ...] = ("fontanka",)) -> None:
    когда = _день(дней_назад)
    for номер in range(сколько):
        издание = издания[номер % len(издания)]
        item_id, _ = store.remember(
            conn, издание, "https://x/{}-{}-{}".format(имя, дней_назад, номер),
            "{} снова в новостях".format(имя), когда, published_at=когда)
        store.fill(conn, item_id, "", "Компания «{}» что-то сделала.".format(имя), когда)
        entities.save(conn, item_id, "{} снова в новостях".format(имя),
                      "Компания «{}» что-то сделала.".format(имя))


def test_один_громкий_день_не_прячет_сегодняшний(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    # Обычный фон: примерно раз в день. Плюс один очень громкий день в прошлом.
    for день in range(2, 26):
        _упомянуть(conn, "Метрострой", день, 1)
    _упомянуть(conn, "Метрострой", 14, 20)
    _упомянуть(conn, "Метрострой", 0, 8, издания=("fontanka", "dp", "rbc", "ria", "bumaga"))
    всплески = {str(row["имя"]).lower(): row for row in store.bursts(conn)}
    метро = next(row for имя, row in всплески.items() if "метрострой" in имя)
    assert метро["сейчас"] == 8 and метро["изданий"] == 5
    assert метро["норма"] <= 2, "медиана не поднимается из-за одного громкого дня"
    assert метро["отклонение"] >= 3


def test_у_новой_сущности_нормы_нет(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    _упомянуть(conn, "Мостотрест", 0, 3)
    всплески = {str(row["имя"]).lower(): row for row in store.bursts(conn)}
    новичок = next(row for имя, row in всплески.items() if "мостотрест" in имя)
    assert новичок["новое"] is True
    assert новичок["во_сколько_раз"] is None, "делить на неизвестный фон нечем"


def test_ровный_фон_не_всплеск(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for день in range(0, 26):
        _упомянуть(conn, "Водоканал", день, 2)
    имена = [str(row["имя"]).lower() for row in store.bursts(conn)]
    assert not any("водоканал" in имя for имя in имена), "стабильный фон — это не новость"


def test_нулевая_норма_без_none_в_сводке(tmp_path: Path) -> None:
    """Регрессия: о ком пишут реже чем через день, у того медиана фона — 0,
    множитель не определён, и сводка печатала «(×None)»."""
    from news import digest

    conn = _conn(tmp_path)
    _упомянуть(conn, "Ленэнерго", 10, 1)
    _упомянуть(conn, "Ленэнерго", 0, 4, издания=("fontanka", "dp"))
    строка = next(row for row in store.bursts(conn) if "ленэнерго" in str(row["имя"]).lower())
    assert строка["новое"] is False and строка["норма"] == 0
    assert строка["во_сколько_раз"] is None
    данные = {"часов": 24, "всего": 5, "ваше": [], "правки": [], "снятия": [],
              "всплески": [строка]}
    текст = digest.text(данные)
    assert "None" not in текст
    assert "обычно — ноль в день" in текст


def test_множитель_в_сводке_когда_норма_есть(tmp_path: Path) -> None:
    from news import digest

    данные = {"часов": 24, "всего": 1, "ваше": [], "правки": [], "снятия": [],
              "всплески": [{"имя": "Икс", "сейчас": 9, "новое": False,
                            "во_сколько_раз": 4.5}]}
    assert "(×4.5 к норме)" in digest.text(данные)


def test_окно_режется_по_времени_а_не_по_дате(tmp_path: Path) -> None:
    """Вчерашний день, задетый окном одной заметкой, не уходит в «сейчас»
    целиком: утренние заметки того же дня старше суток — это фон."""
    import pytest
    from datetime import timezone

    сейчас = datetime.now(timezone.utc)
    свежая = сейчас - timedelta(hours=23, minutes=50)
    полночь = свежая.replace(hour=0, minute=0, second=5, microsecond=0)
    if полночь >= сейчас - timedelta(hours=24, minutes=5):
        pytest.skip("около полуночи UTC дата свежей и старой заметки не совпадёт")
    conn = _conn(tmp_path)

    def _в(когда: datetime, номер: int) -> None:
        iso = когда.isoformat(timespec="seconds")
        item_id, _ = store.remember(conn, "fontanka", "https://x/okno-{}".format(номер),
                                    "Горводоканал в новостях", iso, published_at=iso)
        store.fill(conn, item_id, "", "Компания «Горводоканал» что-то сделала.", iso)
        entities.save(conn, item_id, "Горводоканал в новостях",
                      "Компания «Горводоканал» что-то сделала.")

    for номер in range(5):
        _в(полночь + timedelta(minutes=номер), номер)  # тот же день, но старше суток
    for номер in range(5, 8):
        _в(свежая + timedelta(minutes=номер), номер)   # в окне
    строки = [row for row in store.bursts(conn, порог=0)
              if "горводоканал" in str(row["имя"]).lower()]
    assert строки and строки[0]["сейчас"] == 3
