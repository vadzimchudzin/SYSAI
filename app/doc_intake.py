"""Incoming documents (№18): what each one is, its requisites and deadlines with the words they rest on, the arithmetic,
and where it goes by the «что кому» rules of the Documents page.

A file a person sent is read (doctext.py) and analyzed here in the same background job: the model names the type and
pulls out the number, the date, the counterparty, the УНП, the sums and the VAT, the deadlines and the lines of the
table — each with a quote from the text. The code keeps a value only when its quote is in the text and holds that value,
checks the sums (без НДС + НДС = итого, НДС = база × ставка, количество × цена, сумма строк) and the УНП, and plans the
route: the first active rule that fits names who gets the document (a person, or the head of a department) and whether
its deadlines become their tasks with reminders. The plan is carried out after the AI employee answered the person who
sent the file (they hear what it is and where it goes), or by the background loop when the answer went before the
analysis was ready (then the person gets a short note).

Document.facts:
  {"kind": "invoice", "fields": {"number": {"value": "15", "quote": "Счёт № 15 от 12.11.2025"}, ...},
   "unverified": [{"field", "value", "quote"}], "deadlines": [{"what", "date", "quote"}], "items": [{"name", "qty",
   "price", "sum"}], "problems": ["…"], "route": {"status", "rule_id", "rule", "to_id", "to", "why", "tasks",
   "case_id", "task_ids", "reminder_ids", "at"}, "held_by": conversation message id, "told": bool, "at", "attempts"}
"""
import concurrent.futures
import datetime as dt
import json
import logging
import re
import threading
import time
from decimal import Decimal, InvalidOperation

from . import config, deadlines, llm, reminders, settings_store
from .db import (ConversationMessage, Department, DocRule, Document, DocumentTransfer, Employee, SessionLocal, Task,
                 now)

log = logging.getLogger("sysai.doc_intake")

KINDS = {"invoice": "Счёт", "act": "Акт выполненных работ", "reconciliation": "Акт сверки", "contract": "Договор",
         "waybill": "Накладная (ТТН, ТН)", "estimate": "Смета", "payment": "Платёжное поручение", "letter": "Письмо",
         "claim": "Претензия", "power": "Доверенность", "other": "Другое"}
FIELDS = {"number": "Номер", "date": "Дата", "counterparty": "Контрагент", "unp": "УНП", "amount": "Сумма с НДС",
          "amount_without_vat": "Сумма без НДС", "vat": "НДС", "vat_rate": "Ставка НДС", "currency": "Валюта",
          "subject": "Предмет", "object": "Объект"}
MONEY = ("amount", "amount_without_vat", "vat")
ROUTE_RU = {"planned": "будет передан после ответа", "sent": "передан", "self": "уже у получателя (он и прислал)",
            "none": "правила нет", "no_contact": "у получателя нет Telegram", "by_person": "передан по просьбе в чате",
            "in_case": "документ по делу — его передаёт дело", "sensitive": "конфиденциальный — по правилам не пересылается",
            "late": "передан (разобран после ответа)", "failed": "передать не удалось", "off": "разбор выключен",
            "ready": "готов к передаче по правилам (кнопка «Передать по правилам»)",
            "in_mail": "вложение письма — передаётся вместе с письмом (страница «Почта»)"}
WAIT_SECONDS = 45     # the answer to the person waits this long for the reading and the analysis of their files
LATE_SECONDS = 120    # a plan nobody carried out after the answer is carried out by the background loop
FRESH_HOURS = 48      # only files this fresh are analyzed and routed by the background loop
MAX_ATTEMPTS = 3
MAX_TEXT = 24_000
MAX_TASKS = 5
SYSTEM = (
    "Ты разбираешь входящий документ компании (Беларусь: суммы обычно в BYN, УНП — 9 цифр). Определи тип kind: "
    + ", ".join(f"{k} — {v}" for k, v in KINDS.items()) + ". "
    "Выпиши реквизиты — только то, что прямо написано в тексте, и у каждого точную цитату: кусок текста документа "
    "слово в слово, где это написано. Поля: number (номер документа), date (дата документа, ГГГГ-ММ-ДД), counterparty "
    "(организация или ИП, от которой документ: поставщик, подрядчик, заказчик — не наша компания company), unp (УНП "
    "контрагента), amount (итог с НДС), amount_without_vat, vat (сумма НДС), vat_rate (ставка НДС в процентах: 20, 10, "
    "0; «без НДС» — 0), currency (BYN, RUB, USD, EUR), subject (о чём документ, 3–8 слов, без цитаты), object (объект "
    "или адрес работ, если назван). Сроки deadlines — что и к какой дате нужно сделать по документу (оплатить, "
    "подписать и вернуть, поставить, устранить, ответить): date ГГГГ-ММ-ДД, what — коротко что, quote — слова документа; "
    "срок вида «в течение 5 банковских дней с даты счёта» посчитай от даты документа (банковские и рабочие дни — без "
    "субботы и воскресенья). Строки таблицы items (до 30): name, qty, price, sum — числа как в документе. Не "
    "выдумывай: чего нет в тексте, не включай. JSON: {\"kind\":\"…\",\"fields\":{\"number\":{\"value\":\"…\","
    "\"quote\":\"…\"}},\"deadlines\":[{\"what\":\"…\",\"date\":\"ГГГГ-ММ-ДД\",\"quote\":\"…\"}],\"items\":[{\"name\":"
    "\"…\",\"qty\":\"…\",\"price\":\"…\",\"sum\":\"…\"}]}")
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()
_POOL = []


class RuleError(ValueError):
    """A rule cannot be saved (Russian message for the panel)."""


def _testing() -> bool:
    return config.TESTING


def enabled() -> bool:
    return bool(settings_store.get("doc_intake"))


def _lock(document_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(int(document_id), threading.Lock())


def _pool():
    if not _POOL:
        _POOL.append(concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="doc-intake"))
    return _POOL[0]


def _read_now() -> bool:
    """Read a file the conversation waits for right away (tests turn this on; otherwise the intake job does it)."""
    return not _testing()


# ----------------------------------------------------------------------------- words, numbers, dates
_QUOTES = str.maketrans({c: " " for c in "«»“”„\"'‘’`"})


def norm(text: str) -> str:
    """Lower case, ё → е, no quote marks, one space, no spaces inside numbers («1 200,00» = «1200,00»)."""
    t = str(text or "").lower().replace("ё", "е").replace("\u00a0", " ").replace("\u202f", " ").translate(_QUOTES)
    t = re.sub(r"\s+", " ", t)
    return re.sub(r"(?<=\d) (?=\d)", "", t).strip()


def backed(quote: str, text_norm: str) -> bool:
    """The quote is in the text (case, quote marks, spaces and line breaks may differ)."""
    q = norm(quote)
    if len(q) < 2:
        return False
    if q in text_norm:
        return True
    words = q.split()
    if len(words) < 6:
        return False
    found = set(text_norm.split())
    return sum(w in found for w in words) / len(words) >= 0.9 and " ".join(words[:3]) in text_norm


_NUMBER = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:,\d{1,2})?|\d{1,3}(?:,\d{3})+\.\d{1,2}"
                     r"|\d+(?:[.,]\d{1,4})?)(?![\d])")


def _decimal(raw: str):
    t = re.sub(r"[\s\u00a0\u202f']", "", raw)
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if t.count(",") == 1 else t.replace(",", "")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def amount(value):
    """«12 000,50», «12000.50», 12000.5, «1 200 руб.» → Decimal; None when there is no number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    found = _NUMBER.search(str(value).replace("\u00a0", " "))
    return _decimal(found.group(0)) if found else None


def numbers_in(text: str) -> list:
    """Every number of the text. «3 150,00» in a table may be 3 and 150,00 as well as 3150,00: all of them are given."""
    out = []
    for m in _NUMBER.finditer(str(text or "").replace("\u00a0", " ").replace("\u202f", " ")):
        pieces = m.group(0).split(" ")
        for raw in [m.group(0)] + ([" ".join(pieces[i:]) for i in range(1, len(pieces))] + pieces[:-1]
                                   if len(pieces) > 1 else []):
            n = _decimal(raw)
            if n is not None:
                out.append(n)
    return out


def plain(number) -> str:
    """Decimal → «20», «0.5» (no exponent, no trailing zeros)."""
    return f"{Decimal(number).normalize():f}"


def money(value) -> str:
    """Decimal → «1 200,00»."""
    if value is None:
        return ""
    q = Decimal(value).quantize(Decimal("0.01"))
    whole, frac = f"{abs(q):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return ("-" if q < 0 else "") + " ".join(groups) + "," + frac


_MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "август": 8,
           "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
_DATE_NUM = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{4}|\d{2})(?!\d)")
_DATE_WORD = re.compile(r"(?<!\d)(\d{1,2})\s+(январ|феврал|март|апрел|мая|май|июн|июл|август|сентябр|октябр|ноябр|"
                        r"декабр)[а-я]*\s+(\d{4})")
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")


def dates_in(text: str) -> list:
    t = norm(text)
    out = []
    for d, m, y in _DATE_NUM.findall(t):
        y = int(y) + (2000 if len(y) == 2 else 0)
        try:
            out.append(dt.date(y, int(m), int(d)))
        except ValueError:
            pass
    for d, m, y in _DATE_WORD.findall(t):
        try:
            out.append(dt.date(int(y), _MONTHS[m], int(d)))
        except ValueError:
            pass
    for y, m, d in _DATE_ISO.findall(t):
        try:
            out.append(dt.date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return out


def parse_date(value):
    if isinstance(value, dt.date):
        return value
    found = dates_in(str(value or ""))
    return found[0] if found else None


def _ru_date(value) -> str:
    d = parse_date(value)
    return d.strftime("%d.%m.%Y") if d else str(value or "")


def currency(value) -> str:
    low = norm(value)
    if not low:
        return ""
    if re.search(r"usd|доллар|\$", low):
        return "USD"
    if re.search(r"eur|евро|€", low):
        return "EUR"
    if re.search(r"rub|рос|рф|₽|российск", low):
        return "RUB"
    if re.search(r"byn|byr|бел|руб|br", low):
        return "BYN"
    return ""


# ----------------------------------------------------------------------------- the model and the checks
def _ask(filename: str, caption: str, text: str) -> dict:
    from . import voice
    meta = {"filename": filename, "caption": caption[:500], "company": str(settings_store.get("company_name") or ""),
            "today": deadlines.today().isoformat()}
    raw = llm.chat(voice.model(), [{"role": "system", "content": SYSTEM},
                                   {"role": "user", "content": "<data>" + json.dumps(meta, ensure_ascii=False) +
                                    "</data>\nТекст документа:\n<data>" + text[:MAX_TEXT] + "</data>"}],
                   json_mode=True, temperature=0.0, max_tokens=3000, timeout=150)
    data = llm.parse_json(raw)
    return data if isinstance(data, dict) else {}


def _words_in(value: str, quote: str) -> bool:
    v, q = norm(value), norm(quote)
    if v and v in q:
        return True
    words = [w for w in re.findall(r"[а-яa-z0-9]+", v) if len(w) > 2]
    return bool(words) and sum(w in q for w in words) / len(words) >= 0.6


def _field(name: str, value, quote: str, text_norm: str):
    """The value as kept (string), or None when the quote does not back it."""
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()[:300]
    if name == "subject":
        return value[:200]
    if not backed(quote, text_norm):
        return None
    if name in MONEY:
        n = amount(value)
        return str(n) if n is not None and any(abs(n - x) < Decimal("0.005") for x in numbers_in(quote)) else None
    if name == "vat_rate":
        n = amount(value)
        if n is None or n < 0 or n > 30:
            return None
        if n == 0 and re.search(r"без\s+ндс|ндс\s+не\s+облага", norm(quote)):
            return "0"
        return plain(n) if any(abs(n - x) < Decimal("0.005") for x in numbers_in(quote)) else None
    if name == "date":
        d = parse_date(value)
        return d.isoformat() if d and d in dates_in(quote) else None
    if name == "unp":
        digits = re.sub(r"\D", "", value)
        return digits if digits and digits in re.sub(r"\D", "", quote) else None
    if name == "currency":
        code = currency(value) or currency(quote)
        return code or None
    return value if _words_in(value, quote) else None


_KIND_WORDS = (("сверк", "reconciliation"), ("сч", "invoice"), ("акт", "act"), ("догов", "contract"),
               ("наклад", "waybill"), ("ттн", "waybill"), ("смет", "estimate"), ("плат", "payment"), ("письм", "letter"),
               ("претенз", "claim"), ("доверен", "power"))


def _kind(raw) -> str:
    kind = str(raw or "").strip().lower().replace("ё", "е")
    if kind in KINDS:
        return kind
    return next((key for word, key in _KIND_WORDS if kind.startswith(word) or f" {word}" in f" {kind}"), "other")


def clean(raw: dict, text: str) -> dict:
    """What the model gave, kept only where the document backs it."""
    text_norm = norm(text)
    facts = {"kind": _kind(raw.get("kind")), "fields": {}, "unverified": [], "deadlines": [], "items": []}
    fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
    for name in FIELDS:
        item = fields.get(name)
        if isinstance(item, (str, int, float)) and not isinstance(item, bool):
            item = {"value": item, "quote": ""}
        if not isinstance(item, dict) or item.get("value") in (None, ""):
            continue
        quote = str(item.get("quote") or "").strip()[:400]
        kept = _field(name, item.get("value"), quote, text_norm)
        if kept is None:
            facts["unverified"].append({"field": name, "value": str(item.get("value"))[:200], "quote": quote[:200]})
        else:
            facts["fields"][name] = {"value": kept, "quote": quote}
    seen = set()
    for d in (raw.get("deadlines") if isinstance(raw.get("deadlines"), list) else [])[:10]:
        if not isinstance(d, dict):
            continue
        day, quote = parse_date(d.get("date")), str(d.get("quote") or "").strip()[:400]
        what = " ".join(str(d.get("what") or "").split())[:200]
        if day is None or not what or not backed(quote, text_norm) or (day, what) in seen:
            continue
        seen.add((day, what))
        facts["deadlines"].append({"what": what, "date": day.isoformat(), "quote": quote})
    present = set(numbers_in(text))
    for it in (raw.get("items") if isinstance(raw.get("items"), list) else [])[:30]:
        if not isinstance(it, dict):
            continue
        row = {"name": " ".join(str(it.get("name") or "").split())[:200]}
        for key in ("qty", "price", "sum"):
            n = amount(it.get(key))
            row[key] = str(n) if n is not None else ""
        if row["sum"] and Decimal(row["sum"]) in present:
            facts["items"].append(row)
    return facts


def value(facts: dict, name: str):
    item = ((facts or {}).get("fields") or {}).get(name) or {}
    return item.get("value")


def _near(a: Decimal, b: Decimal, tolerance: Decimal) -> bool:
    return abs(a - b) <= tolerance


def problems(facts: dict) -> list:
    """What does not add up, in plain Russian (an empty list when everything checks out or there is nothing to check)."""
    out = []
    total, base, vat = (amount(value(facts, k)) for k in ("amount", "amount_without_vat", "vat"))
    rate = amount(value(facts, "vat_rate"))
    cent = Decimal("0.02")
    if total is not None and base is not None and vat is not None and not _near(base + vat, total, cent):
        out.append(f"Сумма без НДС {money(base)} + НДС {money(vat)} = {money(base + vat)}, а итог в документе "
                   f"{money(total)}")
    if rate is not None and vat is not None:
        expected = None
        if base is not None:
            expected = base * rate / 100
        elif total is not None:
            expected = total * rate / (100 + rate)
        if expected is not None and not _near(expected, vat, max(Decimal("0.05"), abs(vat) * Decimal("0.002"))):
            out.append(f"НДС {plain(rate)}% от {money(base if base is not None else total)} — это "
                       f"{money(expected)}, а в документе {money(vat)}")
    lines = []
    for it in facts.get("items") or []:
        qty, price, s = amount(it.get("qty")), amount(it.get("price")), amount(it.get("sum"))
        if s is not None:
            lines.append(s)
        if qty is not None and price is not None and s is not None \
                and not _near(qty * price, s, max(cent, abs(s) * Decimal("0.001"))):
            out.append(f"Строка «{it.get('name') or '…'}»: {plain(qty)} × {money(price)} = {money(qty * price)}, "
                       f"а в документе {money(s)}")
    if len(lines) >= 2 and (base is not None or total is not None):
        summed = sum(lines, Decimal(0))
        tolerance = cent * len(lines)
        if not any(x is not None and _near(summed, x, tolerance) for x in (base, total)):
            target = base if base is not None else total
            out.append(f"Сумма строк {money(summed)} не сходится с {'суммой без НДС' if base is not None else 'итогом'} "
                       f"{money(target)}")
    unp = value(facts, "unp")
    if unp and not re.fullmatch(r"\d{9}", unp):
        out.append(f"УНП «{unp}» — должно быть 9 цифр")
    day = parse_date(value(facts, "date"))
    for d in facts.get("deadlines") or []:
        when = parse_date(d.get("date"))
        if day and when and when < day:
            out.append(f"Срок «{d['what']}» ({_ru_date(when)}) раньше даты документа ({_ru_date(day)})")
    if day and day > deadlines.today() + dt.timedelta(days=1):
        out.append(f"Дата документа {_ru_date(day)} — ещё не наступила")
    return out[:8]


def line(facts: dict) -> str:
    """«Счёт № 15 от 12.11.2025 · ООО «Монолит» · 1 200,00 BYN, НДС 200,00»."""
    if not facts:
        return ""
    head = KINDS.get(facts.get("kind") or "other", "Документ")
    if head == "Другое":
        head = "Документ"
    number, day = value(facts, "number"), value(facts, "date")
    if number:
        head += f" № {number}"
    if day:
        head += f" от {_ru_date(day)}"
    parts = [head]
    if value(facts, "counterparty"):
        parts.append(value(facts, "counterparty"))
    total = amount(value(facts, "amount")) or amount(value(facts, "amount_without_vat"))
    if total is not None:
        money_part = money(total) + (f" {value(facts, 'currency')}" if value(facts, "currency") else "")
        vat, rate = amount(value(facts, "vat")), amount(value(facts, "vat_rate"))
        if vat is not None and vat != 0:
            money_part += f", НДС {money(vat)}"
        elif rate == 0:
            money_part += ", без НДС"
        parts.append(money_part)
    return " · ".join(parts)


# ----------------------------------------------------------------------------- the rules «что кому»
def _head_of(department) -> Employee | None:
    members = [m for m in department.memberships if m.employee is not None and m.employee.active]
    for m in members:
        if m.is_primary and m.employee.telegram_chat_id:
            return m.employee
    return next((m.employee for m in members if m.employee.telegram_chat_id), None)


def _keywords(rule) -> list:
    return [w.strip().casefold() for w in re.split(r"[,;\n]", rule.keywords or "") if w.strip()]


def rule_label(s, rule) -> str:
    what = KINDS.get(rule.kind, "любой документ") if rule.kind else "любой документ"
    words = _keywords(rule)
    if words:
        what += " со словами " + ", ".join(f"«{w}»" for w in words[:5])
    to = "—"
    if rule.to_employee_id and s.get(Employee, rule.to_employee_id):
        to = s.get(Employee, rule.to_employee_id).name
    elif rule.to_department_id and s.get(Department, rule.to_department_id):
        to = f"отдел «{s.get(Department, rule.to_department_id).name}»"
    return f"{what} → {to}"


def _fits(rule, facts: dict, hay: str) -> bool:
    if rule.kind and rule.kind != facts.get("kind"):
        return False
    words = _keywords(rule)
    return not words or any(w in hay for w in words)


def plan_route(s, doc, facts: dict) -> dict:
    """Who gets the document by the rules (not carried out yet)."""
    if doc.case_id is not None:
        return {"status": "in_case"}
    if doc.sensitive:
        return {"status": "sensitive"}
    hay = " ".join([doc.filename or "", doc.caption or "", value(facts, "subject") or "", value(facts, "counterparty") or "",
                    (doc.text or "")[:6000]]).casefold()
    for rule in s.query(DocRule).filter(DocRule.active.is_(True)).order_by(DocRule.position, DocRule.id):
        if not _fits(rule, facts, hay):
            continue
        route = {"status": "planned", "rule_id": rule.id, "rule": rule_label(s, rule), "tasks": bool(rule.make_tasks)}
        person = None
        if rule.to_employee_id:
            person = s.get(Employee, rule.to_employee_id)
            if person is not None and not person.active:
                person = None
        elif rule.to_department_id:
            department = s.get(Department, rule.to_department_id)
            person = _head_of(department) if department is not None and department.active else None
            route["department"] = department.name if department is not None else ""
        if person is None:
            route.update(status="no_contact", why="в правиле нет получателя с Telegram (сотрудник или начальник отдела)")
        elif not person.telegram_chat_id:
            route.update(status="no_contact", to=person.name, to_id=person.id, why=f"у {person.name} нет Telegram-контакта")
        else:
            route.update(to=person.name, to_id=person.id)
            if doc.employee_id == person.id:
                route["self"] = True
        return route
    return {"status": "none", "why": "нет правила для такого документа"}


def rules_view(s) -> list:
    out = []
    for rule in s.query(DocRule).order_by(DocRule.position, DocRule.id):
        out.append({"id": rule.id, "kind": rule.kind or "", "kind_ru": KINDS.get(rule.kind, "любой") if rule.kind else "любой",
                    "keywords": rule.keywords or "", "label": rule_label(s, rule), "make_tasks": rule.make_tasks,
                    "active": rule.active, "to": (f"e:{rule.to_employee_id}" if rule.to_employee_id else
                                                  f"d:{rule.to_department_id}" if rule.to_department_id else ""),
                    "by": rule.updated_by or ""})
    return out


def targets(s) -> list:
    """Whom a rule can name: people with Telegram, and departments (their head gets it)."""
    out = [{"value": f"d:{d.id}", "label": f"Отдел «{d.name}» (начальнику)"}
           for d in s.query(Department).filter(Department.active.is_(True)).order_by(Department.name)]
    out += [{"value": f"e:{e.id}", "label": e.name + (f" — {e.position}" if e.position else "")}
            for e in s.query(Employee).filter(Employee.active.is_(True)).order_by(Employee.name)
            if e.telegram_chat_id]
    return out


def save_rule(*, rule_id=None, kind: str = "", keywords: str = "", to: str = "", make_tasks: bool = True,
              by: str = "") -> int:
    kind = kind if kind in KINDS else ""
    keywords = ", ".join(w.strip() for w in re.split(r"[,;\n]", keywords or "") if w.strip())[:300]
    m = re.fullmatch(r"([ed]):(\d+)", str(to or ""))
    if not m:
        raise RuleError("выберите, кому передавать: сотрудника или отдел")
    with SessionLocal() as s:
        if m.group(1) == "e":
            person = s.get(Employee, int(m.group(2)))
            if person is None or not person.active:
                raise RuleError("такого сотрудника нет в справочнике")
            if not person.telegram_chat_id:
                raise RuleError(f"у {person.name} нет Telegram-контакта — документ не дойдёт")
        elif s.get(Department, int(m.group(2))) is None:
            raise RuleError("такого отдела нет")
        rule = s.get(DocRule, int(rule_id)) if rule_id else None
        if rule is None:
            last = s.query(DocRule).order_by(DocRule.position.desc()).first()
            rule = DocRule(position=(last.position + 1) if last else 0)
            s.add(rule)
        rule.kind, rule.keywords, rule.make_tasks, rule.updated_by = kind, keywords, bool(make_tasks), (by or "")[:120]
        rule.to_employee_id = int(m.group(2)) if m.group(1) == "e" else None
        rule.to_department_id = int(m.group(2)) if m.group(1) == "d" else None
        s.commit()
        return rule.id


def rule_action(rule_id: int, action: str) -> None:
    with SessionLocal() as s:
        rule = s.get(DocRule, rule_id)
        if rule is None:
            raise RuleError("правило не найдено")
        if action == "toggle":
            rule.active = not rule.active
        elif action == "delete":
            s.delete(rule)
        elif action == "up":
            rules = s.query(DocRule).order_by(DocRule.position, DocRule.id).all()
            i = next(i for i, r in enumerate(rules) if r.id == rule.id)
            if i > 0:
                rules[i - 1], rules[i] = rules[i], rules[i - 1]
            for position, r in enumerate(rules):
                r.position = position
        else:
            raise RuleError("неизвестное действие")
        s.commit()


# ----------------------------------------------------------------------------- the analysis
def _skip_reason(doc) -> str:
    if doc.chat_id == "generated":
        return "сделан по шаблону"
    text = (doc.text or "").strip()
    if doc.text_status != "done" or len(text) < 60 or (text.startswith("[") and text.endswith("]")):
        return "нет текста" if doc.text_status != "failed" else "не удалось прочитать"
    return ""


def analyze(document_id: int, force: bool = False) -> str:
    """Ask the model, keep what the text backs, check the sums, plan the route. Returns the new facts_status."""
    with _lock(document_id):
        with SessionLocal() as s:
            doc = s.get(Document, document_id)
            if doc is None:
                return "missing"
            if doc.facts_status in ("done", "skipped", "old") and not force:
                return doc.facts_status
            if doc.text_status == "pending":
                return "pending"  # read first (doctext.py); the intake job or the background loop comes back
            reason = _skip_reason(doc)
            if reason:
                facts = dict(doc.facts or {})
                facts.update(skipped=reason, at=now().isoformat())
                doc.facts, doc.facts_status = facts, "skipped"
                s.commit()
                return "skipped"
            filename, caption, text = doc.filename or "", doc.caption or "", doc.text or ""
            before = dict(doc.facts or {})
            doc.facts, doc.facts_status = {**before, "working_since": now().isoformat()}, "working"
            s.commit()
        try:
            raw = _ask(filename, caption, text)
            error = ""
        except Exception as exc:  # noqa: BLE001  the model down: tried again by the background loop
            raw, error = None, str(exc)[:300]
            log.info("document %s not analyzed: %s", document_id, exc)
        with SessionLocal() as s:
            doc = s.get(Document, document_id)
            if raw is None:
                attempts = int(before.get("attempts") or 0) + 1
                facts = {**before, "attempts": attempts, "error": error}
                doc.facts, doc.facts_status = facts, ("failed" if attempts >= MAX_ATTEMPTS else "")
                s.commit()
                return doc.facts_status or "pending"
            facts = clean(raw, text)
            facts["problems"] = problems(facts)
            facts["at"], facts["attempts"] = now().isoformat(), int(before.get("attempts") or 0) + 1
            for key in ("held_by", "told", "route", "working_since", "mail_id"):
                if key in before:
                    facts[key] = before[key]
            route = facts.get("route") or {}
            if not route or route.get("status") in ("planned", "none", "no_contact", "ready"):  # not carried out: again
                planned = plan_route(s, doc, facts)
                if force and planned.get("status") == "planned" and route.get("status") != "planned":
                    planned["status"] = "ready"  # analyzed again from the panel: passed on by the button, not by itself
                facts["route"] = {**planned, "planned_at": now().isoformat()}
            doc.facts, doc.facts_status = facts, "done"
            s.commit()
    try:
        from . import entities
        entities.from_document(document_id)
    except Exception:  # noqa: BLE001  the cards of clients are a bonus, never a reason to fail the intake
        log.exception("document %s: client card not updated", document_id)
    return "done"


def process(document_id: int) -> str:
    """The background job right after a file came (cases.read_later): read it, then analyze it."""
    from . import doctext
    status = doctext.process(document_id)
    if not enabled():
        return status
    if status in ("done", "empty", "failed"):
        return analyze(document_id)
    return status


# ----------------------------------------------------------------------------- the conversation
def view(doc) -> dict:
    """What the conversation model gets about one received file (key «intake» of file_received)."""
    if not enabled():
        return {}
    status = doc.facts_status or ""
    if status in ("", "working"):
        return {"pending": True}
    facts = doc.facts or {}
    if status == "skipped":
        return {"not_analyzed": facts.get("skipped") or "нет текста"}
    if status == "failed":
        return {"not_analyzed": "не удалось разобрать (модель недоступна)"}
    if status == "old":
        return {}
    out = {"doc_type": KINDS.get(facts.get("kind") or "other", "Другое"),
           "requisites": {k: shown(k, v["value"]) for k, v in (facts.get("fields") or {}).items()},
           "deadlines": [{"what": d["what"], "date": d["date"],
                          **({"past": True} if parse_date(d["date"]) and parse_date(d["date"]) < deadlines.today() else {})}
                         for d in facts.get("deadlines") or []],
           "problems": facts.get("problems") or []}
    route = facts.get("route") or {}
    if route.get("status") == "planned" and route.get("to"):
        future = [d for d in facts.get("deadlines") or [] if parse_date(d["date"]) and parse_date(d["date"]) >= deadlines.today()]
        out["route"] = {"to": route["to"], "rule": route.get("rule") or "", **({"already_theirs": True} if route.get("self") else {}),
                        "tasks": [f"{d['what']} до {_ru_date(d['date'])}" for d in future[:MAX_TASKS]] if route.get("tasks") else [],
                        "reminders": bool(route.get("tasks") and future)}
    elif route.get("status") in ("sent", "self", "by_person", "late"):
        out["route"] = {"done": ROUTE_RU[route["status"]], "to": route.get("to")}
    else:
        out["route"] = {"to": None, "why": route.get("why") or ROUTE_RU.get(route.get("status"), "")}
    return out


def shown(name: str, raw) -> str:
    """A kept value as people write it: «12.11.2025», «1 200,00», «20%»."""
    if name == "date":
        return _ru_date(raw)
    if name in MONEY:
        n = amount(raw)
        return money(n) if n is not None else str(raw)
    if name == "vat_rate":
        return f"{raw}%"
    return str(raw)


def prepare(document_ids: list, row_id=None, wait: float = WAIT_SECONDS) -> dict:
    """Before the answer: the files of this message are read and analyzed (at most `wait` seconds). Returns
    {document_id: view}; a file not ready in time is finished in the background and the person is told then."""
    ids = [int(x) for x in dict.fromkeys(document_ids or [])][:30]
    if not ids or not enabled():
        return {}
    with SessionLocal() as s:
        for doc in s.query(Document).filter(Document.id.in_(ids)):
            if doc.facts_status != "old":
                doc.facts = {**(doc.facts or {}), "held_by": row_id}  # the conversation decides about this file
        s.commit()
    if _testing():
        for i in ids:
            process(i) if _read_now() else analyze(i)
    else:
        deadline, started = time.time() + wait, set()
        while time.time() < deadline:
            with SessionLocal() as s:
                rows = s.query(Document.id, Document.text_status, Document.facts_status).filter(Document.id.in_(ids)).all()
            waiting = [r for r in rows if r.facts_status in ("", "working")]
            if not waiting:
                break
            for r in waiting:  # read but not analyzed, and nobody is at it (the service restarted): start it
                if r.text_status in ("done", "empty", "failed") and r.facts_status == "" and r.id not in started:
                    started.add(r.id)
                    _pool().submit(analyze, r.id)
            time.sleep(1.0)
    out = {}
    with SessionLocal() as s:
        for doc in s.query(Document).filter(Document.id.in_(ids)):
            out[doc.id] = view(doc)
            if doc.facts_status == "done":
                doc.facts = {**(doc.facts or {}), "told": True}
        s.commit()
    return out


def after_reply(document_ids: list, actions: list, by: str = "") -> None:
    """The answer went: carry out the planned routes, except for files the person said where to send themselves."""
    delivered = set()
    for a in actions or []:
        if isinstance(a, dict) and a.get("type") == "deliver_files":
            delivered |= {int(x) for x in a.get("document_ids") or [] if str(x).isdigit()}
    for document_id in dict.fromkeys(int(x) for x in document_ids or []):
        try:
            carry_out(document_id, by=by, by_person=document_id in delivered)
        except Exception:  # noqa: BLE001
            log.exception("document %s not routed", document_id)


def _deadline_tasks(s, doc, facts: dict, person) -> tuple:
    """The deadlines of the document as the person's tasks, each with a reminder the working day before at 10:00."""
    task_ids, reminder_ids = [], []
    today = deadlines.today()
    head = line(facts) or (doc.filename or "документ")
    for d in (facts.get("deadlines") or [])[:MAX_TASKS]:
        day = parse_date(d.get("date"))
        if day is None or day < today:
            continue
        task = Task(meeting_id=None, title=f"{d['what'][:1].upper()}{d['what'][1:]} — {head}"[:500],
                    description=f"Срок из документа «{doc.filename}»: «{d.get('quote') or ''}»"[:4000],
                    assignee_id=person.id, assignee_name=person.name, deadline=day, deadline_source="stated",
                    deadline_quote=(d.get("quote") or "")[:500], status="sent", executor="person",
                    source_quote=f"Документ «{doc.filename}»"[:1000])
        s.add(task)
        s.flush()
        task_ids.append(task.id)
        due = _remind_at(day)
        if due is not None and reminders.reachable(person):
            reminder_ids.append(reminders.create(
                s, chat=str(doc.chat_id or ""), requested_by="разбор документов", due=due,
                topic=f"Срок по документу: {d['what']} до {_ru_date(day)}"[:300],
                details=f"{head}. Файл «{doc.filename}». В документе: «{d.get('quote') or ''}»",
                recipients=[{"ref": reminders.contact(person), "name": person.name, "employee_id": person.id,
                             "user_id": person.telegram_user_id or ""}]))
    return task_ids, reminder_ids


def _remind_at(day: dt.date):
    zone = reminders.zone()
    moment = now()
    before = day - dt.timedelta(days=1)
    while before.weekday() >= 5:
        before -= dt.timedelta(days=1)
    for when in (dt.datetime.combine(before, dt.time(10, 0), zone), dt.datetime.combine(day, dt.time(9, 0), zone)):
        utc = when.astimezone(dt.timezone.utc)
        if utc > moment + dt.timedelta(minutes=5) and utc <= moment + dt.timedelta(days=reminders.MAX_AHEAD_DAYS):
            return utc
    return None


def carry_out(document_id: int, by: str = "", by_person: bool = False, late: bool = False) -> dict:
    """Send the file to the person of the rule (a delivery case) and turn its deadlines into their tasks with reminders."""
    from . import cases
    with _lock(document_id):
        with SessionLocal() as s:
            doc = s.get(Document, document_id)
            if doc is None or doc.facts_status != "done":
                return {}
            facts = dict(doc.facts or {})
            route = dict(facts.get("route") or {})
            if route.get("status") != "planned":
                return route
            transferred = s.query(DocumentTransfer.id).filter_by(document_id=doc.id).first() is not None
            if by_person or doc.case_id is not None or transferred:
                route["status"] = "by_person" if (by_person or transferred) else "in_case"
            elif facts.get("mail_id") and not by:
                route["status"] = "in_mail"  # the letter goes to the person with its attachments (mail.py)
            elif doc.sensitive:
                route["status"] = "sensitive"
            person = s.get(Employee, route.get("to_id")) if route.get("to_id") else None
            if route["status"] == "planned" and (person is None or not person.active or not person.telegram_chat_id):
                route.update(status="no_contact", why="получателя нет в справочнике или у него нет Telegram")
            if route["status"] != "planned":
                facts["route"] = route
                doc.facts = facts
                s.commit()
                return route
            own = bool(route.get("self")) or doc.employee_id == person.id
            note = f"{line(facts) or doc.filename} — по правилу «{route.get('rule') or ''}»"
            person_id = person.id
            sender = by or (doc.employee.name if doc.employee else "")
        case_id = None
        if not own:
            try:
                case_id = cases.start_delivery(sender or "входящие документы", person_id, [document_id], False,
                                               note=note[:500], report=False)
            except cases.CaseError as exc:
                with SessionLocal() as s:
                    doc = s.get(Document, document_id)
                    facts = dict(doc.facts or {})
                    facts["route"] = {**route, "status": "failed", "why": str(exc)[:300]}
                    doc.facts = facts
                    s.commit()
                    return facts["route"]
        with SessionLocal() as s:
            doc = s.get(Document, document_id)
            person = s.get(Employee, person_id)
            facts = dict(doc.facts or {})
            task_ids, reminder_ids = _deadline_tasks(s, doc, facts, person) if route.get("tasks") else ([], [])
            route.update(status="self" if own else ("late" if late else "sent"), case_id=case_id, task_ids=task_ids,
                         reminder_ids=reminder_ids, at=now().isoformat(), by=(by or "")[:120])
            facts["route"] = route
            doc.facts = facts
            s.commit()
    if case_id is not None:
        if _testing():
            cases.advance(case_id)
        else:
            cases._pool().submit(cases.advance, case_id)
    return route


def _late_note(doc, facts: dict) -> str:
    route = facts.get("route") or {}
    text = f"Разобрал «{doc.filename}»: {line(facts) or KINDS.get(facts.get('kind'), 'документ')}."
    if facts.get("problems"):
        text += " Не сходится: " + "; ".join(facts["problems"][:3]) + "."
    if route.get("status") in ("sent", "late"):
        text += f" Отправил по правилу: {route.get('to')}" + (", поставил задачи по срокам" if route.get("task_ids") else "") + "."
    elif route.get("status") == "self" and route.get("task_ids"):
        text += " Поставил задачи по срокам."
    elif route.get("status") in ("none", "no_contact"):
        text += " Кому передать — правила для такого документа нет; скажи, если нужно."
    return text


def _deciding(s, facts: dict) -> bool:
    """The conversation that got this file is still answering (its plan and its note wait for it)."""
    held = facts.get("held_by")
    row = s.get(ConversationMessage, held) if held else None
    return (row is not None and row.status == "agent_pending" and row.created_at is not None
            and reminders.aware(row.created_at) > now() - dt.timedelta(minutes=30))


def run_pending() -> None:
    """Background loop: fresh files not analyzed yet (read later, the model was down, the job was cut off), plans
    nobody carried out, and a short note to the person whose answer went before the analysis was ready."""
    if not enabled():
        return
    since = now() - dt.timedelta(hours=FRESH_HOURS)
    stuck = (now() - dt.timedelta(minutes=10)).isoformat()
    with SessionLocal() as s:
        for doc in s.query(Document).filter(Document.created_at >= since, Document.facts_status == "working").limit(20):
            if str((doc.facts or {}).get("working_since") or "") < stuck:
                doc.facts_status = ""  # cut off (a restart): analyze again
        s.commit()
        todo = [d.id for d in s.query(Document.id).filter(Document.created_at >= since, Document.facts_status == "",
                                                         Document.text_status.in_(("done", "empty", "failed")))
                .order_by(Document.id).limit(5)]
    for document_id in todo:
        analyze(document_id)
    late = (now() - dt.timedelta(seconds=LATE_SECONDS)).isoformat()
    with SessionLocal() as s:
        due = []
        for doc in (s.query(Document).filter(Document.created_at >= since, Document.facts_status == "done")
                    .order_by(Document.id).limit(300)):
            facts = doc.facts or {}
            route = facts.get("route") or {}
            planned = route.get("status") == "planned" and str(route.get("planned_at") or "") < late
            untold = bool(facts.get("held_by")) and not facts.get("told") and str(facts.get("at") or "") < late
            if (planned or untold) and not _deciding(s, facts):
                due.append(doc.id)
    for document_id in due[:5]:
        route = carry_out(document_id, late=True)
        with SessionLocal() as s:
            doc = s.get(Document, document_id)
            facts = dict(doc.facts or {})
            if facts.get("told") or not facts.get("held_by") or not doc.chat_id or doc.chat_id == "generated":
                continue
            facts["told"] = True
            doc.facts = facts
            s.commit()
            text, chat = _late_note(doc, facts), doc.chat_id
        try:
            from . import userbot
            userbot.send_message(chat, text, answering=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("document %s: the sender not told: %s", document_id, exc)
        log.info("document %s handled after the answer: %s", document_id, (route or {}).get("status"))


# ----------------------------------------------------------------------------- the panel
def card(doc) -> dict:
    """The document page: what was found (with quotes), what does not add up, where it went."""
    facts = doc.facts or {}
    status = doc.facts_status or ""
    out = {"status": status, "kind": KINDS.get(facts.get("kind") or "", ""), "line": line(facts) if facts else "",
           "fields": [{"name": FIELDS[k], "value": (_ru_date(v["value"]) if k == "date" else
                                                     money(amount(v["value"])) if k in MONEY else
                                                     f"{v['value']}%" if k == "vat_rate" else v["value"]),
                       "quote": v.get("quote") or ""}
                      for k, v in (facts.get("fields") or {}).items() if k in FIELDS],
           "unverified": [{"name": FIELDS.get(u["field"], u["field"]), "value": u["value"], "quote": u.get("quote") or ""}
                          for u in facts.get("unverified") or []],
           "deadlines": [{"what": d["what"], "date": _ru_date(d["date"]), "quote": d.get("quote") or ""}
                         for d in facts.get("deadlines") or []],
           "rows": facts.get("items") or [], "problems": facts.get("problems") or [],
           "skipped": facts.get("skipped") or "", "error": facts.get("error") or "", "check": facts.get("check") or {}}
    route = facts.get("route") or {}
    out["route"] = {"status": route.get("status") or "", "status_ru": ROUTE_RU.get(route.get("status") or "", ""),
                    "to": route.get("to") or "", "rule": route.get("rule") or "", "why": route.get("why") or "",
                    "case_id": route.get("case_id"), "tasks": len(route.get("task_ids") or []),
                    "reminders": len(route.get("reminder_ids") or [])} if route else {}
    return out


def brief(doc) -> dict:
    """One line for the library list."""
    facts = doc.facts or {}
    if doc.facts_status != "done":
        return {}
    route = facts.get("route") or {}
    return {"line": line(facts), "problems": len(facts.get("problems") or []),
            "route": (f"→ {route['to']}" if route.get("to") and route.get("status") in ("sent", "late", "planned")
                      else ROUTE_RU.get(route.get("status") or "", ""))}


def mark(document_id: int, ok: bool, note: str = "", by: str = "") -> None:
    """The admin's check of one analysis («верно» / «есть ошибка»): the Documents page counts them (the intake is good
    when the requisites and the route are right on at least 9 of 10 real documents)."""
    with SessionLocal() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.facts_status != "done":
            raise RuleError("документ ещё не разобран")
        facts = dict(doc.facts or {})
        facts["check"] = {"ok": bool(ok), "note": " ".join(str(note or "").split())[:300], "by": (by or "")[:120],
                          "at": now().isoformat()}
        doc.facts = facts
        s.commit()


def checked(s) -> dict:
    """{"total", "ok", "wrong": [{"id", "filename", "note"}]} over the documents checked by hand."""
    total, right, wrong = 0, 0, []
    rows = (s.query(Document.id, Document.filename, Document.facts).filter(Document.facts_status == "done")
            .order_by(Document.id.desc()).limit(3000))
    for document_id, filename, facts in rows:
        check = (facts or {}).get("check")
        if not check:
            continue
        total += 1
        if check.get("ok"):
            right += 1
        else:
            wrong.append({"id": document_id, "filename": filename, "note": check.get("note") or ""})
    return {"total": total, "ok": right, "wrong": wrong[:10]}


def route_again(document_id: int, by: str = "") -> dict:
    """«Передать по правилам» in the panel: plan with the rules as they are now and carry it out."""
    with SessionLocal() as s:
        doc = s.get(Document, document_id)
        if doc is None or doc.facts_status != "done":
            raise RuleError("документ ещё не разобран")
        facts = dict(doc.facts or {})
        route = facts.get("route") or {}
        if route.get("status") in ("sent", "late", "self", "by_person"):
            raise RuleError("документ уже передан")
        facts["route"] = {**plan_route(s, doc, facts), "planned_at": now().isoformat()}
        doc.facts = facts
        s.commit()
    return carry_out(document_id, by=by)
