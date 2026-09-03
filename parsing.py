"""Разбор трат из свободного текста.

Понимает сообщения вида:
    вчера хлеб 200
    01.09.2026
    Бургер 2800
    Мойка 700
    Непредвиденные расходы (котёл починить)
    12500
    2 сентября: такси 350, кофе 150₽
Строка с датой («вчера», «сегодня», «позавчера», «01.09», «01.09.2026», «2 сентября») задаёт дату
для последующих строк. Сумма может стоять в конце или в начале строки, с ₽/руб/р, с пробелами
(«12 500») и копейками; если строка без числа, а следующая — только число, они объединяются.
"""
import datetime
import re

DATE_WORDS = {"сегодня": 0, "вчера": 1, "позавчера": 2}
MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
CURRENCY = r"(?:₽|руб(?:лей|ля|\.)?|р\.?|rub)?"
NUM = r"\d{1,3}(?:[ \u00a0]\d{3})+|\d+"
AMOUNT_END = re.compile(rf"^(?P<name>.*?)[\s:\-–—]*(?P<amt>(?:{NUM})(?:[.,]\d{{1,2}})?)\s*{CURRENCY}\s*$", re.I)
AMOUNT_START = re.compile(rf"^(?P<amt>(?:{NUM})(?:[.,]\d{{1,2}})?)\s*{CURRENCY}\s*[\-–—:]?\s*(?P<name>.+?)\s*$", re.I)
AMOUNT_MID = re.compile(rf"^(?P<a>[^\d]+?)\s+(?P<amt>(?:{NUM})(?:[.,]\d{{1,2}})?)\s*{CURRENCY}\s+(?P<b>[^\d]+?)\s*$", re.I)
ONLY_AMOUNT = re.compile(rf"^(?P<amt>(?:{NUM})(?:[.,]\d{{1,2}})?)\s*{CURRENCY}\s*$", re.I)
DATE_NUMERIC = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$")
DATE_TEXT = re.compile(r"^(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?$", re.I)
MAX_AMOUNT = 100_000_000


def _amount(s: str):
    s = s.replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        v = int(round(float(s)))
    except ValueError:
        return None
    return v if 0 < v <= MAX_AMOUNT else None


def parse_date_token(token: str, today: datetime.date):
    """Дата из отдельного слова/строки или None."""
    t = token.strip().strip(":—–-").strip().lower()
    if t in DATE_WORDS:
        return today - datetime.timedelta(days=DATE_WORDS[t])
    m = DATE_NUMERIC.match(t)
    if m:
        d, mo, y = m.groups()
        y = int(y) if y else today.year
        if y < 100:
            y += 2000
        try:
            return datetime.date(y, int(mo), int(d))
        except ValueError:
            return "invalid"
    m = DATE_TEXT.match(t)
    if m:
        d, mon, y = m.groups()
        for key, num in MONTHS.items():
            if mon.startswith(key):
                try:
                    return datetime.date(int(y) if y else today.year, num, int(d))
                except ValueError:
                    return "invalid"
    return None


def _split_leading_date(line: str, today: datetime.date):
    """«вчера бургер 2800» / «01.09 кофе 150» -> (date|None|'invalid', остаток)."""
    parts = line.split(None, 1)
    if len(parts) == 2:
        d = parse_date_token(parts[0], today)
        if d is not None:
            return d, parts[1]
        # «2 сентября такси 350»
        m = re.match(r"^(\d{1,2}\s+[а-яё]+(?:\s+\d{4})?):?\s+(.+)$", line, re.I)
        if m:
            d = parse_date_token(m.group(1), today)
            if d is not None:
                return d, m.group(2)
    return None, line


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip(" \t:-–—•·*")
    return (name[:1].upper() + name[1:])[:100] if name else ""


def parse_line(line: str):
    """-> (name, amount) или None."""
    line = line.strip()
    m = AMOUNT_END.match(line)
    if m and _clean_name(m.group("name")):
        amt = _amount(m.group("amt"))
        if amt:
            return _clean_name(m.group("name")), amt
    m = AMOUNT_START.match(line)
    if m and _clean_name(m.group("name")) and not re.search(r"\d", m.group("name")[:1]):
        amt = _amount(m.group("amt"))
        if amt:
            return _clean_name(m.group("name")), amt
    m = AMOUNT_MID.match(line)          # «Мясо 7500 продукты» — сумма посередине
    if m:
        amt = _amount(m.group("amt"))
        name = _clean_name(m.group("a") + " " + m.group("b"))
        if amt and name:
            return name, amt
    return None


def parse_free_text(text: str, today: datetime.date):
    """-> (items, unparsed): items = [{"name","amount","date"}], unparsed = строки, которые не удалось разобрать."""
    items, unparsed = [], []
    date = today
    pending_name = None
    for raw in (text or "").splitlines():
        line = raw.strip().strip("•·*-–— ").strip()
        if not line:
            continue
        d = parse_date_token(line, today)
        if d == "invalid":
            unparsed.append(line)
            pending_name = None
            continue
        if d is not None:
            date = d
            pending_name = None
            continue
        # дата в начале строки, возможно двойная: «сегодня 05.09 хлеб 40» — явная дата важнее слова
        line_date, rest = None, line
        for _ in range(2):
            d2, rest2 = _split_leading_date(rest, today)
            if d2 is None:
                break
            line_date, rest = d2, rest2
        if line_date == "invalid":
            unparsed.append(line)
            pending_name = None
            continue
        if line_date:
            date = line_date          # дата в начале строки действует и на строки ниже
        use_date = date
        # «бургер 2800, мойка 700» в одной строке (запятая с пробелом или точка с запятой)
        parts = [p for p in re.split(r";\s*|,\s+", rest) if p.strip()]
        if len(parts) > 1:
            sub = [parse_line(p) for p in parts]
            if all(sub):
                items.extend({"name": n, "amount": a, "date": use_date} for n, a in sub)
                pending_name = None
                continue
        parsed = parse_line(rest)
        if parsed:
            items.append({"name": parsed[0], "amount": parsed[1], "date": use_date})
            pending_name = None
            continue
        m = ONLY_AMOUNT.match(rest)
        if m and pending_name:
            amt = _amount(m.group("amt"))
            if amt:
                items.append({"name": pending_name, "amount": amt, "date": use_date})
                pending_name = None
                continue
        if not re.search(r"\d", rest):
            pending_name = _clean_name(rest)
            continue
        unparsed.append(line)
        pending_name = None
    return items, unparsed
