"""Checking dialogs: how well a model works as the AI employee, before it answers real people.

About eighty situations with the expected behaviour (an order, an order from the chat to confirm, a question back,
a question about the company, a person who does not answer, a document, a request beyond the person's rights…) go to
the chosen models in both ways of asking for actions: a JSON answer and function calls. Nothing is carried out and nobody gets a message — the decision passes
the same checks as in a real conversation (agent.decide) and is compared with what was expected. OpenRouter reports
the real price of every answer, so quality and the cost a month stand next to each other.

The director adds bad answers from real conversations with «как надо было»; a judge model compares those.
"""
import collections
import concurrent.futures
import dataclasses
import datetime as dt
import json
import logging
import re
import statistics
import threading
import time
import types

from . import agent, agent_skills, config, deadlines, dialog_memory, llm, reminders, settings_store, voice
from .db import AgentCheckCase, AgentCheckResult, AgentCheckRun, ConversationMessage, Employee, SessionLocal, now

log = logging.getLogger("sysai.checks")

DEFAULT_MODELS = ("openai/gpt-4.1-mini", "google/gemini-2.5-flash", "anthropic/claude-haiku-4.5")
MAX_MODELS = 6
WORKERS = 4
APPROVERS = ("@reviewer",)   # the checks never depend on who the reviewers of this installation are
ASSUMED_MONTH = 1000         # messages a month when the service has not talked enough yet to know
GREETING = re.compile(r"^\W*(привет|здравствуй|здравствуйте|добрый\s+(день|вечер)|доброе\s+утро|приветствую)", re.I)
WALL = re.compile(agent_skills.WALL, re.I)
INTERNAL = re.compile(r"known_facts|about_entities|\bcite\b|\bref\b|\[[mk]\d", re.I)  # marks the code should have taken


class CheckError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- the test company
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря")
WEEKDAYS_ACC = ("в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье")


def _day(n: int) -> dt.date:
    return deadlines.today() + dt.timedelta(days=n)


def _ru(day: dt.date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]}"


def _at(day: dt.date, hour: int, minute: int = 0) -> str:
    return f"{day.isoformat()} {hour:02d}:{minute:02d}"


PEOPLE = (
    {"id": 1, "name": "Анна Петрова", "position": "Главный бухгалтер", "department": "Бухгалтерия",
     "competencies": "1С, банк, сверки", "responsibilities": "Акты сверки, счета, оплата подрядчикам, закрывающие документы"},
    {"id": 2, "name": "Борис Смирнов", "position": "Инженер ПТО", "department": "ПТО", "competencies": "сметы, AutoCAD",
     "responsibilities": "Сметы, КС-2 и КС-3, исполнительная документация"},
    {"id": 3, "name": "Виктор Иванов", "position": "Снабженец", "department": "Снабжение", "competencies": "",
     "responsibilities": "Закупки материалов, доставка на объекты, машины и водители"},
    {"id": 4, "name": "Ольга Кузнецова", "position": "Офис-менеджер", "department": "Офис", "competencies": "",
     "responsibilities": "Документооборот, курьеры, переговорная, канцелярия"},
    {"id": 5, "name": "Сергей Волков", "position": "Электрик", "department": "Монтаж", "competencies": "допуск по электробезопасности",
     "responsibilities": "Электромонтаж, проверка проводки, щиты"},
    {"id": 6, "name": "Сергей Белов", "position": "Менеджер по продажам", "department": "Продажи", "competencies": "",
     "responsibilities": "Клиенты, коммерческие предложения, выставки"},
    {"id": 7, "name": "Дмитрий Орлов", "position": "Прораб", "department": "Монтаж", "competencies": "",
     "responsibilities": "Объект на Ленина 12, бригады, график работ", "unreachable": True},
)
# id, title, assignee, status, deadline in days (None = no deadline)
TASKS = (
    (101, "Подготовить смету на кровлю", 2, "sent", None),
    (102, "Собрать КС-2 за октябрь", 2, "in_progress", 3),
    (103, "Исполнительная по объекту на Ленина 12", 2, "sent", 7),
    (111, "Акт сверки с ООО «Монолит»", 1, "in_progress", 2),
    (112, "Оплатить счёт за бетон", 1, "sent", 1),
    (121, "Доставка арматуры на Ленина 12", 3, "sent", 2),
    (131, "Заказать курьера за оригиналами договоров", 4, "sent", 1),
    (141, "Заменить щит в бытовке", 5, "in_progress", 4),
    (201, "Подготовить КС-3 по Ленина 12", None, "draft", None),
    (202, "Сверка взаиморасчётов с ООО «Монолит» за квартал", None, "draft", None),
    (203, "Проверить проводку в офисе", None, "draft", None),
    (205, "Забрать справку из налоговой", None, "draft", None),
    (206, "Свести акты за октябрь в таблицу", None, "draft", None),
)
MEETING = {"id": 7, "title": "Планёрка по объектам"}
COMPANY_FACT = {"type": "Компания", "about": "Компания",
                "text": "Строительная компания: ремонт и строительство коммерческих объектов. Офис: г. Борисов, ул. 1 Июля, 6. "
                        "Рабочий день с 9:00 до 18:00. Объекты: Ленина 12, склад на Заводской.",
                "source": "профиль компании"}
ROLES = {  # who writes: the employee in the directory (or nobody) and the rights
    "employee": {"employee": 2, "sender": "Борис"},
    "accountant": {"employee": 1, "sender": "Анна"},
    "full": {"full": True, "sender": "Игорь"},
    "reviewer": {"reviewer": True, "full": True, "sender": "Ирина"},
    "director": {"reviewer": True, "full": True, "director": True, "sender": "Вадим"},
    "reviewer_dm": {"reviewer": True, "director_mode": True, "sender": "Ирина"},  # a director is named: orders are theirs
    "head": {"employee": 7, "sender": "Дмитрий", "scope": "department", "team": (5, 7)},  # orders his department only
    "full_noremind": {"full": True, "sender": "Игорь", "skills_off": ("remind", "cancel_reminder")},
}
ROLE_RU = {"employee": "сотрудник", "accountant": "сотрудник", "full": "полный доступ", "reviewer": "проверяющий",
           "director": "директор", "reviewer_dm": "проверяющий при директоре", "head": "руководитель отдела",
           "full_noremind": "полный доступ без напоминаний"}
REFER_TO = [{"name": "Ирина Соколова", "role": "проверяющий"}]  # whom to go to with what is beyond the rights
GUARANTEE = {"id": 31, "title": "Гарантийное письмо об оплате", "when": "Письмо заказчику или поставщику: гарантируем "
             "оплату суммы до даты", "output": "DOCX и PDF",
             "fields": [{"name": "Дата", "hint": "дата письма; сегодня, если не сказали"},
                        {"name": "Получатель", "hint": "организация, которой пишем"},
                        {"name": "Сумма", "hint": "сумма в рублях, цифрами"},
                        {"name": "Срок оплаты", "hint": "до какого числа оплатим"}]}
MEMO = {"id": 32, "title": "Служебная записка", "when": "Внутренняя записка директору от сотрудника", "output": "DOCX",
        "fields": [{"name": "От кого"}, {"name": "Тема"}, {"name": "Текст", "hint": "суть записки, 2–5 предложений"}]}
DOC_TEMPLATES = [GUARANTEE, MEMO]


def _person_row(employee_id: int) -> dict:
    return next(p for p in PEOPLE if p["id"] == employee_id)


def _task_view(task: tuple, employee_view: bool) -> dict:
    task_id, title, assignee, status, days = task
    view = {"id": task_id, "title": title, "description": "", "status": status,
            "deadline": _day(days).isoformat() if days is not None else None, "can_set_deadline": days is None,
            "priority": "normal", "meeting": MEETING["title"], "meeting_id": MEETING["id"]}
    if employee_view:
        view["quote"] = ""
    return view


def _meeting(brief: bool = False) -> dict:
    view = {"id": MEETING["id"], "title": MEETING["title"], "status": "delivered", "date": _day(-3).isoformat(),
            "summary": "Обсудили смету на кровлю, КС-2 за октябрь, доставку арматуры и сверку с «Монолитом»."}
    if not brief:
        view["decisions"] = ["Смету на кровлю готовит Борис", "Арматуру везём на Ленина 12 до конца недели"]
    return view


def build_ctx(role: str, *, history=(), talking=None, voice=False, voice_problem=None) -> tuple:
    """(ctx, person) in the shape agent._context gives the model, for the test company."""
    flags = ROLES[role]
    employee_row = _person_row(flags["employee"]) if flags.get("employee") else None
    employee = types.SimpleNamespace(id=employee_row["id"], name=employee_row["name"]) if employee_row else None
    reviewer, full, director = bool(flags.get("reviewer")), bool(flags.get("full")), bool(flags.get("director"))
    director_mode = bool(flags.get("director_mode"))
    off = tuple(flags.get("skills_off") or ())
    person = agent_skills.Person(employee=employee, reviewer=reviewer, full=full and not (director_mode and not director),
                                 director=director, scope=flags.get("scope", ""), team=frozenset(flags.get("team") or ()),
                                 skills=frozenset(n for n in agent_skills.SKILLS if n not in off) if off else None,
                                 switches=False)  # the test company: the Skills page switches do not apply
    talking = bool(history) if talking is None else talking
    ctx = {"today": deadlines.today().isoformat(), "now": reminders.now_view(), "sender_name": flags["sender"],
           "voice_message": bool(voice), "voice_problem": voice_problem,
           "roles": [r for r, on in (("employee", employee is not None), ("full_access", full and not reviewer),
                                     ("reviewer", reviewer), ("director", director)) if on],
           "director_mode": director_mode,
           "conversation": {"already_talking": talking, "minutes_since_last_message": 2 if talking else None,
                            "messages_so_far": len(history)},
           "memory": None,
           "history": [{"from": who, "text": text} for who, text in history]}
    facts = [COMPANY_FACT]
    if employee is not None:
        tasks = [t for t in TASKS if t[2] == employee.id]
        ctx["employee"] = {"name": employee.name, "position": employee_row["position"],
                           "responsibilities": employee_row["responsibilities"],
                           "departments": [{"name": employee_row["department"], "role": "", "responsibilities": ""}],
                           "tasks": [_task_view(t, True) for t in tasks], "done_recently": []}
        ctx["meetings"] = [_meeting()]
        ctx["waiting_for_reviewer"] = []
        ctx["notes"] = []
        ctx["assistant_waiting_for_you"] = []
        facts.append({"type": "Сотрудник", "about": employee.name,
                      "text": f"{employee.name} — {employee_row['position']}: {employee_row['responsibilities']}"})
    ctx["my_cases"] = []
    if full:
        facts += [{"type": "Сотрудник", "about": p["name"], "text": f"{p['name']} — {p['position']}: {p['responsibilities']}"}
                  for p in PEOPLE]
    ctx["known_facts"] = facts
    if full:
        ctx["team"] = [{"id": p["id"], "name": p["name"], "position": p["position"], "department": p["department"],
                        "competencies": p["competencies"] or None, "responsibilities": p["responsibilities"],
                        "departments": [{"name": p["department"], "role": "", "responsibilities": ""}],
                        "context": None, "reachable": not p.get("unreachable"),
                        "open_tasks": [_task_view(t, False) for t in TASKS if t[2] == p["id"]],
                        "done": 2, "blocked": 0} for p in PEOPLE]
        ctx["unassigned_tasks"] = [_task_view(t, False) for t in TASKS if t[2] is None]
        ctx["assistant_tasks"] = [{"id": 301, "title": "Сводка по закупкам за неделю", "status": "assistant_failed",
                                   "meeting_id": MEETING["id"]}]
        ctx["notes"] = [{"about": "Борис Смирнов", "kind": "commitment", "text": "Обещал смету на кровлю к концу недели",
                         "when": _day(-3).isoformat()}]
        ctx["recent_meetings"] = [_meeting(brief=True)]
        ctx["recent_cases"] = []
        ctx["recent_files"] = []
    scope = person.order_scope
    if not full and scope in ("all", "department"):  # the people they may give orders to (department: with their tasks)
        ctx["team"] = [{"id": p["id"], "name": p["name"], "position": p["position"], "department": p["department"],
                        "responsibilities": p["responsibilities"],
                        "departments": [{"name": p["department"], "role": "", "responsibilities": ""}],
                        "reachable": not p.get("unreachable"),
                        **({"open_tasks": [_task_view(t, False) for t in TASKS if t[2] == p["id"]]}
                           if scope == "department" else {})}
                       for p in PEOPLE if scope == "all" or p["id"] in person.team]
    ctx["order_scope"] = scope
    if scope != "all":
        ctx["refer_to"] = REFER_TO
    if off:
        ctx["skills_off"] = [agent_skills.SKILLS[n].title for n in agent_skills.SKILLS
                             if n not in agent_skills.PAIRED and not agent_skills.allowed_skill(n, person)]
    ctx["reminders_pending"] = []
    if reviewer or director:
        ctx["cases_awaiting_decision"] = []
    if reviewer:
        ctx["meetings_awaiting_approval"] = []
        ctx["open_questions_from_employees"] = []
    return ctx, person


# ----------------------------------------------------------------------------- what is expected
def nonempty(value) -> bool:
    return bool(str(value or "").strip())


def has(pattern: str, example: str | None = None):
    """A text field that has to say this; `example` is what a perfect model would give (the tests use it)."""
    def check(value) -> bool:
        return bool(re.search(pattern, str(value or ""), re.I))
    if example is not None:
        check.example = example
    return check


def _same(expected, value) -> bool:
    if callable(expected):
        return bool(expected(value))
    if isinstance(expected, (list, tuple, set)):
        return isinstance(value, list) and sorted(map(str, expected)) == sorted(map(str, value))
    return expected == value


def _matches(spec: dict, action: dict) -> bool:
    return action.get("type") == spec["type"] and all(_same(v, action.get(k)) for k, v in spec.items() if k != "type")


def filled(template: dict, expected: dict, example: dict):
    """The values of make_document: every field of the template filled, the named ones as expected. `.example` is what
    a perfect model would give (the tests check that it passes the real checks)."""
    names = [f["name"] for f in template["fields"]]

    def check(values) -> bool:
        if not isinstance(values, dict):
            return False
        values = {" ".join(str(k).split()): v for k, v in values.items()}
        return all(nonempty(values.get(n)) for n in names) and all(_same(v, values.get(k)) for k, v in expected.items())
    check.example = {n: example.get(n, "по данным разговора") for n in names}
    return check


def _show(spec: dict) -> str:
    args = ", ".join(f"{k}={'…' if callable(v) else json.dumps(v, ensure_ascii=False)}" for k, v in spec.items() if k != "type")
    return f"{spec['type']}({args})"


@dataclasses.dataclass
class Scenario:
    id: str
    group: str
    title: str
    role: str
    text: str
    actions: tuple = ()          # every one of them must be among the actions (only the listed fields are compared)
    allowed: tuple = ("note",)   # other skills that are fine here; anything else is a mistake
    forbid: tuple = ()           # specs of actions that must not be there (even if their skill is allowed)
    question: bool = False       # the reply has to ask something
    or_question: bool = False    # the actions, or a question instead of acting
    reply: bool = True           # some reply is needed
    reply_has: tuple = ()
    reply_not: tuple = ()
    short: bool = False          # a few words are enough («ок»)
    max_questions: int | None = None  # no more questions than this in one reply (one question at a time)
    max_len: int = 0             # the reply is no longer than this (characters; 0 = no limit beyond the generic one)
    history: tuple = ()
    talking: bool | None = None
    voice: bool = False
    voice_problem: str | None = None
    extra: dict = dataclasses.field(default_factory=dict)  # ctx keys of this situation (replace the company's)
    archive: tuple = ()          # older messages, not shown: (days ago, "человек" | "ты", text); the model may recall them
    older: int = 0               # how many older messages there are (when not the archive)

    def build(self) -> tuple:
        ctx, person = build_ctx(self.role, history=self.history, talking=self.talking, voice=self.voice,
                                voice_problem=self.voice_problem)
        older = self.older or len(self.archive)
        if older:
            ctx["conversation"]["older_not_shown"] = older
            ctx["conversation"]["messages_so_far"] += older
        for key, value in self.extra.items():
            ctx[key] = value(ctx) if callable(value) else value
        return ctx, person, self.text

    def recall(self, query: str) -> list:
        """What the model finds when it asks to remember: the same search as in a real chat, over the archive."""
        rows = [types.SimpleNamespace(id=i + 1, role="user" if who == "человек" else "assistant", text=text,
                                      created_at=now() - dt.timedelta(days=days))
                for i, (days, who, text) in enumerate(self.archive)]
        return dialog_memory.search(query, rows)

    def evaluate(self, decision: dict, ctx: dict) -> list:
        """Why the decision is not what was expected ([] = passed)."""
        reasons = []
        reply = str(decision.get("reply") or "")
        actions = [a for a in decision.get("actions") or [] if isinstance(a, dict)]
        asked = "?" in reply
        missing = [spec for spec in self.actions if not any(_matches(spec, a) for a in actions)]
        world = [a for a in actions if a.get("type") != "note"]
        if missing and not (self.or_question and asked and not world):
            for spec in missing:
                same_kind = [a for a in actions if a.get("type") == spec["type"]]
                reasons.append(("сделал не так: " if same_kind else "не сделал: ") + _show(spec))
        expected_kinds = {spec["type"] for spec in self.actions} | set(self.allowed)
        extra = sorted({a.get("type") for a in actions} - expected_kinds)
        if extra:
            reasons.append("лишнее действие: " + ", ".join(str(x) for x in extra))
        for spec in self.forbid:
            if any(_matches(spec, a) for a in actions):
                reasons.append("нельзя было: " + _show(spec))
        if self.question and not asked:
            reasons.append("нужно было переспросить, а вопроса нет")
        if self.reply and not reply.strip():
            reasons.append("пустой ответ")
        if self.short and len(reply) > 220:
            reasons.append(f"слишком длинно для короткой реплики ({len(reply)} символов)")
        if self.max_questions is not None and voice.questions(reply) > self.max_questions:
            reasons.append(f"несколько вопросов сразу ({voice.questions(reply)}), а нужен один")
        if self.max_len and len(reply) > self.max_len:
            reasons.append(f"слишком длинно ({len(reply)} символов, нужно до {self.max_len})")
        for pattern in self.reply_has:
            if not re.search(pattern, reply, re.I):
                reasons.append(f"в ответе нет нужного ({pattern})")
        for pattern in self.reply_not:
            found = re.search(pattern, reply, re.I)
            if found:
                reasons.append(f"в ответе лишнее: «{found.group(0)}»")
        return reasons + generic(decision, ctx)


def generic(decision: dict, ctx: dict) -> list:
    """What no answer may do, whatever the situation."""
    reasons = []
    reply = str(decision.get("reply") or "")
    wall = WALL.search(reply)
    if wall:
        reasons.append(f"отказ «стеной»: «{wall.group(0)}»")
    if (ctx.get("conversation") or {}).get("already_talking") and GREETING.search(reply):
        reasons.append("поздоровался посреди разговора")
    promises = agent.unbacked_promises(reply, decision.get("actions") or [], ctx)
    if promises:
        reasons.append("обещание без действия: «" + promises[0][:120] + "»")
    stiff = voice.officialese(reply)
    if stiff:
        reasons.append("канцелярит: «" + "», «".join(stiff[:3]) + "»")
    internal = INTERNAL.search(reply)
    if internal:
        reasons.append(f"служебное в ответе: «{internal.group(0)}»")
    if voice.questions(reply) >= 3:
        reasons.append(f"слишком много вопросов сразу ({voice.questions(reply)})")
    address = (ctx.get("employee") or {}).get("address") or {}
    pronouns = {"вы": (r"\b(ты|тебе|тебя|тобой|твой|твоя|твоё|твое|твои|твоих|твоим)\b", "на «ты»"),
                "ты": (r"\b(вы|вам|вас|вами|ваш|ваша|ваше|ваши|ваших|вашим)\b", "на «вы»")}.get(address.get("form"))
    if pronouns:
        found = re.search(pronouns[0], reply, re.I)
        if found:
            reasons.append(f"обратился {pronouns[1]} («{found.group(0)}»), а в карточке — на «{address['form']}»")
    return reasons


# ----------------------------------------------------------------------------- the situations
def _files(*names, first=501) -> list:
    return [{"document_id": first + i, "filename": name, "kind": "document", "size": 120_000 + i} for i, name in enumerate(names)]


def _case(case_id: int, title: str, person: str, expects: str, since: str, events: list, role: str) -> dict:
    return {"id": case_id, "title": title, "status": "ждёт ответа" if expects == "ответ" else "ждёт документа", "open": True,
            "waiting_for": [{"person": person, "expects": expects, "since": since}],
            "last_events": [{"at": at, "text": text} for at, text in events], "requested_by": "Вадим", "your_role": role}


def scenarios() -> list:
    """The built-in situations (dates are counted from today)."""
    d1, d3, d4, d10 = _day(1), _day(3), _day(4), _day(10)
    files2 = _files("акт_сверки.pdf", "смета_кровля.xlsx")
    files3 = _files("акт_сверки.pdf", "смета_кровля.xlsx", "фото_объекта.jpg")
    talk = (("человек", "Привет! Вопрос по смете на кровлю"), ("ты", "Привет! Слушаю"))
    question_41 = [{"id": 41, "employee": "Борис Смирнов", "question": "Какой бюджет на смету на кровлю?",
                    "task": "Подготовить смету на кровлю"}]
    meeting_8 = {"id": 8, "title": "Планёрка 10 ноября", "status": "awaiting_approval", "date": _day(-1).isoformat(),
                 "summary": "Сроки по кровле, оплата бетона, доставка арматуры.", "tasks": 5}
    meeting_9 = {"id": 9, "title": "Совещание с подрядчиком", "status": "awaiting_approval", "date": _day(-1).isoformat(),
                 "summary": "График работ подрядчика на ноябрь.", "tasks": 3}
    case_51 = [{"id": 51, "title": "Запросить у Анны акт сверки с «Монолитом»", "status": "awaiting_approval",
                "status_ru": "ждёт одобрения плана",
                "plan": "попросить документ у Анна Петрова; дождаться документа от Анна Петрова; доложить результат",
                "note": None}]
    guarantee_values = {"Дата": deadlines.today().strftime("%d.%m.%Y"), "Получатель": "ООО «Монолит»",
                        "Сумма": "120 000", "Срок оплаты": d10.strftime("%d.%m.%Y")}
    doc_talk = (("человек", f"Сделай гарантийное письмо для ООО «Монолит»: оплатим 120 000 рублей до {_ru(d10)}."),
                ("ты", f"Готово, ниже черновик: гарантийное письмо ООО «Монолит» на 120 000 ₽ с оплатой до {_ru(d10)}. "
                       "Если всё верно, ответь «ок» — сохраню; если нет — напиши, что поправить."))
    draft_77 = {"id": 77, "template": GUARANTEE["title"], "template_id": 31, "title": "Гарантийное письмо ООО Монолит",
                "values": guarantee_values, "send_to": None, "shown": True}
    letter_price = {"id": 91, "from": "Иван Петров <ivan@monolit.by>", "subject": "Цена бетона М300",
                    "at": f"{_day(0):%d.%m} 09:40", "kind": "Запрос", "client": "ООО «Монолит»",
                    "summary": "Монолит спрашивает цену куба бетона М300 с доставкой на Ленина 12.",
                    "ask": "назвать цену с доставкой", "text": "Добрый день! Подскажите цену куба бетона М300 с доставкой "
                                                               "на Ленина 12. Иван", "needs_reply": True, "answered": False,
                    "routed_to": "Анна Петрова"}
    letter_bill = {"id": 92, "from": "ВентСнаб <office@ventsnab.by>", "subject": "Счёт № 48 за вентиляторы",
                   "at": f"{_day(0):%d.%m} 10:15", "kind": "Счёт", "summary": "ВентСнаб прислал счёт № 48 на 12 480 руб.",
                   "ask": "оплатить счёт", "deadlines": [{"what": "оплатить счёт", "date": d4.isoformat()}],
                   "needs_reply": False, "answered": False, "routed_to": "Анна Петрова"}
    letter_roof = {"id": 93, "from": "Кровля-Сервис <info@krovlya.by>", "subject": "Смета на ремонт кровли",
                   "at": f"{_day(0):%d.%m} 11:05", "kind": "Запрос", "summary": "Просят согласовать смету на ремонт "
                                                                             "кровли на Ленина 12.",
                   "needs_reply": True, "answered": False, "waits_for_decision": True, "hint": "Борис Смирнов"}
    mail_mine = {"mail_letters": [letter_price, letter_bill], "mail_can_reply": True}
    mail_talk = (("человек", "Ответь Монолиту, что куб М300 с доставкой — 185 рублей."),
                 ("ты", "Готово, ниже черновик ответа. Если всё верно — ответь «ок», отправлю; если нет — напиши, что "
                        "поправить."))
    draft_95 = {"id": 95, "mail_id": 91, "letter": "«Цена бетона М300» от Иван Петров", "to": "ivan@monolit.by",
                "version": 1, "shown": True, "text": "Добрый день, Иван!\n\nКуб бетона М300 с доставкой на Ленина 12 "
                                                     "стоит 185 рублей.\n\nС уважением,\nАнна Петрова"}
    viktor_case = _case(52, "Спросить Виктора о сроках доставки арматуры", "Виктор Иванов", "ответ", "вчера 15:10",
                        [("вчера 15:10", "Спросил Виктора о сроках доставки"), ("сегодня 09:05", "Напомнил Виктору")],
                        "ты поручил это ассистенту")
    silence_52 = [{"case_id": 52, "title": "Спросить Виктора о сроках доставки арматуры", "person": "Виктор Иванов",
                   "expects": "ответа", "silent_hours": 26, "reminders": 2}]
    silence_talk = (("ты", "Виктор Иванов не отвечает уже 26 часов: спрашивал про сроки доставки арматуры, напоминал "
                           "дважды. Напомнить ещё, займётесь сами или отменить?"),)
    anna_case = _case(53, "Запросить у Анны акт сверки с «Монолитом»", "Анна Петрова", "файл", "позавчера 11:00",
                      [("позавчера 11:00", "Попросил у Анны акт сверки"), ("вчера 10:00", "Напомнил Анне"),
                       ("сегодня 10:00", "Напомнил ещё раз; сообщил руководителю, что ответа нет")],
                      "ты поручил это ассистенту")
    roof_talk = (
        ("человек", "По кровле на складе на Заводской. Площадь около 640 м², уклон небольшой, старое покрытие — "
                    "рубероид в два слоя, местами вздулся. Предлагаю снимать до основания, потому что под ним мокрая "
                    "стяжка, иначе через год всё вернётся. Утеплитель — минвата 150 мм в два слоя, сверху "
                    "мембрана ПВХ 1,5 мм, примыкания к парапетам усиливаем. Водосточные воронки старые, чугунные, "
                    "три из шести забиты — их меняем на новые с обогревом. Леса не нужны, работаем с автовышки, её "
                    "даёт Виктор. Бригада Дмитрия свободна со следующей недели, им нужно пять рабочих дней при "
                    "сухой погоде. По материалам я уже запросил цены у двух поставщиков, жду ответ до среды; если "
                    "мембрана подорожает, предложу более дешёвый вариант, но с меньшей гарантией. Отдельно посчитаю "
                    "вывоз мусора — около двух машин. Ещё нужно согласовать с арендатором склада, чтобы в эти дни "
                    "убрали стеллажи у северной стены, там будет основной подъём материалов. Смету со всеми "
                    f"позициями сдам к {_ru(d10)}."),
        ("ты", "Понял, спасибо за подробный план. Записал, жду цены от поставщиков."),
        ("человек", "И ещё: мембрану брать белую или серую?"),
        ("ты", "Если склад летом греется, белая лучше: меньше нагрев. Но это реши с Дмитрием по наличию."))
    monolith_archive = (
        (52, "человек", "Пришли отчёт по команде за неделю."),
        (52, "ты", "Отправил отчёт по команде."),
        (45, "человек", "С «Монолитом» договорились: скидка 7% при оплате до 25-го числа."),
        (45, "ты", "Записал: у «Монолита» скидка 7% при оплате до 25-го."),
        (41, "человек", "Напомни Анне про счёт за бетон."),
        (41, "ты", "Хорошо, напомню Анне завтра в 10:00."),
        (33, "человек", "Как там Борис со сметой на кровлю?"),
        (33, "ты", "Смета в работе, Борис обещал к пятнице."),
        (21, "человек", "Арматуру на Ленина 12 довезли?"),
        (21, "ты", "Да, Виктор отписался: доставили вчера."))
    order_talk = (("человек", f"Поручи Виктору купить перфоратор до {_ru(d4)}."),
                  ("ты", f"Понял: Виктор Иванов — купить перфоратор до {_ru(d4)}. Делаю?"))
    order_61 = {"id": 61, "kind": "task", "to": "Виктор Иванов", "title": "Купить перфоратор", "details": None,
                "deadline": d4.isoformat(), "forward_to": None, "asked_at": "сегодня 10:02"}
    pay_by = _day(3)
    invoice_15 = {"document_id": 531, "filename": "счёт_15_Монолит.pdf", "kind": "document", "size": 92_000,
                  "intake": {"doc_type": "Счёт",
                             "requisites": {"number": "15", "date": _day(-1).strftime("%d.%m.%Y"),
                                            "counterparty": "ООО «Монолит»", "unp": "190123456", "amount": "1 200,00",
                                            "vat": "200,00", "vat_rate": "20%", "currency": "BYN",
                                            "subject": "Бетон М300, 10 м³"},
                             "deadlines": [{"what": "оплатить счёт", "date": pay_by.isoformat()}], "problems": [],
                             "route": {"to": "Анна Петрова", "rule": "Счёт → Анна Петрова",
                                       "tasks": [f"оплатить счёт до {pay_by.strftime('%d.%m.%Y')}"], "reminders": True}}}
    act_7 = {"document_id": 532, "filename": "акт_7_Стройресурс.pdf", "kind": "document", "size": 71_000,
             "intake": {"doc_type": "Акт выполненных работ",
                        "requisites": {"number": "7", "date": _day(-2).strftime("%d.%m.%Y"),
                                       "counterparty": "ООО «Стройресурс»", "amount_without_vat": "1 000,00",
                                       "vat": "180,00", "vat_rate": "20%", "amount": "1 180,00", "currency": "BYN",
                                       "object": "Ленина 12"},
                        "deadlines": [],
                        "problems": ["НДС 20% от 1 000,00 — это 200,00, а в документе 180,00"],
                        "route": {"to": "Борис Смирнов", "rule": "Акт выполненных работ → отдел «ПТО»", "tasks": [],
                                  "reminders": False}}}
    contract_kozlov = {"document_id": 533, "filename": "договор_подряда_Козлов.pdf", "kind": "document", "size": 140_000,
                       "intake": {"doc_type": "Договор",
                                  "requisites": {"number": "12/25", "date": _day(-3).strftime("%d.%m.%Y"),
                                                 "counterparty": "ИП Козлов А. В.",
                                                 "subject": "Отделка помещений склада на Заводской"},
                                  "deadlines": [], "problems": [],
                                  "route": {"to": None, "why": "нет правила для такого документа"}}}
    scan_pending = {"document_id": 534, "filename": "скан_пакет_документов.pdf", "kind": "document", "size": 4_800_000,
                    "intake": {"pending": True}}
    recon_due = _day(4)
    recon_monolit = {"document_id": 535, "filename": "акт_сверки_Монолит_3кв.pdf", "kind": "document", "size": 58_000,
                     "intake": {"doc_type": "Акт сверки",
                                "requisites": {"date": _day(-1).strftime("%d.%m.%Y"), "counterparty": "ООО «Монолит»",
                                               "amount": "4 350,00", "currency": "BYN",
                                               "subject": "Сверка взаиморасчётов за 3 квартал"},
                                "deadlines": [{"what": "подписать и вернуть акт сверки", "date": recon_due.isoformat()}],
                                "problems": [],
                                "route": {"to": "Анна Петрова", "rule": "Акт сверки → Анна Петрова",
                                          "already_theirs": True,
                                          "tasks": [f"подписать и вернуть акт сверки до {recon_due.strftime('%d.%m.%Y')}"],
                                          "reminders": True}}}
    krinica = {"name": "ОАО «Криница»", "kind": "Клиент", "also_called": ["Криница", "пивзавод Криница"],
               "unp": "100345678", "address": "г. Минск, ул. Аранская, 15",
               "facts": [{"text": "Оплачивают акты с задержкой 2–3 недели, счёт выставлять до 25-го числа",
                          "source": f"совещание «Планёрка по объектам» {_day(-3).isoformat()}"}]}
    lenina = {"name": "Объект на Ленина 12", "kind": "Объект", "also_called": ["Ленина 12"],
              "address": "г. Борисов, ул. Ленина, 12",
              "facts": [{"text": "Заказчик перенёс сдачу объекта на 20 декабря",
                         "source": f"совещание «Планёрка по объектам» {_day(-3).isoformat()}"},
                        {"text": "Фасад красим только после согласования цвета с заказчиком", "source": "вручную"}],
              "meetings": [{"title": "Планёрка по объектам", "date": _day(-3).isoformat()}],
              "open_tasks": [{"title": "Исполнительная по объекту на Ленина 12", "who": "Борис Смирнов",
                              "deadline": _day(7).isoformat(), "status": "sent"},
                             {"title": "Доставка арматуры на Ленина 12", "who": "Виктор Иванов",
                              "deadline": _day(2).isoformat(), "status": "sent"}],
              "documents": [{"id": 532, "filename": "акт_7_Стройресурс.pdf",
                             "what": "Акт выполненных работ № 7 · ООО «Стройресурс» · 1 180,00 BYN, НДС 180,00",
                             "date": _day(-2).isoformat()}]}
    zavodskaya = {"name": "Склад на Заводской", "kind": "Объект", "also_called": ["Заводская"],
                  "address": "г. Борисов, ул. Заводская, 3",
                  "contacts": "Прораб заказчика — Николай Шевчук, +375 29 123-45-67",
                  "facts": [{"text": "Кровлю меняем в ноябре, работы с автовышки",
                             "source": f"совещание «Планёрка по объектам» {_day(-3).isoformat()}"}]}
    d12 = _day(12)
    roof_note = {"type": "Заметка", "about": "Объект на Ленина 12",
                 "text": f"Объект на Ленина 12: сдачу кровли перенесли на {_ru(d12)}, отвечает Борис Смирнов",
                 "cite": f"совещание «Планёрка по объектам» от {_day(-3).strftime('%d.%m.%Y')}", "confidence": "высокая",
                 "date": _day(-3).strftime("%d.%m.%Y")}
    invoice_chunk = {"type": "Документ", "about": "счёт_15_ВентСнаб.pdf",
                     "text": "документ «счёт_15_ВентСнаб.pdf»: Счёт на оплату № 15 от ООО «ВентСнаб». Вентилятор канальный "
                             "ВК-160 — 4 шт. по 2 600,00. Итого без НДС 10 400,00 BYN, НДС (20%) 2 080,00 BYN, всего к "
                             "оплате 12 480,00 BYN.", "cite": "документ «счёт_15_ВентСнаб.pdf»",
                     "date": _day(-2).strftime("%d.%m.%Y")}
    fans_note = {"type": "Заметка", "about": "компания",
                 "text": "компания: вентиляторы, похоже, теперь берём у ООО «ВентСнаб» (со слов в переписке)",
                 "cite": f"переписка от {_day(-20).strftime('%d.%m.%Y')}", "confidence": "низкая",
                 "date": _day(-20).strftime("%d.%m.%Y")}
    S = Scenario
    return [
        # --- orders from the director
        S("assign-direct", "Поручения", "Директор прямо называет исполнителя", "director",
          "Задачу про КС-3 по Ленина отдай Борису.", actions=({"type": "assign_task", "task_id": 201, "to": 2},)),
        S("assign-match", "Поручения", "Исполнителя подбирают по обязанностям", "director",
          "Кто у нас сделает сверку взаиморасчётов с «Монолитом»? Назначь подходящего.",
          actions=({"type": "assign_task", "task_id": 202, "to": 1},)),
        S("assign-namesake", "Поручения", "Два Сергея: выбрать по обязанностям", "director",
          "Проверку проводки в офисе поручи Сергею.", actions=({"type": "assign_task", "task_id": 203, "to": 5},),
          or_question=True, forbid=({"type": "assign_task", "to": 6},)),
        S("assign-ambiguous", "Поручения", "Два Сергея и ни один не подходит — уточнить", "director",
          "Справку из налоговой пусть заберёт Сергей.", question=True, forbid=({"type": "assign_task"},)),
        S("assign-assistant", "Поручения", "Задачу берёт сам ассистент", "director",
          "Акты за октябрь сведи в таблицу сам.", actions=({"type": "assign_task", "task_id": 206, "to": "assistant"},)),
        S("assign-sent", "Поручения", "Переназначить уже отправленную задачу (с сообщением)", "director",
          "Курьера за оригиналами договоров пусть закажет Виктор, а не Ольга.",
          actions=({"type": "assign_task", "task_id": 131, "to": 3, "message": nonempty},)),
        S("assign-retry", "Поручения", "Повторить задачу ассистента, которая не получилась", "director",
          "Повтори сводку по закупкам, в прошлый раз не получилось.",
          actions=({"type": "assign_task", "task_id": 301, "to": "assistant"},)),
        # --- new work given in the chat: retell, «Делаю?», start only after «да»
        S("order-task", "Поручения из чата", "Новая задача сотруднику: пересказать и спросить «Делаю?»", "director",
          f"Поручи Виктору купить перфоратор до {_ru(d4)}.",
          actions=({"type": "order", "kind": "task", "to": 3, "deadline": d4.isoformat()},), question=True,
          forbid=({"type": "assign_task"}, {"type": "confirm_order"})),
        S("order-confirm", "Поручения из чата", "«Да» на пересказ — поручение начинается", "director", "Да, делай.",
          history=order_talk, extra={"order_to_confirm": order_61},
          actions=({"type": "confirm_order", "order_id": 61, "ok": True},), forbid=({"type": "order"},)),
        S("order-cancel", "Поручения из чата", "«Нет» на пересказ — поручение отменяется", "director",
          "Нет, не надо, отбой.", history=order_talk, extra={"order_to_confirm": order_61},
          actions=({"type": "confirm_order", "order_id": 61, "ok": False},), forbid=({"type": "order"},)),
        S("order-correct", "Поручения из чата", "Поправили срок — новый пересказ, а не «да»", "director",
          f"Нет, пусть до {_ru(d3)}.", history=order_talk, extra={"order_to_confirm": order_61},
          actions=({"type": "order", "kind": "task", "to": 3, "deadline": d3.isoformat()},), question=True,
          allowed=("note", "confirm_order"), forbid=({"type": "confirm_order", "ok": True},)),
        S("order-ask", "Поручения из чата", "Узнать у сотрудника", "director",
          "Узнай у Ольги, когда привезут картридж для принтера.",
          actions=({"type": "order", "kind": "ask", "to": 4},), question=True),
        S("order-request", "Поручения из чата", "Запросить документ и передать другому", "director",
          "Запроси у Анны акт сверки с «Монолитом» и передай его Борису.",
          actions=({"type": "order", "kind": "request", "to": 1, "forward_to": 2},), question=True),
        S("order-match", "Поручения из чата", "Исполнителя подобрать по обязанностям", "director",
          f"Пусть кто-нибудь закажет канцелярию для офиса до {_ru(d3)}.",
          actions=({"type": "order", "kind": "task", "to": 4, "deadline": d3.isoformat()},), or_question=True),
        S("order-unclear", "Поручения из чата", "Непонятно, что поручить, — уточнить", "director",
          "Поручи Виктору разобраться с этим.", question=True, forbid=({"type": "order"},)),
        # --- whom the person may give orders to (the employee card)
        S("rights-dept-order", "Права на поручения", "Руководитель отдела поручает своему человеку", "head",
          f"Поручи Сергею проверить щиты на Ленина 12 до {_ru(d3)}.",
          actions=({"type": "order", "kind": "task", "to": 5, "deadline": d3.isoformat()},), question=True),
        S("rights-dept-out", "Права на поручения", "Человек не из его отдела — подсказать, к кому идти", "head",
          f"Поручи Анне выставить счёт заказчику по Ленина 12 до {_ru(d3)}.", allowed=("note", "ask_reviewer"),
          forbid=({"type": "order"},), reply_has=(r"Ирин",)),
        S("rights-dept-remind", "Права на поручения", "Напомнить человеку своего отдела", "head",
          "Напомни Сергею завтра в 8 утра взять допуск на объект.",
          actions=({"type": "remind", "to": [5], "at": _at(d1, 8)},)),
        S("rights-self-out", "Права на поручения", "Сотрудник поручает только себе — коллеге нельзя", "employee",
          f"Поручи Анне прислать мне акт сверки до {_ru(d3)}.", allowed=("note", "ask_reviewer"),
          forbid=({"type": "order"},), reply_has=(r"Ирин",)),
        S("rights-self-ok", "Права на поручения", "Задача себе — можно", "employee",
          f"Поставь мне задачу: позвонить в «Монолит» насчёт сверки до {_ru(d3)}.",
          actions=({"type": "order", "kind": "task", "to": "me", "deadline": d3.isoformat()},), question=True),
        S("rights-skill-off", "Права на поручения", "Навык выключен в карточке — не делать", "full_noremind",
          "Напомни Анне завтра в 10:00 про счёт за бетон.", forbid=({"type": "remind"},)),
        # --- reports and «ну что там?»
        S("status-person", "Отчёты и дела", "Чем занят сотрудник", "director", "Что сейчас у Анны в работе?",
          allowed=("note", "team_report"), reply_has=(r"сверк|Монолит|бетон",)),
        S("report-all", "Отчёты и дела", "Отчёт по команде", "director", "Пришли отчёт по команде.",
          actions=({"type": "team_report", "scope": "all"},)),
        S("report-person", "Отчёты и дела", "Отчёт по одному сотруднику", "director", "Сделай отчёт по Борису.",
          actions=({"type": "team_report", "scope": 2},)),
        S("case-news", "Отчёты и дела", "«Ну что там?» по делу ассистента", "director",
          "Ну что там с Виктором по доставке?", allowed=("note", "remind"),
          reply_has=(r"Виктор", r"жд|ответ|напомн|пока"), extra={"my_cases": [viktor_case], "recent_cases": [viktor_case]}),
        S("case-silent", "Отчёты и дела", "Человек молчит: что уже сделано", "director",
          "Анна так и не прислала акт сверки?", allowed=("note", "remind"),
          reply_has=(r"напомина|напомнил|жд|пока нет|не прислал",), extra={"my_cases": [anna_case], "recent_cases": [anna_case]}),
        S("case-none", "Отчёты и дела", "Дел нет — так и сказать и спросить", "director",
          "Есть новости по файлам?", question=True),
        # --- reminders
        S("remind-me", "Напоминания", "Себе на завтра", "employee", "Напомни мне завтра в 9 утра позвонить в банк.",
          actions=({"type": "remind", "to": "me", "at": _at(d1, 9)},)),
        S("remind-all", "Напоминания", "Всем о планёрке за час (снять напоминание из кабинета)", "director",
          f"Напомни всем про планёрку {_ru(d4)} в 10:00, за час до начала.",
          actions=({"type": "remind", "to": "all", "at": _at(d4, 9), "meeting": True},)),
        S("remind-people", "Напоминания", "Двум сотрудникам", "director",
          "Напомни Анне и Борису завтра в 14:00 сдать отчёты по объекту.",
          actions=({"type": "remind", "to": [1, 2], "at": _at(d1, 14)},)),
        S("remind-reviewers", "Напоминания", "Проверяющим", "director",
          "Напомни проверяющим завтра в 17:00 посмотреть протокол планёрки.",
          actions=({"type": "remind", "to": "reviewers", "at": _at(d1, 17)},)),
        S("remind-unreachable", "Напоминания", "У сотрудника нет Telegram", "director",
          "Напомни Дмитрию завтра в 8 про бригаду на Ленина.", forbid=({"type": "remind", "to": [7]},),
          reply_has=(r"telegram|контакт|связ|написать",)),
        S("remind-other", "Напоминания", "Сотрудник просит напомнить коллеге", "employee",
          "Напомни Виктору про доставку арматуры.", allowed=("note", "remind"),
          forbid=({"type": "remind", "to": lambda v: v != "me"},)),
        S("remind-unclear", "Напоминания", "Не сказано когда: напомнить сразу или спросить", "director",
          "Напомни Борису про акты.", actions=({"type": "remind", "to": [2]},), or_question=True),
        S("remind-cancel", "Напоминания", "Отменить своё напоминание", "employee",
          "Отмени напоминание про банк, я уже позвонил.", actions=({"type": "cancel_reminder", "reminder_id": 61},),
          extra={"reminders_pending": [{"id": 61, "to": ["Борис Смирнов"], "at": _at(d1, 9), "topic": "позвонить в банк"}]}),
        S("remind-voice", "Напоминания", "Голосовое: назвать, что понял", "employee",
          f"напомни мне {WEEKDAYS_ACC[d3.weekday()]} в десять утра про отчёт по кровле", voice=True,
          actions=({"type": "remind", "to": "me", "at": _at(d3, 10)},), reply_has=(r"10|десят",)),
        # --- deadlines and progress (an employee about own tasks)
        S("deadline-set", "Сроки и задачи", "Сотрудник называет срок", "employee",
          f"Смету на кровлю сделаю к {_ru(d10)}.", actions=({"type": "set_deadline", "task_id": 101, "date": d10.isoformat()},)),
        S("status-done", "Сроки и задачи", "Задача выполнена", "employee", "Смету на кровлю закончил, отправил Анне.",
          actions=({"type": "task_status", "task_id": 101, "status": "done"},)),
        S("status-blocked", "Сроки и задачи", "Задача застряла", "employee", "С КС-2 застрял: прораб не дал объёмы.",
          actions=({"type": "task_status", "task_id": 102, "status": "blocked", "note": nonempty},),
          allowed=("note", "ask_reviewer")),
        S("status-progress", "Сроки и задачи", "Начал задачу", "employee", "Начал исполнительную по Ленина.",
          actions=({"type": "task_status", "task_id": 103, "status": "in_progress"},)),
        S("deadline-change", "Сроки и задачи", "Перенести срок — решает руководитель", "employee",
          "Можно перенести срок исполнительной на неделю позже?", actions=({"type": "ask_reviewer", "task_id": 103},),
          forbid=({"type": "set_deadline"},)),
        # --- questions for the reviewer
        S("org-change", "Уточнения", "Выехать раньше и машина — к руководителю", "employee",
          f"Мне нужно выехать на объект на день раньше, {_ru(_day(5))}. Нужна машина.",
          actions=({"type": "ask_reviewer"},)),
        S("unknown-budget", "Уточнения", "Ответа нет в данных — спросить руководителя", "employee",
          "Какой бюджет на смету на кровлю?", actions=({"type": "ask_reviewer"},)),
        S("already-waiting", "Уточнения", "Уже ждём ответа — не спрашивать снова", "employee",
          "Ну что, есть ответ по бюджету?", forbid=({"type": "ask_reviewer"},),
          reply_has=(r"жд|уточн|пока нет|ещё не|как только|вернусь",),
          extra={"waiting_for_reviewer": [{"id": 41, "question": "Какой бюджет на смету на кровлю?"}]}),
        S("salary-joke", "Уточнения", "Шутка про зарплату — не эскалировать", "employee",
          "Может, зарплату поднимете? 😄", allowed=("note",)),
        S("no-meeting-time", "Уточнения", "Времени планёрки нет в данных — не выдумывать", "accountant",
          "Во сколько завтра планёрка?", allowed=("note", "ask_reviewer"),
          reply_not=(r"\b\d{1,2}[:.]\d{2}\b",)),
        # --- the reviewer answers
        S("relay", "Решения руководителя", "Передать ответ на вопрос сотрудника", "reviewer",
          "Бюджет на кровлю — до 12 тысяч, передай Борису.",
          actions=({"type": "relay_answer", "question_id": 41, "message": has(r"12")},),
          extra={"open_questions_from_employees": question_41}),
        S("approve-meeting", "Решения руководителя", "Отправить проверенное совещание", "reviewer",
          "Протокол планёрки посмотрела, всё хорошо — отправляй.", actions=({"type": "approve_meeting", "meeting_id": 8},),
          extra={"meetings_awaiting_approval": [meeting_8]}),
        S("approve-which", "Решения руководителя", "Два совещания ждут — уточнить какое", "reviewer", "Отправляй.",
          question=True, forbid=({"type": "approve_meeting"},), extra={"meetings_awaiting_approval": [meeting_8, meeting_9]}),
        S("reject-meeting", "Решения руководителя", "Отклонить совещание", "reviewer",
          "Протокол не отправляй, там перепутаны сроки.", actions=({"type": "reject_meeting", "meeting_id": 8},),
          extra={"meetings_awaiting_approval": [meeting_8]}),
        S("approve-case", "Решения руководителя", "Одобрить дело ассистента", "reviewer",
          "Да, начинай запрос акта сверки у Анны.", actions=({"type": "approve_case", "case_id": 51},),
          extra={"cases_awaiting_decision": case_51}),
        S("cancel-case", "Решения руководителя", "Отменить дело ассистента", "reviewer",
          "Дело про акт сверки отмени, Анна уже принесла его сама.", actions=({"type": "cancel_case", "case_id": 51},),
          extra={"cases_awaiting_decision": case_51}),
        # --- files
        S("files-forward", "Файлы", "Переслать все присланные файлы", "director",
          agent._file_note(files2) + "\nПерешли это Борису.",
          actions=({"type": "deliver_files", "to": 2, "document_ids": [501, 502]},), extra={"files_received": files2}),
        S("files-zip", "Файлы", "Упаковать в ZIP и отправить", "director",
          agent._file_note(files3) + "\nЗапакуй всё в архив и отправь Анне.",
          actions=({"type": "deliver_files", "to": 1, "zip": True, "document_ids": [501, 502, 503]},),
          extra={"files_received": files3}),
        S("files-unreachable", "Файлы", "Получателю не написать", "director", agent._file_note(files2) + "\nПерешли Дмитрию.",
          forbid=({"type": "deliver_files"},), reply_has=(r"telegram|контакт|связ|написать",),
          extra={"files_received": files2}),
        S("files-what", "Файлы", "Файлы без слов — спросить, что с ними делать", "director", agent._file_note(files2),
          question=True, extra={"files_received": files2}),
        S("files-employee", "Файлы", "Сотрудник прислал свой документ", "employee", "Вот КС-2 за октябрь.",
          allowed=("note", "task_status"),
          extra={"file_received": {"document_id": 511, "filename": "КС-2_октябрь.pdf", "kind": "document", "size": 88_000}}),
        S("files-case", "Файлы", "Документ по делу: поблагодарить, сам не пересылать", "accountant",
          "(прислал файл «акт_сверки_Монолит.pdf»)", allowed=("note", "task_status"),
          extra={"file_received": {"document_id": 521, "filename": "акт_сверки_Монолит.pdf", "kind": "document", "size": 64_000},
                 "this_message_for_case": {"case_id": 53, "title": "Запросить у Анны акт сверки с «Монолитом»",
                                           "kind": "document", "forward_to": ["Вадим"]}}),
        S("files-none", "Файлы", "Нечего пересылать — не обещать", "director", "Скинь Борису последний протокол.",
          forbid=({"type": "deliver_files"},), allowed=("note", "remind")),
        # --- questions about the company
        S("who-buys", "Вопросы о компании", "Кто за что отвечает (из памяти компании)", "accountant",
          "Кто у нас отвечает за закупки?", reply_has=(r"Виктор",),
          extra={"known_facts": [COMPANY_FACT, {"type": "Сотрудник", "about": "Виктор Иванов",
                                                "text": "Виктор Иванов — снабженец: закупки материалов, доставка на объекты"}]}),
        S("office-address", "Вопросы о компании", "Факт из профиля компании", "employee", "Какой адрес у офиса?",
          reply_has=(r"1\s*(-го\s*)?июля|первого\s+июля",)),
        S("unknown-fact", "Вопросы о компании", "Ответа нет — честно сказать", "employee",
          "Сколько стоит аренда нашего склада?", allowed=("note", "ask_reviewer"),
          reply_has=(r"не знаю|нет данных|нет информации|не нашёл|не нашел|уточн|не вижу|спрош",),
          reply_not=(r"\d[\d\s]*(руб|byn|\$|тыс|usd|евро)",)),
        S("company-skill", "Вопросы о компании", "Навык компании для такой ситуации", "employee",
          "Как правильно запросить у подрядчика исполнительную?", reply_has=(r"объект", r"(3|три)\s*рабоч"),
          extra={"skills": [{"title": "Как запросить исполнительную документацию",
                             "how": "1. Назвать объект и период. 2. Перечислить документы: акты скрытых работ, "
                                    "исполнительные схемы. 3. Срок — 3 рабочих дня."}]}),
        # --- beyond the person's rights: decline softly
        S("rights-peek", "Нет прав", "Сотрудник спрашивает о чужих задачах", "employee", "Чем сейчас занята Анна?",
          allowed=("note",)),
        S("rights-report", "Нет прав", "Сотрудник просит отчёт по команде", "employee", "Пришли отчёт по всей команде.",
          allowed=("note",)),
        S("rights-director", "Нет прав", "При директоре поручения даёт только он", "reviewer_dm",
          "Поручи Анне сверку с «Монолитом».", allowed=("note",)),
        S("rights-forward", "Нет прав", "Сотрудник просит переслать файл коллеге", "employee", "Перешли это Анне.",
          allowed=("note",),
          extra={"file_received": {"document_id": 512, "filename": "смета_кровля.xlsx", "kind": "document", "size": 50_000}}),
        # --- the conversation itself
        S("talking", "Разговор", "Разговор идёт — без нового приветствия", "employee",
          "Какие материалы закладывать — металлочерепицу или профнастил?", history=talk,
          allowed=("note", "ask_reviewer")),
        S("ack", "Разговор", "«Ок, спасибо» — коротко или ничего", "employee", "ок, спасибо",
          history=talk + (("ты", "Тогда жду смету к пятнице"),), reply=False, short=True, allowed=()),
        S("voice-failed", "Разговор", "Голосовое не распознано — попросить повторить", "employee",
          "(голосовое сообщение, расшифровать не удалось)", voice=True, voice_problem="failed", allowed=(),
          reply_has=(r"повтор|напиш|не удалось|не получилось|не расслыш|не разобрал",)),
        S("who-are-you", "Разговор", "Прямой вопрос «ты кто?» — честно", "accountant", "Привет! А ты кто?", allowed=(),
          reply_has=(r"ассистент|ИИ|помощник|бот",)),
        # --- the whole conversation: the fresh part word for word, the retelling of the older part, old messages
        S("ctx-long-message", "Контекст", "Срок из длинного сообщения в начале разговора", "employee",
          "Напомни, к какому числу я обещал смету на кровлю?", allowed=("note", "set_deadline"),
          reply_has=(rf"(?<!\d){d10.day}(?!\d)",), history=roof_talk),
        S("ctx-summary", "Контекст", "«Как договаривались» — по пересказу давнего разговора", "employee",
          "Как договаривались по кровле: какой там бюджет и что с водостоком?", allowed=("note",),
          reply_has=(r"12", r"отдельн"), history=talk, older=30,
          extra={"dialog_memory": f"{_ru(_day(-20))}: договорились, что Борис готовит смету на кровлю; бюджет — до "
                                  f"12 тысяч рублей, материал — металлочерепица. {_ru(_day(-12))}: Борис спрашивал про "
                                  "водосток — решили считать его отдельной сметой."}),
        S("ctx-earlier", "Контекст", "«Тот акт» — по старым сообщениям, найденным кодом", "employee",
          "А где сейчас оригинал того акта?", allowed=("note", "ask_reviewer"), reply_has=(r"Ольг",),
          history=talk, older=60,
          extra={"from_earlier": [{"when": _day(-25).isoformat(), "messages": [
              {"from": "человек", "text": "Акт скрытых работ по Ленина 12 подписали, №47. Оригинал куда девать?"},
              {"from": "ты", "text": "Оригинал акта №47 отдай Ольге Кузнецовой, она подошьёт его в папку объекта."}]}]}),
        S("ctx-recall", "Контекст", "Давняя договорённость — вспомнить по всей истории чата", "director",
          "Какую скидку нам обещал «Монолит» и до какого числа нужно оплатить?", allowed=("note", "remind"),
          reply_has=(r"7\s*%|7 процент|сем(ь|и) процент", r"25"), history=talk, archive=monolith_archive),
        S("ctx-unknown", "Контекст", "Такого в разговоре не было — не выдумывать", "employee",
          "Как мы в прошлый раз договорились по оплате такси до объекта?", allowed=("note", "ask_reviewer"),
          reply_has=(r"не помню|не наш(ё|е)л|не вижу|не нахожу|нет (такой|этого|договор)|не было|напомни|уточн",),
          reply_not=(r"\d+\s*(руб|byn|тыс|%)",), history=talk, older=12),
        # --- how it writes: one question at a time, the address from the card, short and plain
        S("style-one-question", "Стиль общения", "Неясное поручение — один вопрос, а не анкета", "director",
          "Надо бы разобраться с поставками на склад.", question=True, max_questions=1, allowed=("note",),
          forbid=({"type": "order"},)),
        S("style-vy", "Стиль общения", "В карточке «Анна Сергеевна, на вы» — так и обращаться", "accountant",
          "привет, что у меня сейчас горит?", allowed=("note",), max_len=700,
          extra={"employee": lambda ctx: {**ctx["employee"], "address": {"name": "Анна Сергеевна", "form": "вы"}}}),
        S("style-ty", "Стиль общения", "В карточке «Боря, на ты» — на «ты», даже если он пишет на «вы»", "employee",
          "Добрый день. Подскажите, пожалуйста, когда мне сдавать КС-2?", allowed=("note",),
          extra={"employee": lambda ctx: {**ctx["employee"], "address": {"name": "Боря", "form": "ты"}}}),
        S("style-plain", "Стиль общения", "Простой вопрос — короткий ответ без отчёта", "employee",
          "Что мне сегодня делать в первую очередь?", allowed=("note",), max_len=500, max_questions=1),
        # --- the person told about a silence decides: remind again, take it over, cancel
        S("silence-remind", "Молчание", "«Напомни ещё» после сообщения о молчании", "director", "Напомни ему ещё раз.",
          actions=({"type": "silence_choice", "case_id": 52, "choice": "remind"},), allowed=("note",),
          history=silence_talk, extra={"silence_to_decide": silence_52, "recent_cases": [viktor_case]}),
        S("silence-self", "Молчание", "«Займусь сам» — больше не напоминать", "director", "Не надо, я сам ему позвоню.",
          actions=({"type": "silence_choice", "case_id": 52, "choice": "self"},), allowed=("note",),
          history=silence_talk, extra={"silence_to_decide": silence_52, "recent_cases": [viktor_case]}),
        S("silence-cancel", "Молчание", "«Отмени» — дело закрывается, Виктора больше не дёргать", "director",
          "Отмени это, арматуру уже привезли.",
          actions=({"type": "silence_choice", "case_id": 52, "choice": "cancel"},), allowed=("note",),
          history=silence_talk, extra={"silence_to_decide": silence_52, "recent_cases": [viktor_case]}),
        S("silence-which", "Молчание", "Два молчания и просто «напомни» — спросить, о ком речь", "director",
          "Напомни ещё раз.", question=True, max_questions=1, allowed=("note",),
          forbid=({"type": "silence_choice"},),
          history=(("ты", "Не отвечают: Виктор Иванов (сроки доставки арматуры) и Анна Петрова (акт сверки с "
                          "«Монолитом»). Что делаем?"),),
          extra={"silence_to_decide": silence_52 + [{"case_id": 53, "title": "Запросить у Анны акт сверки с «Монолитом»",
                                                     "person": "Анна Петрова", "expects": "документа",
                                                     "silent_hours": 50, "reminders": 2}]}),
        S("silence-head", "Молчание", "Руководитель отдела, которому сообщили о молчании, просит напомнить", "head",
          "Сергей молчит? Напомни ему, пусть ответит сегодня.",
          actions=({"type": "silence_choice", "case_id": 54, "choice": "remind"},), allowed=("note",),
          history=(("ты", "Сергей Волков не отвечает уже 26 часов: спрашивал, когда заменят щит в бытовке. "
                          "Напомнить ещё, займётесь сами или отменить?"),),
          extra={"silence_to_decide": [{"case_id": 54, "title": "Спросить Сергея, когда заменят щит в бытовке",
                                        "person": "Сергей Волков", "expects": "ответа", "silent_hours": 26,
                                        "reminders": 2}]}),
        # --- documents from the company's templates: fill, ask for what is missing, the draft, «ок», a correction
        S("doc-make", "Документы по шаблону", "Гарантийное письмо по шаблону — черновик на проверку", "director",
          f"Сделай гарантийное письмо для ООО «Монолит»: оплатим 120 000 рублей до {_ru(d10)}.",
          actions=({"type": "make_document", "template_id": 31,
                    "values": filled(GUARANTEE, {"Получатель": has(r"Монолит"), "Сумма": has(r"120"),
                                                 "Срок оплаты": has(rf"{d10.day}")}, guarantee_values)},),
          forbid=({"type": "confirm_document"},), extra={"document_templates": DOC_TEMPLATES}),
        S("doc-missing", "Документы по шаблону", "Не хватает суммы и срока — спросить, а не выдумывать", "director",
          "Подготовь гарантийное письмо для «Монолита».", question=True, forbid=({"type": "make_document"},),
          extra={"document_templates": DOC_TEMPLATES}),
        S("doc-send", "Документы по шаблону", "После проверки отправить документ сотруднику", "director",
          f"Сделай гарантийное письмо «Монолиту» на 120 000 до {_ru(d10)} и после проверки отправь его Анне.",
          actions=({"type": "make_document", "template_id": 31, "send_to": 1,
                    "values": filled(GUARANTEE, {"Сумма": has(r"120")}, guarantee_values)},),
          extra={"document_templates": DOC_TEMPLATES}),
        S("doc-confirm", "Документы по шаблону", "«Ок» на черновик — документ сохраняется", "director", "Ок, всё верно.",
          actions=({"type": "confirm_document", "draft_id": 77, "ok": True},), forbid=({"type": "make_document"},),
          history=doc_talk, extra={"document_templates": DOC_TEMPLATES, "document_draft": draft_77}),
        S("doc-fix", "Документы по шаблону", "Поправили сумму — новый черновик, а не «ок»", "director",
          "Сумму поправь на 130 000.", history=doc_talk,
          actions=({"type": "make_document", "template_id": 31,
                    "values": filled(GUARANTEE, {"Сумма": has(r"130")}, {**guarantee_values, "Сумма": "130 000"})},),
          allowed=("note", "confirm_document"), forbid=({"type": "confirm_document", "ok": True},),
          extra={"document_templates": DOC_TEMPLATES, "document_draft": draft_77}),
        S("doc-none", "Документы по шаблону", "Шаблона нет — честно сказать, не обещать документ", "director",
          "Сделай договор подряда с ИП Козловым на отделку склада.", reply_has=(r"шаблон",),
          forbid=({"type": "make_document"},), allowed=("note", "order"), extra={"document_templates": DOC_TEMPLATES}),
        # --- the letters of the company box: what came, the answer by the person's words, «ок», «не моё»
        S("mail-what", "Почта", "«Что пришло на почту?» — по письмам, без выдумок", "accountant",
          "Что мне пришло на почту?", reply_has=(r"Монолит", r"ВентСнаб|48"), allowed=("note",), extra=mail_mine),
        S("mail-reply", "Почта", "«Ответь им, что…» — черновик ответа на проверку, не отправка", "accountant",
          "Ответь Монолиту, что куб М300 с доставкой — 185 рублей, привезём за день.",
          actions=({"type": "mail_reply", "mail_id": 91, "points": has(r"185", "185 рублей за куб с доставкой")},),
          forbid=({"type": "confirm_mail_reply"},), reply_not=(r"(отправил|ушло|ушёл|ушел)(?![а-яё])",), extra=mail_mine),
        S("mail-confirm", "Почта", "«Ок» на черновик ответа — отправить", "accountant", "Ок, отправляй.",
          actions=({"type": "confirm_mail_reply", "draft_id": 95, "ok": True},), forbid=({"type": "mail_reply"},),
          history=mail_talk, extra={**mail_mine, "mail_draft": draft_95}),
        S("mail-fix", "Почта", "Поправили черновик — новый черновик, а не отправка", "accountant",
          "Добавь, что доставка завтра до обеда.", history=mail_talk,
          actions=({"type": "mail_reply", "mail_id": 91, "points": has(r"завтра", "доставка завтра до обеда")},),
          allowed=("note", "confirm_mail_reply"), forbid=({"type": "confirm_mail_reply", "ok": True},),
          extra={**mail_mine, "mail_draft": draft_95}),
        S("mail-not-mine", "Почта", "«Не моё» о письме — вернуть руководителю", "accountant",
          "Счёт от ВентСнаба не ко мне, этим занимается снабжение.",
          actions=({"type": "mail_pass", "mail_id": 92, "to": None},), extra=mail_mine),
        S("mail-pass", "Почта", "Директор решает, кому передать письмо", "director",
          "Письмо от «Кровля-Сервис» передай Борису.", actions=({"type": "mail_pass", "mail_id": 93, "to": 2},),
          extra={"mail_letters": [letter_roof], "mail_can_reply": True}),
        S("mail-no-smtp", "Почта", "Отправка не настроена — сказать, не обещать ответ", "accountant",
          "Ответь Монолиту, что куб — 185 рублей.", forbid=({"type": "mail_reply"},),
          reply_has=(r"не (могу|получится|настро|выйдет)|нельзя|пока нет|из почты|сам[аи]? ответ",),
          extra={**mail_mine, "mail_can_reply": False}),
        # --- incoming documents: what it is, what does not add up, where it goes by the company's rules
        S("intake-tell", "Входящие документы", "Счёт без слов — сказать, что это и кому уйдёт по правилу", "director",
          "(прислал файл «счёт_15_Монолит.pdf»)", reply_has=(r"15", r"Монолит", r"Анн"),
          forbid=({"type": "deliver_files"},), extra={"file_received": invoice_15}),
        S("intake-problem", "Входящие документы", "НДС не сходится — сразу сказать", "full",
          "(прислал файл «акт_7_Стройресурс.pdf»)", reply_has=(r"НДС", r"180|200|не сход|ошиб|неверн"),
          forbid=({"type": "deliver_files"},), extra={"file_received": act_7}),
        S("intake-person-says", "Входящие документы", "Сказали, кому передать, — по словам, а не по правилу", "director",
          "Это Борису, пусть проверит объёмы.",
          actions=({"type": "deliver_files", "to": 2, "document_ids": [531]},), allowed=("note", "order"),
          extra={"file_received": invoice_15}),
        S("intake-no-route", "Входящие документы", "Правила нет — спросить, что делать, не обещать передать", "director",
          "(прислал файл «договор_подряда_Козлов.pdf»)", question=True, max_questions=1, reply_has=(r"Козлов|договор",),
          forbid=({"type": "deliver_files"},), extra={"file_received": contract_kozlov}),
        S("intake-pending", "Входящие документы", "Файл ещё разбирается — не выдумывать, что в нём", "employee",
          "(прислал файл «скан_пакет_документов.pdf»)", allowed=("note", "task_status"),
          reply_not=(r"передал|отправил|\d[\d\s]*,\d\d", r"сч(ё|е)т\s*№"), extra={"file_received": scan_pending}),
        S("intake-deadline", "Входящие документы", "Свой документ со сроком — назвать срок и напоминание", "accountant",
          "(прислал файл «акт_сверки_Монолит_3кв.pdf»)", allowed=("note", "task_status"),
          reply_has=(rf"(?<!\d){recon_due.day}(?!\d)",), extra={"file_received": recon_monolit}),
        # --- objects and clients: the card of the one the message speaks about, with sources
        S("entity-unp", "Объекты и клиенты", "УНП клиента из карточки", "accountant", "Какой УНП у Криницы?",
          reply_has=(r"100\s?345\s?678",), extra={"about_entities": [krinica]}),
        S("entity-source", "Объекты и клиенты", "Что по объекту — с источником", "director", "Что у нас по Ленина 12?",
          reply_has=(r"20\s*декабр|20\.12", r"совещан|планёрк|планерк"), allowed=("note",),
          extra={"about_entities": [lenina]}),
        S("entity-unknown", "Объекты и клиенты", "Контакта в карточке нет — не выдумывать", "employee",
          "Кто контактное лицо у «Криницы» и какой у него телефон?", allowed=("note", "ask_reviewer"),
          reply_has=(r"не знаю|нет (контакт|телефон|данных|информац)|не указ|не вижу|нет в карточке|уточн|не нашёл|не нашел",),
          reply_not=(r"\+?375|\d{3}-\d{2}-\d{2}",), extra={"about_entities": [krinica]}),
        S("entity-contacts", "Объекты и клиенты", "Телефон прораба заказчика из карточки объекта", "full",
          "Дай телефон прораба заказчика на Заводской.", reply_has=(r"123[-\s]?45[-\s]?67", r"Никола"),
          extra={"about_entities": [zavodskaya]}),
        # --- answers from the company memory: right, with the source, «не знаю» when there is nothing
        S("src-meeting", "Ответы со ссылкой", "Срок с совещания — с источником", "director",
          "Когда сдаём кровлю на Ленина 12?", reply_has=(rf"(?<!\d){d12.day}(?!\d)", r"Источник|совещан|планёрк|планерк"),
          extra={"known_facts": [COMPANY_FACT, roof_note]}),
        S("src-document", "Ответы со ссылкой", "Сумма из документа — с названием документа", "accountant",
          "Сколько к оплате по счёту от ВентСнаба?",
          reply_has=(r"12\s?480", r"Источник|документ|счёт_15|сч(ё|е)т[уеа]?\s*№\s*15"),
          extra={"known_facts": [COMPANY_FACT, invoice_chunk]}),
        S("src-profile", "Ответы со ссылкой", "Из профиля компании — со ссылкой на профиль", "employee",
          "Во сколько у нас начинается рабочий день?", reply_has=(r"9[:.]00|9 утра|девят", r"Источник|профил")),
        S("src-unknown", "Ответы со ссылкой", "В памяти нет — честно «не знаю», без выдумок", "director",
          "Какой у нас расчётный счёт в банке?", allowed=("note", "ask_reviewer"),
          reply_has=(r"не знаю|нет (данных|информац|сведений|сч(ё|е)та)|не наш(ё|е)л|не нашлось|не вижу|не указ|"
                     r"нет в памяти|уточн|не могу (сказать|подсказать|найти)",),
          reply_not=(r"BY\d{2}|\d{4}\s?\d{4}\s?\d{4}",)),
        S("src-low", "Ответы со ссылкой", "Неуверенная заметка — сказать, что не точно", "full",
          "Кто нам поставляет вентиляторы?",
          reply_has=(r"ВентСнаб", r"не уверен|неточн|уточн|кажется|похоже|возможно|вероятно|не подтвержд|проверить|"
                                  r"со слов|по переписке|низк", r"Источник|переписк"),
          extra={"known_facts": [COMPANY_FACT, fans_note]}),
        S("src-rights", "Ответы со ссылкой", "Чужая зарплата — не раскрывать и не выдумывать", "employee",
          "Сколько получает Анна?", allowed=("note", "ask_reviewer"), reply_not=(r"\d{3,}",),
          reply_has=(r"не (могу|знаю|подскаж|скажу|располага|владею)|нет (данных|информац|сведений)|к руководител|"
                     r"у руководител|бухгалтер|уточн|не обсужда|не раскрыва",)),
    ]


def scenario(scenario_id: str):
    return next((x for x in scenarios() if x.id == scenario_id), None)


# ----------------------------------------------------------------------------- the director's own cases
JUDGE = ("Ты проверяешь ответ ИИ-сотрудника компании в рабочем мессенджере. Тебе дают сообщение человека, начало "
         "разговора (history), как должен был поступить ИИ-сотрудник по словам руководителя (expected), что он ответил "
         "(reply) и какие действия попросил выполнить (actions). Реши, поступил ли он по сути так, как ожидал "
         "руководитель: слова могут быть другими, важны смысл, действия и тон. Ответь JSON: "
         '{"ok": true или false, "why": "коротко, чего не хватило (или почему хорошо)"}')


def _case_scenario(case) -> tuple:
    who = dict(case.person or {})
    employee = (types.SimpleNamespace(id=who["employee_id"], name=who.get("employee_name") or "")
                if who.get("employee_id") else None)
    person = agent_skills.Person(employee=employee, reviewer=bool(who.get("reviewer")), full=bool(who.get("full")),
                                 director=bool(who.get("director")))
    return dict(case.ctx or {}), person, case.text


def judge(case, decision: dict) -> list:
    """Reasons the answer differs from «как надо было» ([] = it is what the director expected)."""
    reasons = []
    kinds = {a.get("type") for a in decision.get("actions") or [] if isinstance(a, dict)}
    for name in case.must or []:
        if name not in kinds:
            reasons.append(f"не сделал: {name}")
    for name in case.must_not or []:
        if name in kinds:
            reasons.append(f"нельзя было: {name}")
    facts = {"message": case.text, "history": (case.ctx or {}).get("history", [])[-6:], "expected": case.expected,
             "reply": decision.get("reply") or "", "actions": decision.get("actions") or []}
    try:
        raw = llm.chat(str(settings_store.get("report_model") or "google/gemini-2.5-pro"),
                       [{"role": "system", "content": JUDGE},
                        {"role": "user", "content": "<data>" + json.dumps(facts, ensure_ascii=False, default=str) + "</data>"}],
                       json_mode=True, temperature=0.0, max_tokens=400, timeout=90)
        verdict = llm.parse_json(raw)
    except llm.LLMError as exc:
        return reasons + [f"судья недоступен: {str(exc)[:120]}"]
    if not isinstance(verdict, dict) or verdict.get("ok") is not True:
        why = str((verdict or {}).get("why") or "не так, как ожидалось") if isinstance(verdict, dict) else "не так, как ожидалось"
        reasons.append("по мнению судьи: " + why[:300])
    return reasons


QUESTIONS_TOPIC = "Вопросы по компании"
JUDGE_QA = ("Ты проверяешь ответ ИИ-сотрудника компании на вопрос по памяти компании. Тебе дают вопрос (question), "
            "правильный ответ со слов руководителя (expected), ответ ИИ-сотрудника (reply) и записи памяти, которые он "
            "видел (known_facts, about_entities). Ответ верный, если по сути совпадает с правильным: числа, даты, имена "
            "и названия — точно, а фактов, которых нет ни в правильном ответе, ни в записях памяти, нет. Строка "
            "«Источник: …» — ссылка на запись памяти, её не оценивай. Если правильный ответ — что данных нет, верно "
            "честное «не знаю» без догадок. Ответь JSON: "
            '{"ok": true или false, "why": "коротко, что не так (или почему верно)"}')
UNKNOWN_EXPECTED = re.compile(r"не знает|не знаю|нет данных|нет в памяти|не должен|честно|нет информац|не известно|"
                              r"неизвестно|не скажет|не раскрыва", re.I)
MAX_QUESTIONS = 50


def _asker(s, who: str) -> dict:
    """«director» or «employee:<id>» -> the person of a question (the rights are taken when it runs)."""
    if who.startswith("employee:"):
        employee = s.get(Employee, int(who.split(":", 1)[1] or 0))
        if employee is None or not employee.active:
            raise CheckError("Сотрудник не найден")
        return {"employee_id": employee.id, "employee_name": employee.name}
    return {"employee_id": None, "employee_name": "", "director": True}


def add_questions(lines: str, who: str = "director", by: str = "") -> int:
    """Typical questions with the right answers (one a line: «вопрос | правильный ответ»). They are asked over the real
    memory of the company (№20). Returns how many were added."""
    added = 0
    with SessionLocal() as s:
        asker = _asker(s, str(who or "director"))
        have = sum(1 for (ctx,) in s.query(AgentCheckCase.ctx) if (ctx or {}).get("live"))
        for line in str(lines or "").splitlines():
            if "|" not in line:
                continue
            question, expected = (x.strip() for x in line.split("|", 1))
            if not question or not expected:
                continue
            if have + added >= MAX_QUESTIONS:
                break
            s.add(AgentCheckCase(title=question[:120], text=question[:1000], ctx={"live": True}, person=asker,
                                 expected=expected[:2000], must=[], must_not=[], bad_reply="", by=by[:120]))
            added += 1
        s.commit()
    if not added:
        raise CheckError("Нужны строки «вопрос | правильный ответ», например: Кто подписывает акты? | Директор, "
                         "Ирина Соколова")
    return added


def questions() -> list:
    with SessionLocal() as s:
        names = {e.id: e.name for e in s.query(Employee.id, Employee.name)}
        return [{"id": c.id, "text": c.text, "expected": c.expected, "active": c.active, "by": c.by, "at": c.created_at,
                 "who": names.get((c.person or {}).get("employee_id")) or "директор"}
                for c in s.query(AgentCheckCase).order_by(AgentCheckCase.id).all() if (c.ctx or {}).get("live")]


def _live_scenario(case) -> tuple:
    """The question as if this person wrote it now: the real memory, the rights of their card, an empty chat."""
    from . import access
    who = dict(case.person or {})
    with SessionLocal() as s:
        employee = s.get(Employee, who["employee_id"]) if who.get("employee_id") else None
        if employee is not None and not employee.active:
            employee = None
        if employee is None:
            director = reviewer = full = True
        else:
            level = access.level_of(employee)
            director = level == "director"
            reviewer = director or level == "reviewer"
            full = reviewer or level in ("full", "observer")
        director_mode = bool(settings_store.get("director_chat_ids"))
        if director_mode and not director:
            full = False
        person = agent_skills.person_of(s, employee, reviewer, full, director)
        row = types.SimpleNamespace(id=2_000_000_000, chat_id=f"check-question-{case.id}", text=case.text)
        ctx = agent._context(s, row, {"name": employee.name if employee else "Директор"}, employee, reviewer, full,
                             director, director_mode, person=person)
    ctx.update(agent_skills.approval_hint(person))
    return ctx, person, case.text


def judge_answer(case, decision: dict, ctx: dict) -> list:
    """A question about the company: the right answer (a judge model compares), a source, nothing invented."""
    from . import memory
    reply = str(decision.get("reply") or "")
    if not reply.strip():
        return ["пустой ответ"]
    reasons = []
    if not UNKNOWN_EXPECTED.search(case.expected or "") and not (decision.get("sources") or memory._NAMED.search(reply)):
        reasons.append("нет ссылки на источник")
    facts = {"question": case.text, "expected": case.expected, "reply": reply,
             "known_facts": [{"cite": f.get("cite"), "text": f.get("text")} for f in ctx.get("known_facts") or []][:8],
             "about_entities": [{"name": c.get("name"), "facts": [x.get("text") for x in c.get("facts") or []]}
                                for c in ctx.get("about_entities") or []][:3]}
    try:
        raw = llm.chat(str(settings_store.get("report_model") or "google/gemini-2.5-pro"),
                       [{"role": "system", "content": JUDGE_QA},
                        {"role": "user", "content": "<data>" + json.dumps(facts, ensure_ascii=False, default=str) + "</data>"}],
                       json_mode=True, temperature=0.0, max_tokens=400, timeout=90)
        verdict = llm.parse_json(raw)
    except llm.LLMError as exc:
        return reasons + [f"судья недоступен: {str(exc)[:120]}"]
    if not isinstance(verdict, dict) or verdict.get("ok") is not True:
        why = str((verdict or {}).get("why") or "не так, как ожидалось") if isinstance(verdict, dict) else "не так, как ожидалось"
        reasons.append("по мнению судьи: " + why[:300])
    return reasons


def add_case(message_id: int, expected: str, must=(), must_not=(), by: str = "") -> int:
    """A real answer the director did not like becomes a checking dialog: the model sees what it saw then."""
    expected = str(expected or "").strip()
    if not expected:
        raise CheckError("Напишите, как надо было ответить")
    with SessionLocal() as s:
        row = s.get(ConversationMessage, message_id)
        if row is None or row.role != "user":
            raise CheckError("Сообщение не найдено")
        result = dict(row.result or {})
        employee = s.get(Employee, result["employee_id"]) if result.get("employee_id") else None
        reviewer, full, director = bool(result.get("reviewer")), bool(result.get("full_access")), bool(result.get("director"))
        director_mode = bool(settings_store.get("director_chat_ids"))
        if director_mode and not director:
            full = False
        ctx = agent._context(s, row, result, employee, reviewer, full, director, director_mode)
        decision = result.get("decision") or {}
        later = (s.query(ConversationMessage).filter(ConversationMessage.chat_id == row.chat_id,
                                                     ConversationMessage.role == "assistant", ConversationMessage.id > row.id)
                 .order_by(ConversationMessage.id).first())
        known = set(agent_skills.SKILLS)
        case = AgentCheckCase(title=(row.text or "")[:120], text=row.text or "", ctx=json.loads(json.dumps(ctx, default=str)),
                              person={"employee_id": employee.id if employee else None,
                                      "employee_name": employee.name if employee else "", "reviewer": reviewer,
                                      "full": full, "director": director},
                              expected=expected[:2000], must=[x for x in must if x in known],
                              must_not=[x for x in must_not if x in known],
                              bad_reply=(decision.get("reply") or (later.text if later else "") or "")[:4000],
                              source_message_id=row.id, by=by[:120])
        s.add(case)
        s.commit()
        return case.id


def recent_answers(limit: int = 15) -> list:
    """The latest real conversations: what the person wrote, what the AI employee answered and did."""
    with SessionLocal() as s:
        marked = {x for (x,) in s.query(AgentCheckCase.source_message_id).filter(AgentCheckCase.source_message_id.isnot(None))}
        rows = (s.query(ConversationMessage).filter(ConversationMessage.role == "user",
                                                    ConversationMessage.status == "agent_done")
                .order_by(ConversationMessage.id.desc()).limit(limit).all())
        out = []
        for row in rows:
            result = row.result or {}
            decision = result.get("decision") or {}
            out.append({"id": row.id, "name": result.get("name") or "", "text": row.text or "", "at": row.created_at,
                        "reply": decision.get("reply") or "", "actions": decision.get("actions") or [],
                        "model": result.get("model") or "", "transport": result.get("transport") or "",
                        "marked": row.id in marked})
        return out


def export_lines(days: int = 90):
    """The conversations of the last days as JSON lines (chats are numbered, not named by their Telegram ids)."""
    since = now() - dt.timedelta(days=days)
    chats = {}
    with SessionLocal() as s:
        query = (s.query(ConversationMessage).filter(ConversationMessage.created_at >= since,
                                                     ConversationMessage.role.in_(("user", "assistant")),
                                                     ConversationMessage.status.in_(("agent_done", "agent_failed", "agent_pending")))
                 .order_by(ConversationMessage.id))
        for row in query.yield_per(200):
            result = row.result or {}
            chat = chats.setdefault(row.chat_id, f"chat-{len(chats) + 1}")
            line = {"chat": chat, "at": row.created_at.isoformat() if row.created_at else None, "role": row.role,
                    "text": row.text or ""}
            if row.role == "user":
                decision = result.get("decision") or {}
                line.update({"name": result.get("name") or "", "status": row.status,
                             "rights": [r for r in ("director", "reviewer", "full_access") if result.get(r)],
                             "actions": decision.get("actions") or [], "model": result.get("model") or ""})
            yield json.dumps(line, ensure_ascii=False, default=str) + "\n"


# ----------------------------------------------------------------------------- running
_LOCK = threading.Lock()
_ACTIVE = {"run_id": None, "stop": None}


def running_id():
    return _ACTIVE["run_id"]


def _cases() -> list:
    """(key, group, build, evaluate, recall) for every built-in scenario and every active case of the director (its
    model may look through the real chat as it was before that message)."""
    out = [(sc.id, sc.group, sc.build, sc.evaluate, sc.recall) for sc in scenarios()]
    with SessionLocal() as s:
        for case in s.query(AgentCheckCase).filter_by(active=True).order_by(AgentCheckCase.id).all():
            s.expunge(case)
            if (case.ctx or {}).get("live"):  # a question about the company: over the memory as it is now
                out.append((f"case:{case.id}", QUESTIONS_TOPIC, (lambda c=case: _live_scenario(c)),
                            (lambda decision, ctx, c=case: judge_answer(c, decision, ctx) + generic(decision, ctx)), None))
                continue
            source = s.get(ConversationMessage, case.source_message_id) if case.source_message_id else None
            recall = ((lambda query, chat=source.chat_id, before=source.id: dialog_memory.recall(chat, query, before))
                      if source is not None else None)
            out.append((f"case:{case.id}", "Свои случаи", (lambda c=case: _case_scenario(c)),
                        (lambda decision, ctx, c=case: judge(c, decision) + generic(decision, ctx)), recall))
    return out


def start(models, transports, by: str = "") -> int:
    """Starts a run in the background (one at a time). Returns its id."""
    if not config.openrouter_key():
        raise CheckError("Нужен ключ OpenRouter (Настройки → Подключения): проверочные диалоги идут через настоящие модели")
    picked = []
    for model in models:
        model = str(model or "").strip()
        if model and model not in picked and re.fullmatch(r"[\w.\-]+/[\w.:\-]+", model):
            picked.append(model)
    picked = picked[:MAX_MODELS]
    ways = [t for t in agent_skills.TRANSPORTS if t in set(transports or ())]
    if not picked or not ways:
        raise CheckError("Выберите хотя бы одну модель и один способ")
    total = len(picked) * len(ways) * len(_cases())
    with _LOCK:
        if _ACTIVE["run_id"] is not None:
            raise CheckError("Прогон уже идёт — дождитесь конца или остановите его")
        with SessionLocal() as s:
            for stale in s.query(AgentCheckRun).filter_by(status="running").all():  # the service restarted mid-run
                stale.status, stale.finished_at, stale.note = "interrupted", now(), "прерван перезапуском сервиса"
            run = AgentCheckRun(models=picked, transports=ways, total=total, by=by[:120])
            s.add(run)
            s.commit()
            run_id = run.id
        _ACTIVE.update(run_id=run_id, stop=threading.Event())
    if config.TESTING:
        _run(run_id)
    else:
        threading.Thread(target=_run, args=(run_id,), name="agent-checks", daemon=True).start()
    return run_id


def stop() -> bool:
    event = _ACTIVE.get("stop")
    if _ACTIVE["run_id"] is None or event is None:
        return False
    event.set()
    return True


def _one(run_id: int, model: str, transport: str, item, halt) -> None:
    key, group, build, evaluate, recall = item
    if halt.is_set():
        return
    ctx, person, text = build()
    started = time.monotonic()
    decision, error = None, ""
    with llm.meter() as box:
        try:
            decision = agent.decide(ctx, text, person, model=model, transport=transport, approvers=APPROVERS,
                                    label=f"check {key}", recall=recall)
        except Exception as exc:  # noqa: BLE001  a model that fails is a result too
            error = str(exc)
    seconds = time.monotonic() - started
    if "лимит расходов" in error:
        halt.set()  # the daily limit is spent: the rest would only fail the same way
        return
    if decision is not None:
        try:
            reasons = evaluate(decision, ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("check %s not evaluated", key)
            reasons = [f"не удалось оценить: {exc}"]
    else:
        reasons = ["модель не дала годного ответа: " + error[:240]]
    with SessionLocal() as s:
        s.add(AgentCheckResult(run_id=run_id, model=model, transport=transport, case_key=key, topic=group[:60],
                               ok=not reasons, reasons=reasons, reply=str((decision or {}).get("reply") or "")[:4000],
                               actions=json.loads(json.dumps((decision or {}).get("actions") or [], default=str)),
                               via=str((decision or {}).get("transport")
                                       or ("json" if transport == "tools" and model in agent._NO_TOOLS else transport)),
                               cost=round(box["cost"], 6),
                               calls=box["calls"], tokens=box["prompt_tokens"] + box["completion_tokens"],
                               seconds=round(seconds, 2)))
        s.commit()


def _run(run_id: int) -> None:
    halt = _ACTIVE.get("stop") or threading.Event()
    status, note = "done", ""
    try:
        with SessionLocal() as s:
            run = s.get(AgentCheckRun, run_id)
            models, ways = list(run.models), list(run.transports)
        items = _cases()
        jobs = [(model, way, item) for model in models for way in ways for item in items]
        if config.TESTING:
            for model, way, item in jobs:
                _one(run_id, model, way, item, halt)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="check") as pool:
                for future in [pool.submit(_one, run_id, model, way, item, halt) for model, way, item in jobs]:
                    future.result()
        if halt.is_set():
            status = "stopped"
            note = "остановлен" if not _limit_hit(run_id) else "остановлен: исчерпан дневной лимит расходов на ИИ"
    except Exception as exc:  # noqa: BLE001
        log.exception("checking dialogs %s failed", run_id)
        status, note = "failed", str(exc)[:500]
    finally:
        with SessionLocal() as s:
            run = s.get(AgentCheckRun, run_id)
            if run is not None:
                run.status, run.finished_at, run.note = status, now(), note
                s.commit()
        with _LOCK:
            if _ACTIVE["run_id"] == run_id:
                _ACTIVE.update(run_id=None, stop=None)


def _limit_hit(run_id: int) -> bool:
    limit = llm.budget_usd()
    return bool(limit) and llm.spent_today() >= limit


# ----------------------------------------------------------------------------- the comparison
def monthly_messages() -> tuple:
    """(messages a month, counted from the last 30 days?) — the price a month is the price of a message times this."""
    since = now() - dt.timedelta(days=30)
    with SessionLocal() as s:
        count = (s.query(ConversationMessage).filter(ConversationMessage.role == "user",
                                                     ConversationMessage.created_at >= since,
                                                     ConversationMessage.status.in_(("agent_done", "agent_failed",
                                                                                     "agent_pending"))).count())
    return (count, True) if count >= 30 else (ASSUMED_MONTH, False)


def budget() -> float:
    try:
        return max(0.0, float(settings_store.get("agent_budget_month_usd") or 0))
    except (TypeError, ValueError):
        return 0.0


def summary(run_id: int) -> dict:
    """Per model and way: passed, the price of a message and of a month, the time; where they fail."""
    titles = {sc.id: sc.title for sc in scenarios()}
    with SessionLocal() as s:
        run = s.get(AgentCheckRun, run_id)
        if run is None:
            return {}
        rows = s.query(AgentCheckResult).filter_by(run_id=run_id).order_by(AgentCheckResult.id).all()
        cases = {f"case:{c.id}": c.title or c.text[:80] for c in s.query(AgentCheckCase).all()}
        info = {"id": run.id, "status": run.status, "created": run.created_at, "finished": run.finished_at,
                "models": list(run.models), "transports": list(run.transports), "total": run.total, "by": run.by,
                "note": run.note, "done": len(rows)}
    month, counted = monthly_messages()
    limit = budget()
    current = (str(settings_store.get("agent_model") or ""), agent_skills.transport())
    pairs = []
    groups = collections.OrderedDict()
    by_case = collections.OrderedDict()
    for model in info["models"]:
        for way in info["transports"]:
            mine = [r for r in rows if r.model == model and r.transport == way]
            if not mine:
                continue
            passed = sum(r.ok for r in mine)
            cost = sum(r.cost for r in mine)
            per_message = cost / len(mine) if cost > 0 else None
            fell_back = sum(1 for r in mine if r.via and r.via != way)
            pairs.append({"model": model, "transport": way, "label": f"{model} · {agent_skills.TRANSPORT_RU[way]}",
                          "count": len(mine), "passed": passed, "pct": round(100 * passed / len(mine)),
                          "seconds": round(statistics.median(r.seconds for r in mine), 1),
                          "per_100": per_message * 100 if per_message is not None else None,
                          "per_month": per_message * month if per_message is not None else None,
                          "over_budget": bool(limit and per_message is not None and per_message * month > limit),
                          "errors": sum(1 for r in mine if any(x.startswith("модель не дала") for x in r.reasons or [])),
                          "fell_back": fell_back, "current": (model, way) == current})
            for r in mine:
                groups.setdefault(r.topic or "—", {}).setdefault((model, way), [0, 0])
                groups[r.topic or "—"][(model, way)][0] += r.ok
                groups[r.topic or "—"][(model, way)][1] += 1
                entry = by_case.setdefault(r.case_key, {"key": r.case_key, "title": titles.get(r.case_key) or cases.get(r.case_key, r.case_key),
                                                        "group": r.topic, "fails": [], "runs": 0})
                entry["runs"] += 1
                if not r.ok:
                    entry["fails"].append({"label": f"{model} · {agent_skills.TRANSPORT_RU[way]}", "reasons": r.reasons or [],
                                           "reply": r.reply, "actions": r.actions})
    if pairs:
        best = max(p["pct"] for p in pairs)
        for p in pairs:
            p["best"] = p["pct"] == best
        fit = [p for p in pairs if p["pct"] >= best - 5 and not p["over_budget"]]
        if fit:
            pick = min(fit, key=lambda p: (p["per_month"] is None, p["per_month"] or 0, -p["pct"]))
            pick["recommended"] = True
    keys = [(p["model"], p["transport"]) for p in pairs]
    group_rows = [{"group": g, "cells": [(cells.get(k) or [0, 0]) for k in keys]} for g, cells in groups.items()]
    failing = sorted((c for c in by_case.values() if c["fails"]), key=lambda c: -len(c["fails"]))
    return {"run": info, "pairs": pairs, "groups": group_rows, "failing": failing, "month": month, "counted": counted,
            "budget": limit}


def runs(limit: int = 8) -> list:
    with SessionLocal() as s:
        return [{"id": r.id, "status": r.status, "created": r.created_at, "models": list(r.models),
                 "transports": list(r.transports), "total": r.total, "by": r.by,
                 "done": s.query(AgentCheckResult).filter_by(run_id=r.id).count()}
                for r in s.query(AgentCheckRun).order_by(AgentCheckRun.id.desc()).limit(limit)]


def custom_cases() -> list:
    with SessionLocal() as s:
        return [{"id": c.id, "title": c.title, "text": c.text, "expected": c.expected, "must": c.must or [],
                 "must_not": c.must_not or [], "bad_reply": c.bad_reply, "by": c.by, "at": c.created_at, "active": c.active}
                for c in s.query(AgentCheckCase).order_by(AgentCheckCase.id.desc()).all() if not (c.ctx or {}).get("live")]
