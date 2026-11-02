"""Built-in skills of the AI employee: everything it can do in a conversation, described once.

A skill says when to use it (the model reads this), which data it takes (a JSON schema for function calls and a template
for the JSON answer), who may ask for it, how code carries it out, and in which mode: by itself, after the director's
approval or only as a suggestion to the director. Slow and approved work goes through the case engine (cases.py), so it
is seen on the Progress page, waits for a decision there and reports when it is done.

The model only proposes; `check` compares every call with the data the model was shown and the person's rights, and
nothing happens before that. Both ways of asking the model use the same skills: a JSON answer with "actions", or the
function calling of OpenRouter (`tools`).
"""
import dataclasses
import datetime as dt
import json
import logging

from . import bitrix, cases, deadlines, meeting_alerts, reminders, settings_store, userbot
from .db import ConversationEscalation, ConversationMessage, Employee, Meeting, Note, SessionLocal, Task, now

log = logging.getLogger("sysai.agent")

MODES = ("auto", "approve", "suggest")
MODE_RU = {"auto": "сразу", "approve": "с одобрением руководителя", "suggest": "только предложить"}
MODE_HINT = {"auto": "выполняет сразу", "approve": "сначала спрашивает руководителя (ответ «да» в чате или «Одобрить» "
                                                   "на странице «Прогресс»)",
             "suggest": "только предлагает руководителю, сам не делает"}
WHO_RU = {"anyone": "любой собеседник", "employee": "сотрудник из справочника", "full": "полный доступ",
          "reviewer": "проверяющий", "assign": "проверяющий с полным доступом"}
TRANSPORTS = ("json", "tools")
TRANSPORT_RU = {"json": "ответ в JSON", "tools": "вызов функций"}
WALL = r"нет доступа|недостаточно прав|нет прав|запрещено|не уполномочен"


class SkillError(RuntimeError):
    """The skill cannot be carried out now (the task is gone, nobody got the message)."""


@dataclasses.dataclass(frozen=True)
class Person:
    """Who writes: a directory employee (or nobody) and their rights."""
    employee: object = None
    reviewer: bool = False
    full: bool = False
    director: bool = False

    @property
    def leader(self) -> bool:
        """Leaders approve what needs approval, so what they ask for runs at once."""
        return bool(self.reviewer or self.director)

    @property
    def employee_id(self):
        return getattr(self.employee, "id", None) if self.employee is not None else None


@dataclasses.dataclass(frozen=True)
class Skill:
    name: str
    title: str          # for people: the Skills page, the plan of an approval case
    hint: str           # for people: when the skill is used, one line
    when: str           # for the model: when to use it (the JSON answer and the function description)
    template: str       # the form of the action in the JSON answer
    params: dict        # JSON schema properties of the function
    required: tuple = ()
    who: str = "anyone"
    modes: bool = False      # the director may require an approval for it (Skills page)
    after_reply: bool = False  # sent after the reply (the PDF report)


def _int(description: str) -> dict:
    return {"type": "integer", "description": description}


def _str(description: str, enum=None) -> dict:
    out = {"type": "string", "description": description}
    if enum:
        out["enum"] = list(enum)
    return out


SKILLS = {s.name: s for s in (
    Skill("set_deadline", "Поставить срок", "Сотрудник назвал дату для своей задачи без срока",
          "сотрудник назвал конкретную дату, а у его задачи нет срока",
          '{"type":"set_deadline","task_id":ID,"date":"ГГГГ-ММ-ДД"}',
          {"task_id": _int("ID задачи из employee.tasks, у которой can_set_deadline=true"),
           "date": _str("срок в формате ГГГГ-ММ-ДД")},
          ("task_id", "date"), who="employee", modes=True),
    Skill("task_status", "Отметить ход задачи", "Человек рассказал, как идёт задача: в работе, готово, мешает",
          "человек сообщил, как идёт задача",
          '{"type":"task_status","task_id":ID,"status":"in_progress|done|blocked","note":"что сделано или что мешает"}',
          {"task_id": _int("ID задачи из данных"),
           "status": _str("in_progress — в работе, done — выполнена, blocked — мешает", ("in_progress", "done", "blocked")),
           "note": _str("что сделано или что мешает")},
          ("task_id", "status"), modes=True),
    Skill("note", "Запомнить факт", "Кто что сделал, пообещал или сказал — в заметки о людях и задачах",
          "запомнить факт о том, кто что сделал, пообещал или сказал (для себя, не для пересылки)",
          '{"type":"note","text":"...","employee_id":ID или null,"task_id":ID или null,"kind":"fact|status|commitment|done"}',
          {"text": _str("сам факт, коротко"),
           "employee_id": _int("о ком из team; не указывай, если о самом собеседнике"),
           "task_id": _int("к какой задаче относится; не указывай, если ни к какой"),
           "kind": _str("fact — факт, status — как идёт работа, commitment — обещание, done — сделано",
                        ("fact", "status", "commitment", "done"))},
          ("text",), modes=True),
    Skill("ask_reviewer", "Спросить руководителя", "Ответа нет в данных или сотрудник просит изменить условия задачи",
          "сотруднику нужен ответ, которого нет в данных, или он просит изменить условия задачи (срок, выезд раньше, "
          "водитель, машина, состав) — это решает руководитель, не ты",
          '{"type":"ask_reviewer","task_id":ID или null,"question":"суть","message":"точный текст проверяющему"}',
          {"task_id": _int("ID задачи сотрудника, к которой вопрос; не указывай, если ни к какой"),
           "question": _str("суть вопроса коротко"),
           "message": _str("точный текст проверяющему: кто спрашивает, по какой задаче, что нужно")},
          ("question", "message"), who="employee"),
    Skill("relay_answer", "Передать ответ сотруднику", "Проверяющий ответил на вопрос сотрудника",
          "проверяющий ответил на вопрос сотрудника",
          '{"type":"relay_answer","question_id":ID,"message":"точный текст сотруднику"}',
          {"question_id": _int("ID вопроса из open_questions_from_employees"),
           "message": _str("точный текст сотруднику")},
          ("question_id", "message"), who="reviewer"),
    Skill("approve_meeting", "Отправить совещание", "Проверяющий согласился разослать протокол и задачи",
          "проверяющий прямо согласился отправить именно это совещание (если совещаний несколько и неясно какое, спроси)",
          '{"type":"approve_meeting","meeting_id":ID}',
          {"meeting_id": _int("ID совещания из meetings_awaiting_approval")}, ("meeting_id",), who="reviewer"),
    Skill("reject_meeting", "Отклонить совещание", "Проверяющий отклонил протокол",
          "проверяющий прямо отклонил именно это совещание (если совещаний несколько и неясно какое, спроси)",
          '{"type":"reject_meeting","meeting_id":ID}',
          {"meeting_id": _int("ID совещания из meetings_awaiting_approval")}, ("meeting_id",), who="reviewer"),
    Skill("assign_task", "Назначить исполнителя", "Проверяющий решил, кто делает задачу (или ассистент сам)",
          'проверяющий решил, кто делает задачу (to="assistant" значит, что задачу выполнишь ты сам: график, таблица, '
          "сводка, отчёт, напоминание, запрос статуса; так же повторяют задачу ассистента со статусом assistant_failed)",
          '{"type":"assign_task","task_id":ID,"to":ID сотрудника или "assistant","message":"текст сотруднику, если '
          'совещание уже отправлено"}',
          {"task_id": _int("ID задачи из unassigned_tasks, team[].open_tasks или assistant_tasks"),
           "to": _str('ID сотрудника из team (число) или "assistant"'),
           "message": _str("текст сотруднику, если совещание уже отправлено")},
          ("task_id", "to"), who="assign"),
    Skill("deliver_files", "Переслать файлы", "Переслать или упаковать в ZIP присланные файлы и отправить сотруднику",
          "человек с полным доступом просит переслать, передать, упаковать или отправить файлы сотруднику («перешли "
          "ему», «запакуй и отправь зипом»). Код соберёт ZIP (если zip=true), отправит и доложит, когда Telegram "
          "подтвердит доставку. «Ему/ей» ищи в recent_cases и в разговоре. Бери все файлы, которые человек прислал для "
          "этого, а не один. ОБЕЩАНИЕ = ДЕЙСТВИЕ: не пиши «перешлю», «упакую», «отправлю», «передам», если в actions нет "
          "deliver_files (или другого действия, которое это делает); если не хватает данных (кому, какие файлы) — спроси",
          '{"type":"deliver_files","to":ID сотрудника,"document_ids":[ID из files_received или recent_files],'
          '"zip":true|false,"archive_name":"имя архива","note":"по какому делу"}',
          {"to": _int("ID сотрудника из team"),
           "document_ids": {"type": "array", "items": {"type": "integer"},
                            "description": "ID файлов из files_received, file_received или recent_files"},
           "zip": {"type": "boolean", "description": "упаковать в ZIP"},
           "archive_name": _str("имя архива"), "note": _str("по какому делу")},
          ("to", "document_ids"), who="full", modes=True),
    Skill("team_report", "Отчёт по команде (PDF)", "Кто что делает: PDF из базы приходит после ответа",
          "человек с полным доступом просит отчёт о том, кто что делает (код соберёт красивый PDF из базы и пришлёт "
          "после твоего ответа, в reply коротко скажи главное)",
          '{"type":"team_report","scope":"all или ID сотрудника"}',
          {"scope": _str("all — вся команда, или ID сотрудника из team")}, ("scope",), who="full", modes=True,
          after_reply=True),
    Skill("remind", "Напомнить", "Напомнить себе, всем, проверяющим или выбранным сотрудникам",
          "человек просит кому-то напомнить (например, о совещании). meeting=true, если напоминание о совещании, встрече "
          "или планёрке: напоминание, которое поставлено в личном кабинете и ещё не сработало, тогда снимается для этих "
          "людей, чтобы не писать им дважды (для остальных тем meeting не указывай). at это время напоминания по "
          "now.local (сегодня в 15:00, за час до совещания, завтра утром считай сам от now), null значит сразу. to=me: "
          "себе, это может любой; остальным (all это все сотрудники из справочника, reviewers это проверяющие) "
          "напоминают только проверяющий и люди с полным доступом. Текст напоминания потом напишешь сам, в reply скажи "
          "кому и когда напомнишь; если неясно кому, когда или о чём, сначала спроси",
          '{"type":"remind","to":"me" или "all" или "reviewers" или [ID сотрудников из команды],"at":"ГГГГ-ММ-ДД ЧЧ:ММ" '
          'или null,"topic":"о чём напомнить, коротко","details":"время и место, что подготовить, если назвали",'
          '"meeting":true или false}',
          {"to": _str("me — себе, all — всем сотрудникам, reviewers — проверяющим, people — сотрудникам из people",
                      ("me", "all", "reviewers", "people")),
           "people": {"type": "array", "items": {"type": "integer"}, "description": "ID сотрудников из team, если to=people"},
           "at": _str("время напоминания ГГГГ-ММ-ДД ЧЧ:ММ по now.local; не указывай, чтобы напомнить сразу"),
           "topic": _str("о чём напомнить, коротко"),
           "details": _str("время и место, что подготовить, если назвали"),
           "meeting": {"type": "boolean", "description": "true, если напоминание о совещании, встрече или планёрке"}},
          ("to", "topic"), modes=True),
    Skill("cancel_reminder", "Отменить напоминание", "Снять ещё не сработавшее напоминание",
          "отменить напоминание из reminders_pending",
          '{"type":"cancel_reminder","reminder_id":ID}',
          {"reminder_id": _int("ID напоминания из reminders_pending")}, ("reminder_id",), modes=True),
    Skill("approve_case", "Одобрить дело", "Руководитель разрешил план дела, пересылку документа или повтор шага",
          "руководитель прямо разрешил начать дело, переслать документ или повторить остановленный шаг из "
          "cases_awaiting_decision (если дел несколько и неясно какое, спроси)",
          '{"type":"approve_case","case_id":ID}',
          {"case_id": _int("ID дела из cases_awaiting_decision")}, ("case_id",), who="reviewer"),
    Skill("cancel_case", "Отменить дело", "Руководитель отменил дело ассистента",
          "руководитель прямо отменил дело из cases_awaiting_decision или из разговора (если неясно какое, спроси)",
          '{"type":"cancel_case","case_id":ID}',
          {"case_id": _int("ID дела")}, ("case_id",), who="reviewer"),
)}

MEMORY_TOOL = {"type": "function", "function": {
    "name": "update_memory",
    "description": "Ты узнал что-то важное о человеке или его работе: сохрани обновлённую краткую память о нём "
                   "(до 1200 символов, сохрани важное из прошлой памяти). Не вызывай, если нового нет.",
    "parameters": {"type": "object", "properties": {"summary": _str("обновлённая краткая память о человеке")},
                   "required": ["summary"]}}}
# Not a skill: the model looks through the whole history of the chat and answers with what was found (agent._decide).
RECALL_TOOL = {"type": "function", "function": {
    "name": "recall_conversation",
    "description": "Вспомнить: найти в полной истории этого чата старые сообщения по словам, когда нужного нет в "
                   "history, dialog_memory и from_earlier. Найденное придёт результатом функции, после этого ответь.",
    "parameters": {"type": "object", "properties": {"query": _str("что искать: ключевые слова, названия, номера")},
                   "required": ["query"]}}}


# ----------------------------------------------------------------------------- who and how
def available(skill: Skill, person: Person) -> bool:
    if skill.who == "employee":
        return person.employee is not None
    if skill.who == "full":
        return bool(person.full)
    if skill.who == "reviewer":
        return bool(person.reviewer)
    if skill.who == "assign":
        return bool(person.reviewer and person.full)
    return True


def modes() -> dict:
    """The mode of every skill that may need an approval (the director sets them on the Skills page)."""
    stored = settings_store.get("agent_skill_modes") or {}
    stored = stored if isinstance(stored, dict) else {}
    return {name: (stored.get(name) if stored.get(name) in MODES else "auto") for name, s in SKILLS.items() if s.modes}


def mode_for(name: str, person: Person) -> str:
    """What the director asks for is never sent to the director for approval."""
    if person.leader:
        return "auto"
    return modes().get(name, "auto")


def approval_hint(person: Person) -> dict:
    """For the model: which of this person's requests go to the director first (nothing when everything runs at once)."""
    if person.leader:
        return {}
    waiting = {name: ("с одобрением руководителя" if mode == "approve" else "только предложение руководителю")
               for name, mode in modes().items() if mode != "auto" and available(SKILLS[name], person)}
    return {"skills_with_approval": waiting} if waiting else {}


def transport() -> str:
    value = settings_store.get("agent_transport")
    return value if value in TRANSPORTS else "json"


# ----------------------------------------------------------------------------- what the model reads
def json_help() -> str:
    """The skills in the JSON answer: the form of each action and when to use it."""
    return "".join(f"{s.template}: {s.when}; " for s in SKILLS.values())


def tools(person: Person) -> list:
    """Function definitions for OpenRouter: only the skills this person may ask for, plus the memory."""
    out = []
    for skill in SKILLS.values():
        if not available(skill, person):
            continue
        out.append({"type": "function", "function": {
            "name": skill.name, "description": f"{skill.title}. Когда: {skill.when}.",
            "parameters": {"type": "object", "properties": skill.params, "required": list(skill.required)}}})
    out.append(MEMORY_TOOL)
    return out


_INT_FIELDS = ("task_id", "employee_id", "question_id", "meeting_id", "reminder_id", "case_id")


def _as_int(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("#").isdigit():
        return int(value.strip().lstrip("#"))
    return value


def normalize(action: dict) -> dict:
    """IDs that came as text ("12"), the list form of `remind.to` from a function call, an empty time."""
    out = dict(action)
    for key in _INT_FIELDS:
        if key in out:
            out[key] = _as_int(out[key])
    kind = out.get("type")
    if kind == "deliver_files":
        out["to"] = _as_int(out.get("to"))
        if isinstance(out.get("document_ids"), list):
            out["document_ids"] = [_as_int(x) for x in out["document_ids"]]
    elif kind == "assign_task":
        out["to"] = "assistant" if str(out.get("to") or "").strip().lower() == "assistant" else _as_int(out.get("to"))
    elif kind == "team_report":
        scope = out.get("scope")
        out["scope"] = "all" if scope in (None, "") or str(scope).strip().lower() == "all" else _as_int(scope)
    elif kind == "remind":
        people = out.pop("people", None)
        if out.get("to") == "people":
            out["to"] = [_as_int(x) for x in people] if isinstance(people, list) else people
        elif isinstance(out.get("to"), list):
            out["to"] = [_as_int(x) for x in out["to"]]
        if out.get("at") == "":
            out["at"] = None
    return out


def from_calls(calls: list) -> tuple:
    """(actions, memory) from the function calls of one answer."""
    actions, memory = [], ""
    for call in calls or []:
        name = str(call.get("name") or "")
        raw = call.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw if isinstance(raw, dict) else {})
        except json.JSONDecodeError:
            actions.append({"type": name, "_error": "аргументы не в формате JSON"})
            continue
        if not isinstance(args, dict):
            actions.append({"type": name, "_error": "аргументы должны быть объектом"})
            continue
        if name == "update_memory":
            memory = str(args.get("summary") or "").strip()
            continue
        args.pop("type", None)
        actions.append(normalize({"type": name, **args}))
    return actions, memory


# ----------------------------------------------------------------------------- checking a call (no database here)
def _known_task_ids(ctx: dict, *, own_only: bool = False) -> set:
    ids = {t["id"] for t in (ctx.get("employee") or {}).get("tasks", [])}
    if not own_only:
        for person in ctx.get("team", []):
            ids |= {t["id"] for t in person["open_tasks"]}
        ids |= {t["id"] for t in ctx.get("unassigned_tasks", [])}
    return ids


def check(action: dict, ctx: dict, person: Person, approvers=None) -> str:
    """An error text, or "" when the action may run. `approvers` replaces the setting (the checking dialogs)."""
    employee, reviewer, full = person.employee, person.reviewer, person.full
    approvers = list(settings_store.get("approver_chat_ids") or []) if approvers is None else list(approvers)
    kind = action.get("type")
    if action.get("_error"):
        return f"{kind}: {action['_error']}"
    if kind == "set_deadline":
        if employee is None:
            return "set_deadline доступно только сотруднику"
        ids = {t["id"] for t in ctx["employee"]["tasks"] if t["can_set_deadline"]}
        if action.get("task_id") not in ids:
            return "set_deadline: задача не найдена среди задач без срока"
        try:
            date = dt.date.fromisoformat(str(action.get("date")))
        except ValueError:
            return "set_deadline: дата должна быть в формате ГГГГ-ММ-ДД"
        return "" if date >= deadlines.today() else "set_deadline: дата уже прошла"
    if kind == "task_status":
        if action.get("status") not in ("in_progress", "done", "blocked"):
            return "task_status: статус in_progress, done или blocked"
        ids = _known_task_ids(ctx, own_only=not full)
        return "" if action.get("task_id") in ids else "task_status: задача не найдена среди доступных"
    if kind == "note":
        if not str(action.get("text") or "").strip():
            return "note: нужен text"
        if action.get("kind", "fact") not in ("fact", "status", "commitment", "done"):
            return "note: kind fact, status, commitment или done"
        employee_id = action.get("employee_id")
        if employee is not None and not full:
            if employee_id not in (None, employee.id):
                return "note: сотрудник может писать заметки только о себе"
            if action.get("task_id") is not None and action["task_id"] not in _known_task_ids(ctx, own_only=True):
                return "note: задача не найдена среди задач сотрудника"
        elif employee_id is not None and employee_id not in {p["id"] for p in ctx.get("team", [])}:
            return "note: сотрудник не найден в команде"
        return ""
    if kind == "ask_reviewer":
        if employee is None:
            return "ask_reviewer доступно только сотруднику"
        if not approvers:
            return "ask_reviewer: проверяющие не указаны в настройках"
        if not str(action.get("message") or "").strip() or not str(action.get("question") or "").strip():
            return "ask_reviewer: нужны question и message"
        task_ids = {t["id"] for t in ctx["employee"]["tasks"]}
        if action.get("task_id") is not None and action.get("task_id") not in task_ids:
            return "ask_reviewer: задача не найдена среди задач сотрудника"
        return "" if task_ids else "ask_reviewer: у сотрудника нет задач, вопрос не к чему привязать"
    if kind == "relay_answer":
        if not reviewer:
            return "relay_answer доступно только проверяющему"
        if action.get("question_id") not in {q["id"] for q in ctx.get("open_questions_from_employees", [])}:
            return "relay_answer: вопрос не найден среди открытых"
        return "" if str(action.get("message") or "").strip() else "relay_answer: нужен message"
    if kind in ("approve_meeting", "reject_meeting"):
        if not reviewer:
            return f"{kind} доступно только проверяющему"
        ids = {m["id"] for m in ctx.get("meetings_awaiting_approval", [])}
        return "" if action.get("meeting_id") in ids else f"{kind}: совещание не ждёт проверки"
    if kind == "assign_task":
        if not (reviewer and full):
            return "assign_task доступно только проверяющему"
        task = next((t for t in ctx.get("unassigned_tasks", []) if t["id"] == action.get("task_id")), None)
        if task is None:
            task = next((t for p in ctx.get("team", []) for t in p["open_tasks"] if t["id"] == action.get("task_id")), None)
        again = next((t for t in ctx.get("assistant_tasks", []) if t["id"] == action.get("task_id")
                      and t["status"] in ("assistant_failed", "assistant_done")), None)
        if task is None and again is None:
            return "assign_task: задача не найдена среди доступных"
        target = action.get("to")
        if target != "assistant" and target not in {p["id"] for p in ctx.get("team", [])}:
            return 'assign_task: to должен быть ID сотрудника из команды или "assistant"'
        if target != "assistant" and task is not None and task.get("status") in ("sent", "in_progress", "blocked",
                                                                                 "awaiting_deadline") \
                and not str(action.get("message") or "").strip():
            return "assign_task: совещание уже отправлено, нужен message сотруднику"
        return ""
    if kind == "remind":
        if not str(action.get("topic") or "").strip():
            return "remind: нужен topic (о чём напомнить)"
        if action.get("meeting") not in (None, True, False):
            return "remind: meeting это true или false"
        to = action.get("to")
        if to != "me":
            if not full:
                return 'remind: сотрудник может попросить напомнить только себе (to: "me")'
            team = {p["id"]: p for p in ctx.get("team", [])}
            if to == "all":
                if not any(p.get("reachable") for p in team.values()):
                    return "remind: ни у кого из сотрудников нет Telegram-контакта в справочнике"
            elif to == "reviewers":
                if not approvers:
                    return "remind: проверяющие не указаны в настройках"
            elif isinstance(to, list) and to and all(isinstance(x, int) and not isinstance(x, bool) for x in to):
                missing = [x for x in to if x not in team]
                if missing:
                    return f"remind: сотрудники {missing} не найдены в команде"
                silent = [team[x]["name"] for x in to if not team[x].get("reachable")]
                if silent:
                    return f"remind: у {', '.join(silent)} нет Telegram-контакта в справочнике, написать нельзя"
            else:
                return 'remind: to это "me", "all", "reviewers" или список ID сотрудников'
        try:
            reminders.parse_when(action.get("at"))
        except reminders.WhenError as exc:
            return f"remind: {exc}"
        return ""
    if kind == "cancel_reminder":
        ids = {r["id"] for r in ctx.get("reminders_pending", [])}
        return "" if action.get("reminder_id") in ids else "cancel_reminder: напоминание не найдено среди ожидающих"
    if kind in ("approve_case", "cancel_case"):
        if not reviewer:
            return f"{kind} доступно только руководителю"
        ids = {c["id"] for c in ctx.get("cases_awaiting_decision", [])}
        if kind == "cancel_case":
            return "" if isinstance(action.get("case_id"), int) else "cancel_case: укажи case_id"
        return "" if action.get("case_id") in ids else "approve_case: дело не найдено среди ждущих решения"
    if kind == "deliver_files":
        if not full:
            return "deliver_files доступно только проверяющему и людям с полным доступом"
        team = {p["id"]: p for p in ctx.get("team", [])}
        if action.get("to") not in team:
            return "deliver_files: to должен быть ID сотрудника из команды"
        if not team[action["to"]].get("reachable"):
            return f"deliver_files: у {team[action['to']]['name']} нет Telegram-контакта в справочнике"
        ids = action.get("document_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(x, int) and not isinstance(x, bool) for x in ids):
            return "deliver_files: document_ids — непустой список ID файлов"
        known = {d["id"] for d in ctx.get("recent_files", [])}
        known |= {d.get("document_id") for d in ctx.get("files_received", []) if isinstance(d, dict)}
        if isinstance(ctx.get("file_received"), dict):
            known.add(ctx["file_received"].get("document_id"))
        missing = [x for x in ids if x not in known]
        if missing:
            return f"deliver_files: файлы {missing[:5]} не найдены среди присланных этим человеком (recent_files)"
        if len(set(ids)) > cases.MAX_DELIVERY_FILES:
            return f"deliver_files: не больше {cases.MAX_DELIVERY_FILES} файлов за раз"
        if action.get("zip") not in (None, True, False):
            return "deliver_files: zip это true или false"
        return ""
    if kind == "team_report":
        if not full:
            return "team_report доступно только проверяющему и людям с полным доступом"
        scope = action.get("scope")
        return "" if scope == "all" or scope in {p["id"] for p in ctx.get("team", [])} else "team_report: scope all или ID сотрудника"
    return f"неизвестное действие {kind!r}"


def denied(action: dict, person: Person) -> bool:
    """The action needs more than this person is allowed (as opposed to a mistake in the action itself)."""
    kind = action.get("type")
    employee = person.employee
    if kind in ("team_report", "assign_task", "deliver_files"):
        return not person.full
    if kind == "remind":
        return action.get("to") != "me" and not person.full
    if kind in ("relay_answer", "approve_meeting", "reject_meeting", "approve_case", "cancel_case"):
        return not person.reviewer
    if kind == "note":
        return action.get("employee_id") not in (None, employee.id if employee else None) and not person.full
    return False


# ----------------------------------------------------------------------------- carrying it out (short sessions)
def _remember(s, text: str, *, employee_id=None, task_id=None, meeting_id=None, kind="fact", source="conversation", author=""):
    s.add(Note(employee_id=employee_id, task_id=task_id, meeting_id=meeting_id, kind=kind, text=str(text).strip()[:800],
               source=source, author=author[:80]))


def _employee_ref(s, chat: str):
    employee = s.query(Employee).filter_by(telegram_user_id=chat).first()
    return (employee.telegram_chat_id if employee and employee.telegram_chat_id else chat)


def _testing() -> bool:
    from . import config
    return config.TESTING


def _task(s, task_id):
    task = s.get(Task, task_id) if task_id is not None else None
    if task is None:
        raise SkillError("задача не найдена: её удалили или объединили с другой")
    return task


def execute(action: dict, chat: str, employee_id, author: str, sender: dict | None = None, full: bool = False) -> None:
    """Carries out one checked action now."""
    from . import assistant, pipeline
    kind = action["type"]
    with SessionLocal() as s:
        if kind == "set_deadline":
            task = _task(s, action["task_id"])
            task.deadline, task.deadline_source = dt.date.fromisoformat(action["date"]), "asked"
            if task.status == "awaiting_deadline":
                task.status = "sent"
            if task.bitrix_task_id:
                try:
                    bitrix.update_deadline(task)
                except Exception as exc:  # noqa: BLE001
                    log.warning("bitrix update failed: %s", exc)
            _remember(s, f"Срок по задаче «{task.title}»: {action['date']}", employee_id=task.assignee_id, task_id=task.id,
                      kind="commitment", author=author)
            s.commit()
        elif kind == "task_status":
            task = _task(s, action["task_id"])
            task.status = action["status"]
            note = str(action.get("note") or "").strip()
            label = {"in_progress": "в работе", "done": "выполнена", "blocked": "заблокирована"}[action["status"]]
            _remember(s, f"Задача «{task.title}»: {label}" + (f". {note}" if note else ""), employee_id=task.assignee_id,
                      task_id=task.id, kind="done" if action["status"] == "done" else "status", author=author)
            s.commit()
        elif kind == "note":
            target = action.get("employee_id") if action.get("employee_id") is not None else employee_id
            _remember(s, action["text"], employee_id=target, task_id=action.get("task_id"),
                      kind=action.get("kind", "fact"), author=author)
            s.commit()
        elif kind == "ask_reviewer":
            task = s.get(Task, action["task_id"]) if action.get("task_id") is not None else None
            latest = s.query(Task).filter(Task.assignee_id == employee_id).order_by(Task.id.desc()).first()
            meeting_id = task.meeting_id if task else (latest.meeting_id if latest else None)
            s.add(ConversationEscalation(meeting_id=meeting_id, task_id=task.id if task else None, chat_id=chat,
                                         reason=str(action["question"]).strip()[:1000], status="open"))
            _remember(s, f"Вопрос проверяющему: {str(action['question']).strip()}", employee_id=employee_id,
                      task_id=task.id if task else None, kind="status", source="system", author=author)
            s.commit()
            delivered = 0
            for ref in settings_store.get("approver_chat_ids") or []:
                try:
                    userbot.send_message(ref, str(action["message"]).strip())
                    delivered += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("question for reviewer %s not delivered: %s", ref, exc)
            if not delivered:
                raise SkillError("вопрос не дошёл ни до одного проверяющего")
        elif kind == "relay_answer":
            question = s.get(ConversationEscalation, action["question_id"])
            if question is None:
                raise SkillError("вопрос сотрудника не найден")
            target = _employee_ref(s, question.chat_id)
            employee_chat = question.chat_id
            text = str(action["message"]).strip()
            sent = userbot.send_message(target, text)
            s.add(ConversationMessage(chat_id=employee_chat, message_id=sent["message_id"], role="assistant",
                                      status="agent_done", text=text[:4000], result={}))
            question.status = "resolved"
            _remember(s, f"Ответ проверяющего на вопрос «{question.reason[:120]}»: {text}", employee_id=employee_id,
                      kind="status", source="system", author=author)
            s.commit()
        elif kind in ("approve_meeting", "reject_meeting"):
            meeting = s.get(Meeting, action["meeting_id"])
            if meeting is None:
                raise SkillError("совещание не найдено")
            revision = int((meeting.options or {}).get("approval_revision", 1))
            meeting_id = meeting.id
            s.commit()
            if kind == "approve_meeting":
                if not pipeline.submit_deliver(meeting_id, allowed=("awaiting_approval",), revision=revision):
                    raise SkillError("совещание уже обработано")
            else:
                pipeline.reject(meeting_id, revision=revision)
        elif kind == "remind":
            people = reminders.recipients_for(s, action["to"], chat, sender)
            if not people:
                raise SkillError("некому напоминать: у адресатов нет контактов или это сам автор просьбы")
            due = max(reminders.parse_when(action.get("at")), now() + dt.timedelta(seconds=reminders.SEND_DELAY))
            reminder_id = reminders.create(s, chat=chat, requested_by=author, due=due, topic=str(action["topic"]),
                                           details=str(action.get("details") or ""), recipients=people)
            if action.get("meeting") is True and full:  # the alert from the panel that has not fired gives way to this
                meeting_alerts.claim(s, people, due, reminder_id)
            names = ", ".join(p["name"] or p["ref"] for p in people)
            _remember(s, f"Напоминание «{str(action['topic']).strip()[:120]}» для: {names}, на {reminders.local_text(due)}",
                      employee_id=employee_id, kind="commitment", source="system", author=author)
            s.commit()
            pipeline.poke()
        elif kind == "cancel_reminder":
            if not reminders.cancel(s, action["reminder_id"]):
                raise SkillError("напоминание уже отправлено или отменено")
            meeting_alerts.release(s, action["reminder_id"])
            s.commit()
        elif kind == "deliver_files":
            s.commit()
            try:
                case_id = cases.start_delivery(author or "руководителя", action["to"], action["document_ids"],
                                               bool(action.get("zip")), str(action.get("archive_name") or ""),
                                               str(action.get("note") or ""), requested_chat=chat)
            except cases.CaseError as exc:
                raise SkillError(f"deliver_files: {exc}") from exc
            if _testing():
                cases.advance(case_id)
            else:
                cases._pool().submit(cases.advance, case_id)
        elif kind in ("approve_case", "cancel_case"):
            s.commit()
            if kind == "cancel_case":
                cases.cancel(action["case_id"], author)
            elif not cases.approve(action["case_id"], author):
                cases.retry_step(action["case_id"], author)
        elif kind == "assign_task":
            task = _task(s, action["task_id"])
            target = action["to"]
            delivered = task.status in ("sent", "in_progress", "blocked", "awaiting_deadline")
            meeting_id = task.meeting_id
            if target == "assistant":
                task.executor, task.assignee_id, task.assignee_name = "assistant", None, "ассистент"
                task.status = "assistant_todo"
                _remember(s, f"Задача «{task.title}» поручена ассистенту", task_id=task.id, meeting_id=meeting_id,
                          kind="commitment", source="system", author=author)
                s.commit()
                assistant.schedule(meeting_id)
            else:
                person = s.get(Employee, target)
                if person is None:
                    raise SkillError("сотрудник не найден в справочнике")
                task.executor, task.assignee_id, task.assignee_name = "person", person.id, person.name
                ref = person.telegram_chat_id
                if delivered or task.status.startswith("assistant_"):
                    task.status = "sent"
                _remember(s, f"Задача «{task.title}» поручена: {person.name}", employee_id=person.id, task_id=task.id,
                          meeting_id=meeting_id, kind="commitment", source="system", author=author)
                s.commit()
                message = str(action.get("message") or "").strip()
                if message and ref:
                    userbot.send_message(ref, message)
        elif kind == "team_report":
            pass  # the PDF follows the reply (agent._process), or the approval case sends it (cases._run_skill)
        else:
            raise SkillError(f"неизвестный навык {kind!r}")


def describe(action: dict) -> str:
    """The action in plain words, for the director's approval and the Progress page."""
    kind = action.get("type")
    skill = SKILLS.get(kind)
    title = skill.title if skill else str(kind)
    with SessionLocal() as s:
        def name(employee_id):
            e = s.get(Employee, employee_id) if isinstance(employee_id, int) else None
            return e.name if e else f"#{employee_id}"

        def task(task_id):
            t = s.get(Task, task_id) if isinstance(task_id, int) else None
            return f"«{t.title}»" if t else f"#{task_id}"
        if kind == "set_deadline":
            return f"поставить срок {action.get('date')} задаче {task(action.get('task_id'))}"
        if kind == "task_status":
            label = {"in_progress": "в работе", "done": "выполнена", "blocked": "заблокирована"}.get(action.get("status"), "")
            note = str(action.get("note") or "").strip()
            return f"отметить задачу {task(action.get('task_id'))}: {label}" + (f" ({note[:150]})" if note else "")
        if kind == "note":
            return f"запомнить: {str(action.get('text') or '')[:200]}"
        if kind == "remind":
            to = action.get("to")
            who = {"me": "себе", "all": "всем сотрудникам", "reviewers": "проверяющим"}.get(to) if not isinstance(to, list) \
                else ", ".join(name(x) for x in to)
            when = action.get("at") or "сразу"
            return f"напомнить {who or to} «{str(action.get('topic') or '')[:150]}» — {when}"
        if kind == "cancel_reminder":
            return f"отменить напоминание #{action.get('reminder_id')}"
        if kind == "deliver_files":
            count = len(action.get("document_ids") or [])
            return (f"{'упаковать в ZIP и ' if action.get('zip') else ''}отправить {count} файл(ов) "
                    f"{name(action.get('to'))}")
        if kind == "team_report":
            scope = action.get("scope")
            return "прислать PDF-отчёт по команде" if scope == "all" else f"прислать PDF-отчёт по {name(scope)}"
    return title


def run(action: dict, chat: str, person: Person, author: str, sender: dict | None = None) -> dict:
    """Carries out a checked action in its mode: at once ({"done": True}) or as a case waiting for the director
    ({"case_id": ..., "mode": "approve" | "suggest"})."""
    mode = mode_for(action["type"], person)
    if mode != "auto":
        case_id = cases.start_skill(action, describe(action), mode, chat=chat, requested_by=author,
                                    employee_id=person.employee_id, sender=sender, full=person.full)
        return {"case_id": case_id, "mode": mode}
    execute(action, chat, person.employee_id, author, sender, full=person.full)
    return {"done": True}
