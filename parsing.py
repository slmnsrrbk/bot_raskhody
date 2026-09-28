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
для последующих строк. Сумма может стоять в конце или в начале строки, с символом валюты, с любыми
разделителями разрядов («12 500», «12.500», «12'500»), с копейками и с сокращениями («1,5к», «2 тыс»);
если строка без числа, а следующая — только число, они объединяются.
"""
import datetime
import re

DATE_WORDS = {"сегодня": 0, "вчера": 1, "позавчера": 2}
MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}

# Валюта рядом с суммой: символ или слово -> код ISO. Пишется слитно или через пробел.
CURRENCIES = [
    (r"₽|руб(?:лей|лях|ля|\.)?|р\.?|rub", "RUB"),
    (r"\$|usd|долл(?:ар(?:ов|а|ы)?)?|бакс(?:ов|а)?", "USD"),
    (r"€|eur|евро", "EUR"),
    (r"₸|kzt|тенге", "KZT"),
    (r"₴|uah|гривен|гривны|грн", "UAH"),
    (r"₺|try|лир(?:ы|у)?", "TRY"),
    (r"£|gbp|фунт(?:ов|а)?", "GBP"),
    (r"¥|cny|юан(?:ей|я|и)?", "CNY"),
    (r"₾|gel|лари", "GEL"),
    (r"֏|amd|драм(?:ов|а)?", "AMD"),
    (r"₹|inr|рупий", "INR"),
    (r"฿|thb|бат(?:ов|а)?", "THB"),
    (r"aed|дирхам(?:ов|а)?", "AED"),
    (r"uzs|сум(?:ов|а)?", "UZS"),
]
CURRENCY = "(?P<cur>" + "|".join(p for p, _ in CURRENCIES) + ")?"
CUR_RE = [(re.compile(rf"^(?:{p})$", re.I), code) for p, code in CURRENCIES]

# Разделители разрядов: пробел (в т.ч. неразрывный и тонкий), апостроф, точка, запятая.
THIN = r" \u00a0\u202f\u2009'’"
NUM = rf"\d{{1,3}}(?:[{THIN}.,]\d{{3}})+(?:[.,]\d{{1,2}})?|\d+(?:[.,]\d{{1,2}})?"
MULT = r"(?:\s*(?:кк|к|k|тыс\.?|тысяч[иа]?|тысяча|млн\.?|миллион(?:ов|а)?)(?![а-яёa-z]))?"
AMT = rf"(?P<amt>(?:{NUM}){MULT})"
AMOUNT_END = re.compile(rf"^(?P<name>.*?)[\s:\-–—]*{AMT}\s*{CURRENCY}\s*$", re.I)
AMOUNT_START = re.compile(rf"^{AMT}\s*{CURRENCY}\s*[\-–—:]?\s*(?P<name>.+?)\s*$", re.I)
AMOUNT_MID = re.compile(rf"^(?P<a>[^\d]+?)\s+{AMT}\s*{CURRENCY}\s+(?P<b>[^\d]+?)\s*$", re.I)
ONLY_AMOUNT = re.compile(rf"^{AMT}\s*{CURRENCY}\s*$", re.I)
DATE_NUMERIC = re.compile(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?$")
DATE_TEXT = re.compile(r"^(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?$", re.I)
MAX_AMOUNT = 100_000_000
MULTIPLIERS = [("кк", 1_000_000), ("млн", 1_000_000), ("миллион", 1_000_000),
               ("тыс", 1000), ("тысяч", 1000), ("к", 1000), ("k", 1000)]


def _digits(s: str) -> str:
    """«1.000», «12 500», «1'234'567,89», «1,234.56» -> строка с точкой как десятичным разделителем."""
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", s):
        return s
    parts = re.split(r"[.,]", s)
    if len(parts) == 1:
        return s
    # только разряды, если каждая группа после первой — ровно три цифры, а первая не длиннее трёх
    if len(parts[0]) <= 3 and all(len(p) == 3 for p in parts[1:]):
        return "".join(parts)
    return "".join(parts[:-1]) + "." + parts[-1]


def to_amount(value):
    """Сумма из любой записи: «1.000», «12 500,50», «1,5к», «2 тыс», 1000 -> int или None."""
    s = str(value if value is not None else "").strip().lower()
    s = re.sub(rf"[{THIN}]", "", s)
    mult = 1
    for suffix, factor in MULTIPLIERS:
        if s.endswith(suffix) or s.endswith(suffix + "."):
            mult = factor
            s = s[:len(s) - len(suffix) - (1 if s.endswith(".") else 0)]
            break
    try:
        v = int(round(float(_digits(s)) * mult))
    except ValueError:
        return None
    return v if 0 < v <= MAX_AMOUNT else None


_amount = to_amount          # прежнее внутреннее имя


def _currency(token):
    if not token:
        return None
    for rx, code in CUR_RE:
        if rx.match(token.strip()):
            return code
    return None


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
    """-> (name, amount, currency|None) или None."""
    line = line.strip()
    m = AMOUNT_END.match(line)
    if m and _clean_name(m.group("name")):
        amt = to_amount(m.group("amt"))
        if amt:
            return _clean_name(m.group("name")), amt, _currency(m.group("cur"))
    m = AMOUNT_START.match(line)
    if m and _clean_name(m.group("name")) and not re.search(r"\d", m.group("name")[:1]):
        amt = to_amount(m.group("amt"))
        if amt:
            return _clean_name(m.group("name")), amt, _currency(m.group("cur"))
    m = AMOUNT_MID.match(line)          # «Мясо 7500 продукты» — сумма посередине
    if m:
        amt = to_amount(m.group("amt"))
        name = _clean_name(m.group("a") + " " + m.group("b"))
        if amt and name:
            return name, amt, _currency(m.group("cur"))
    return None


def _item(name, amount, date, currency=None):
    it = {"name": name, "amount": amount, "date": date}
    if currency:
        it["currency"] = currency          # валюта указана в тексте явно («100$»)
    return it


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
                items.extend(_item(n, a, use_date, c) for n, a, c in sub)
                pending_name = None
                continue
        parsed = parse_line(rest)
        if parsed:
            items.append(_item(parsed[0], parsed[1], use_date, parsed[2]))
            pending_name = None
            continue
        m = ONLY_AMOUNT.match(rest)
        if m and pending_name:
            amt = to_amount(m.group("amt"))
            if amt:
                items.append(_item(pending_name, amt, use_date, _currency(m.group("cur"))))
                pending_name = None
                continue
        if not re.search(r"\d", rest):
            pending_name = _clean_name(rest)
            continue
        unparsed.append(line)
        pending_name = None
    return items, unparsed
