"""Who does a task: a person, or the AI employee itself.

The report model proposes an executor for every task. This module re-checks that choice with plain rules, because the
most common and most harmful mistake is to hand a light communication task to the person who is only its counterpart:
«нужно написать Вадиму и узнать статус» is work for the assistant (write to Vadim, wait, report back), not a task for
Vadim. The rules read the task title, description and quote and decide:

- action type: message (написать, сообщить, напомнить), ask (узнать, спросить, уточнить), request (запросить, взять
  у кого-то документ), forward (переслать, передать документ), produce (график, сводка, отчёт по цифрам) or do (any
  substantive work: подготовить, исправить, согласовать, съездить, позвонить);
- the doer: a person addressed directly («Андрей, напиши Ивану», «пусть Вадим подготовит», «с тебя, Мария») or none;
- counterparts: whom the assistant must contact («у Ани» is the source, «Пете» the recipient).

A communication task without an addressed doer goes to the assistant, with the counterpart as the first contact.
A task where a person is addressed as the doer stays with that person, even if it is a message. Nothing here invents
people: every counterpart is a directory employee matched by name in any grammatical case.
"""
import re

COMMUNICATION = ("message", "ask", "request", "forward")

# Stems after lower-casing and «ё» → «е». Order matters: the first matching group wins for the main action.
_FORWARD = ("пересла", "перешл", "передат", "передай", "передайте", "передаст", "переда", "отправ", "направ", "скин",
            "присла", "пришл", "перекин")
_REQUEST = ("запрос", "попрос", "истребова", "собрат", "собери", "соберите", "получить у", "взять у", "возьми у",
            "забрать у", "забери у", "запроси")
_ASK = ("узна", "спрос", "уточн", "выясн", "поинтересова", "свериться", "поговор")
_MESSAGE = ("написа", "напиш", "сообщ", "уведом", "напомн", "оповест", "предупред", "проинформир", "известит",
            "связаться", "свяжись", "свяжитесь", "поговорить с", "поговори с", "обсудить с")
_PRODUCE = ("в виде график", "построить график", "построй график", "нарисовать график", "график рост",
            "график динамик", "диаграм", "сводк", "свести ", "сведи", "свести в таблиц", "сделать таблиц",
            "отчет по цифр", "посчита", "подсчита")
# Work only a person can do, even when it involves other people.
_PERSON_ONLY = ("позвон", "созвон", "съезд", "съезж", "выезд", "встрет", "провести встреч", "подписа", "оплат",
                "закуп", "смонтир", "установ", "отремонт", "снять", "сним", "исправ", "разработ", "протестир",
                "подготов", "согласова", "проанализ", "реализ", "организ", "оформ", "составит", "состав ", "закрыт",
                "найти", "найд", "поиск", "разместит", "размести", "добав", "провер", "внедр", "настро", "доработ",
                "сделат", "сдела", "выполн", "решит", "реши ", "изучит", "изуч", "прораб", "откоррект", "переписат",
                "нарисова", "спроектир", "рассчита", "посчитать стоимост", "купит", "куп ")
# A person told to act: «Андрей, …», «пусть Вадим…», «Вадим должен…», «поручить Вадиму…», «с тебя, Мария».
_DOER_AFTER = ("пусть", "поручи", "поручить", "поручаем", "поручается", "назначить", "закрепить за", "за ")
_DOER_BEFORE = ("должен", "должна", "должны", "займется", "займись", "возьмет", "возьмется", "сделает", "подготовит",
                "напишет", "отправит", "узнает", "запросит", "передаст", "берет", "берется", "отвечает")
_ASSISTANT_WORDS = ("ассистент", "бот", "секретар", "ии ", "сисай", "sysai")

_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def _clean(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _stems(word: str) -> set:
    """Plausible stems of a Russian name in any case: «Вадиму», «у Ани», «с Татьяной», «Андрею»."""
    word = _clean(word)
    stems = {word}
    for ending in ("ами", "ями", "ому", "ему", "ой", "ей", "ою", "ею", "ом", "ем", "ам", "ям", "ах", "ях"):
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            stems.add(word[:-len(ending)])
    if len(word) >= 3 and word[-1] in "аяеыиоуюйь":
        stems.add(word[:-1])
    return stems


def _same(a: str, b: str) -> bool:
    return bool(_stems(a) & _stems(b))


def _names(employee) -> list:
    words = []
    for phrase in [getattr(employee, "name", "") or ""] + _aliases(employee):
        words.extend(w for w in _WORD.findall(phrase) if len(w) >= 3)
    return words


def _aliases(employee) -> list:
    try:
        return list(employee.alias_list())
    except AttributeError:
        return [a.strip() for a in str(getattr(employee, "aliases", "") or "").split(",") if a.strip()]


def _mentions(text: str, employees) -> list:
    """Directory employees named in the text, in order: (position, word, employee). Ambiguous words are skipped."""
    found = []
    for match in _WORD.finditer(text):
        word = match.group(0)
        if len(word) < 3 or not word[0].isupper():
            continue
        hits = [e for e in employees if any(_same(word, n) for n in _names(e))]
        if len(hits) == 1:
            found.append((match.start(), word, hits[0]))
    return found


def _has(text: str, stems) -> bool:
    return any(stem in text for stem in stems)


def _action(text: str) -> str:
    low = " " + _clean(text) + " "
    if _has(low, _PRODUCE) and not _has(low, _FORWARD + _MESSAGE):
        return "produce"
    if _has(low, _REQUEST) or re.search(r"\b(взять|получить|забрать|возьми|забери|получи)\b[^.?!]*\bу\s+[а-я]", low):
        # «взять у Ани акт и отправить Пете» is a forward with a source.
        return "forward" if _has(low, _FORWARD) else "request"
    if _has(low, _FORWARD) and re.search(r"(документ|акт|файл|договор|письм|счет|смет|отчет|скан|протокол|"
                                         r"презентац|таблиц|справк|чертеж|it\b|пдф|pdf)", low):
        return "forward"
    if _has(low, _ASK):
        return "ask"
    if _has(low, _MESSAGE):
        return "message"
    return "do"


def _person_only(text: str) -> bool:
    low = " " + _clean(text) + " "
    return _has(low, _PERSON_ONLY)


def _doer(quote: str, mentions) -> object:
    """The employee addressed as the one who must act, if any."""
    low = _clean(quote)
    for position, word, employee in mentions:
        before = low[max(0, position - 30):position]
        after = low[position + len(word):position + len(word) + 40]
        # Vocative: «Андрей, напиши…» / «Мария, с тебя креативы» — the name opens a clause and a comma follows.
        clause_start = position == 0 or re.search(r"[.!?:;—-]\s*$|^\s*$|\b(так|итак|значит|ну|давай|вот)\s*,?\s*$", before)
        if clause_start and after.startswith(","):
            return employee
        # Trailing vocative: «Скинь Пете файл, Андрей.»
        if re.search(r",\s*$", before) and re.match(r"\s*([.!?]|$)", after):
            return employee
        if re.search(r"(^|\s)(" + "|".join(re.escape(w.strip()) for w in _DOER_AFTER) + r")\s*$", before):
            return employee
        if any(after.lstrip(" ,").startswith(w) for w in _DOER_BEFORE):
            # «Вадим должен узнать у Ани» — the subject acts. A dative («Вадиму должны…») is not a subject.
            if not re.search(r"(у|ю|е|ам|ям)$", _clean(word)) or _clean(word) in {_clean(n) for n in _names(employee)}:
                return employee
    if re.search(r"\bс тебя\b", low) and mentions:
        return mentions[0][2]
    return None


_FIRST_PERSON = ("напишу", "узнаю", "спрошу", "поговорю", "позвоню", "сделаю", "подготовлю", "отправлю", "передам",
                 "запрошу", "возьму", "свяжусь", "уточню", "сам ", "сама ")


def _self_commitment(quote: str) -> bool:
    """The speaker takes it on («я поговорю с ними», «узнаю сам»): the speaker is the doer, not the assistant."""
    low = " " + _clean(quote) + " "
    return bool(re.search(r"\bя\b", low)) or any(" " + w in low for w in _FIRST_PERSON)


def _role(quote: str, position: int, word: str) -> str:
    low = _clean(quote)
    before = low[max(0, position - 12):position]
    if re.search(r"\bу\s*$", before):
        return "source"
    if re.search(r"\b(с|со)\s*$", before):
        return "target"
    if re.search(r"\b(от)\s*$", before):
        return "source"
    return "recipient"


def route(task: dict, employees) -> dict:
    """Return the corrected executor, doer and counterparts of one normalized task.

    `task` has title, description, quote, executor, employee_id. The result adds action_type, counterparts
    ([{role, employee_id, name}]), target_employee_id/name (first contact for the assistant) and routing_note
    (empty when the model's choice was kept).
    """
    title, description, quote = (str(task.get(k) or "") for k in ("title", "description", "quote"))
    text = " ".join(x for x in (title, description, quote) if x)
    by_id = {e.id: e for e in employees}
    action = _action(text)
    mentions = _mentions(quote, employees) or _mentions(title + ". " + description, employees)
    source_text = quote if _mentions(quote, employees) else title + ". " + description
    doer = _doer(source_text, mentions)
    assistant_named = _has(" " + _clean(text) + " ", _ASSISTANT_WORDS)
    counterparts = []
    for position, word, employee in mentions:
        if doer is not None and employee.id == doer.id:
            continue
        role = _role(source_text, position, word)
        if not any(c["employee_id"] == employee.id for c in counterparts):
            counterparts.append({"role": role, "employee_id": employee.id, "name": employee.name})
    result = {
        "executor": task.get("executor") if task.get("executor") in ("person", "assistant") else "person",
        "employee_id": task.get("employee_id"),
        "target_employee_id": task.get("target_employee_id"),
        "target_employee_name": task.get("target_employee_name") or "",
        "action_type": action,
        "counterparts": counterparts,
        "routing_note": "",
    }
    first = None
    if counterparts:
        order = {"source": 0, "target": 1, "recipient": 2} if action in ("request", "forward") else \
            {"recipient": 0, "target": 1, "source": 2}
        first = sorted(counterparts, key=lambda c: order.get(c["role"], 3))[0]

    # The quote is what was said; the title is the model's wording. A spoken «напомни Ивану про оплату» is a message
    # even though «оплата» is person-only work, so person-only verbs are read from the title unless the quote itself
    # asks to contact someone from the directory.
    quote_contact = _action(quote) in COMMUNICATION and bool(_mentions(quote, employees))
    person_only = _person_only(title) and not quote_contact
    communication = action in COMMUNICATION and not person_only and not _self_commitment(quote)
    if communication and not counterparts and result["executor"] != "assistant":
        # «Запросите протокол у заказчика»: nobody from the directory to contact; the assistant cannot do it.
        communication = False
    if assistant_named and (communication or action == "produce"):
        decision = "assistant"
    elif doer is not None:
        decision = "person"
    elif communication:
        decision = "assistant"
    elif result["executor"] == "assistant" and (action in COMMUNICATION or action == "produce"):
        # The model chose the assistant for a light task and nobody is addressed as the doer: keep it.
        decision = "assistant"
    else:
        decision = result["executor"] if result["executor"] == "person" else ("assistant" if action == "produce" else "person")

    if decision == "person":
        owner = doer.id if doer is not None else result["employee_id"]
        if result["executor"] == "assistant" and doer is not None:
            result["routing_note"] = f"исполнитель — {doer.name}: к нему обращаются напрямую"
        elif result["executor"] == "person" and doer is not None and result["employee_id"] not in (None, doer.id):
            result["routing_note"] = f"исполнитель исправлен на {doer.name}: к нему обращаются напрямую"
        result.update(executor="person", employee_id=owner if owner in by_id else None,
                      target_employee_id=None, target_employee_name="")
    else:
        if result["executor"] == "person":
            previous = by_id.get(result["employee_id"])
            who = f" ({previous.name} — тот, к кому нужно обратиться)" if previous is not None else ""
            result["routing_note"] = f"лёгкое поручение: выполняет ассистент сам{who}"
            if previous is not None and first is None:
                first = {"role": "recipient", "employee_id": previous.id, "name": previous.name}
                counterparts.append(first)
        result.update(executor="assistant", employee_id=None)
        if first is not None:
            result.update(target_employee_id=first["employee_id"], target_employee_name=first["name"])
    return result


def describe(action_type: str, counterparts: list) -> str:
    """One Russian line for the report and the assistant: what it does and with whom."""
    by_role = {}
    for c in counterparts or []:
        by_role.setdefault(c.get("role"), []).append(c.get("name") or "")
    source = ", ".join(by_role.get("source", []))
    recipient = ", ".join(by_role.get("recipient", []) + by_role.get("target", []))
    if action_type == "forward" and source and recipient:
        return f"взять у {source} и передать {recipient}"
    if action_type == "request" and (source or recipient):
        return f"запросить у {source or recipient}"
    if action_type == "ask" and (recipient or source):
        return f"узнать у {recipient or source}"
    if action_type == "message" and recipient:
        return f"написать {recipient}"
    return ""
