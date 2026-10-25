"""Cases: work the AI employee carries through to the end by itself.

A light task from a meeting («узнать у Вадима статус», «взять у Ани акт и отправить Пете») becomes a case: a short plan
made of whitelisted steps, run by code. The model may propose the plan, but only from the tools below and only with
the people of the task; code checks every step and falls back to a fixed template when the plan does not pass.

    send_message      write to a person from the task (ask a question, ask for a document)
    wait_reply        wait for that person's answer or document; polite reminders, then the director is told
    forward_document  pass the received document on, after checking its hash and the recipient
    report            tell the director (or the reviewers) how it ended, with the answer or the document
    close             finish the case

Safety rules: nobody outside the task's people is ever written to; the recipient's Telegram contact must be the same as
when the plan was made; the bytes sent are the bytes received (SHA-256); sensitive files and plans under the
«approve» / «suggest» autonomy wait for the director. A send interrupted half-way is never repeated automatically.
Every outgoing text is written by the model; when the model is down the step waits and is tried again.
"""
import collections
import datetime as dt
import hashlib
import io
import json
import logging
import re
import threading
import zipfile

from sqlalchemy.exc import IntegrityError

from . import config, llm, settings_store, task_routing, userbot, voice
from .db import (Artifact, Case, CaseEvent, CaseStep, CaseWait, Document, DocumentTransfer, Employee, Note, SessionLocal, Task,
                 now)

log = logging.getLogger("sysai.cases")

KINDS = ("ask", "request", "forward")
TOOLS = ("send_message", "wait_reply", "forward_document", "report", "close")
DELIVERY_TOOLS = ("zip_pack", "send_files")  # a delivery case is built by code from an order, never by the planner
MAX_DELIVERY_FILES = 50
MAX_ZIP_BYTES = 45 * 1024 * 1024
PURPOSES = ("ask", "request")
MAX_STEPS = 10
MAX_ATTEMPTS = 6               # model or Telegram failures of one step before the case asks for a human
RETRY_MINUTES = 5
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
OPEN = ("awaiting_approval", "suggested", "running", "waiting", "attention", "paused")
AUTONOMY = ("auto", "approve", "suggest")
STATUS_RU = {"awaiting_approval": "ждёт одобрения плана", "suggested": "предложено директору", "running": "выполняется",
             "waiting": "ждёт ответа", "attention": "нужно решение человека", "done": "завершено",
             "failed": "не удалось", "cancelled": "отменено", "paused": "на паузе",
             "handed_over": "передано человеку"}

_locks = collections.defaultdict(threading.Lock)


class CaseError(RuntimeError):
    """The case cannot go on without a person (Russian reason)."""


class Retry(RuntimeError):
    """A temporary problem (model down, Telegram busy): the step is tried again later."""


class PlanError(ValueError):
    pass


def _testing() -> bool:
    return config.TESTING


def _aware(moment):
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=dt.timezone.utc)


def _hours(key: str, default: float) -> float:
    try:
        return max(0.05, float(settings_store.get(key)))
    except (TypeError, ValueError):
        return default


def autonomy() -> str:
    value = settings_store.get("case_autonomy")
    return value if value in AUTONOMY else "auto"


def wants_case(task) -> bool:
    """Light tasks that need a conversation (an answer or a document) are carried as cases. They need the AI employee's
    own Telegram account: only it receives answers and files."""
    return (task.executor == "assistant" and (task.action_type or "") in KINDS and userbot.ai_mode())


def _event(s, case_id: int, kind: str, text: str, step_id=None, data=None):
    s.add(CaseEvent(case_id=case_id, step_id=step_id, kind=kind, text=str(text)[:1500], data=data))


def _leaders() -> list:
    """Who runs the cases day to day: the reviewers (full rights). The director only watches and is told only when
    there are no reviewers."""
    return list(settings_store.get("approver_chat_ids") or []) or list(settings_store.get("director_chat_ids") or [])


# ----------------------------------------------------------------------------- people and plans
def _people(s, task) -> list:
    """The task's counterparts as (role, Employee) pairs, in the order the router found them."""
    out, seen = [], set()
    for c in task.counterparts or []:
        if not isinstance(c, dict) or not c.get("employee_id") or c["employee_id"] in seen:
            continue
        employee = s.get(Employee, c["employee_id"])
        if employee is not None:
            out.append((c.get("role") or "recipient", employee))
            seen.add(employee.id)
    if task.target_employee_id and task.target_employee_id not in seen:
        employee = s.get(Employee, task.target_employee_id)
        if employee is not None:
            out.append(("target", employee))
    return out


def _roles(kind: str, people: list) -> tuple:
    """(who to contact first, who gets the document)."""
    sources = [e for r, e in people if r == "source"]
    others = [e for r, e in people if r != "source"]
    if kind == "ask":
        first = (others or sources or [None])[0]
        return first, []
    first = (sources or others or [None])[0]
    return first, [e for e in others if first is None or e.id != first.id]


def template_plan(kind: str, people: list) -> list:
    first, dests = _roles(kind, people)
    if first is None:
        raise PlanError("в поручении нет сотрудника из справочника, к которому нужно обратиться")
    if kind == "ask":
        return [{"tool": "send_message", "to": first.id, "purpose": "ask"},
                {"tool": "wait_reply", "from": first.id, "expects": "answer"},
                {"tool": "report"}, {"tool": "close"}]
    plan = [{"tool": "send_message", "to": first.id, "purpose": "request"},
            {"tool": "wait_reply", "from": first.id, "expects": "document"}]
    plan += [{"tool": "forward_document", "to": e.id, "from": first.id} for e in dests]
    return plan + [{"tool": "report"}, {"tool": "close"}]


def validate_plan(plan, kind: str, people: list) -> list:
    """Code's check of a proposed plan. Raises PlanError; returns clean steps."""
    if not isinstance(plan, list) or not 2 <= len(plan) <= MAX_STEPS:
        raise PlanError("план должен содержать от 2 до 10 шагов")
    allowed = {e.id for _, e in people}
    first, dests = _roles(kind, people)
    clean, asked, waited = [], set(), {}
    for raw in plan:
        if not isinstance(raw, dict) or raw.get("tool") not in TOOLS:
            raise PlanError(f"неизвестный шаг {raw!r:.80}")
        tool = raw["tool"]
        step = {"tool": tool}
        if tool == "send_message":
            if raw.get("to") not in allowed:
                raise PlanError("сообщение адресовано человеку не из поручения")
            if first is None or raw["to"] != first.id:
                # Only the person who has the answer or the document is asked; recipients only receive the file.
                raise PlanError("просить можно только того, у кого ответ или документ")
            if raw.get("purpose") not in PURPOSES:
                raise PlanError("purpose: ask или request")
            step.update(to=raw["to"], purpose=raw["purpose"])
            asked.add(raw["to"])
        elif tool == "wait_reply":
            if raw.get("from") not in asked:
                raise PlanError("ждать ответа можно только от того, кому уже написали")
            if raw.get("expects") not in ("answer", "document"):
                raise PlanError("expects: answer или document")
            step.update({"from": raw["from"], "expects": raw["expects"]})
            waited[raw["from"]] = raw["expects"]
        elif tool == "forward_document":
            if raw.get("to") not in allowed or waited.get(raw.get("from")) != "document":
                raise PlanError("переслать можно только полученный документ и только человеку из поручения")
            if raw["to"] == raw["from"]:
                raise PlanError("документ нельзя пересылать его отправителю")
            step.update(to=raw["to"], **{"from": raw["from"]})
        clean.append(step)
    if clean[-1]["tool"] != "close" or sum(s["tool"] == "close" for s in clean) != 1:
        raise PlanError("план заканчивается одним шагом close")
    if sum(s["tool"] == "report" for s in clean) != 1 or clean[-2]["tool"] != "report":
        raise PlanError("перед close должен быть один шаг report: руководитель узнаёт результат")
    if sum(s["tool"] == "send_message" for s in clean) != 1 or sum(s["tool"] == "wait_reply" for s in clean) != 1:
        raise PlanError("одна просьба и одно ожидание: повторы делают напоминания, а не новые сообщения")
    # Coverage: the plan must do what the task says, not something smaller.
    if kind == "ask" and (first is None or waited.get(first.id) != "answer"):
        raise PlanError("план не получает ответ от того, кого нужно спросить")
    if kind in ("request", "forward"):
        if first is None or waited.get(first.id) != "document":
            raise PlanError("план не получает документ у источника")
        forwarded = {s["to"] for s in clean if s["tool"] == "forward_document"}
        if {e.id for e in dests} - forwarded:
            raise PlanError("план не передаёт документ всем получателям")
    return clean


def _model_plan(task, kind: str, people: list):
    if not config.llm_enabled() and not _testing():
        return None
    facts = {"task": {"title": task.title, "description": task.description or "", "quote": task.source_quote or ""},
             "action_type": kind, "plan_hint": task_routing.describe(kind, task.counterparts or []),
             "people": [{"id": e.id, "name": e.name, "role": r} for r, e in people]}
    system = voice.system_prompt(
        "Составь план дела из шагов белого списка, ничего другого не добавляй. Шаги: "
        '{"tool":"send_message","to":ID,"purpose":"ask|request"}, {"tool":"wait_reply","from":ID,"expects":"answer|document"}, '
        '{"tool":"forward_document","from":ID,"to":ID}, {"tool":"report"}, {"tool":"close"}. '
        "ID бери только из people. ask: спросить и дождаться ответа; request: попросить документ и дождаться его; "
        "forward: попросить документ у source и передать каждому recipient. Получателей (recipient) ни о чём не "
        "проси — они только получают документ. Предпоследний шаг report, последний close. "
        'JSON: {"steps":[...]}')
    try:
        raw = llm.chat(voice.model(), [{"role": "system", "content": system},
                                       {"role": "user", "content": "<data>" + json.dumps(facts, ensure_ascii=False) + "</data>"}],
                       json_mode=True, temperature=0.1, max_tokens=800, timeout=60)
        data = llm.parse_json(raw)
    except Exception as exc:  # noqa: BLE001
        log.info("planner unavailable, template plan is used: %s", exc)
        return None
    return data.get("steps") if isinstance(data, dict) else None


def make_plan(task, kind: str, people: list) -> tuple:
    """(steps, planner): the model's plan when it passes the checks, otherwise the template."""
    proposed = _model_plan(task, kind, people)
    if proposed is not None:
        try:
            return validate_plan(proposed, kind, people), "model"
        except PlanError as exc:
            log.info("model plan for task %s rejected: %s", task.id, exc)
    return validate_plan(template_plan(kind, people), kind, people), "template"


def describe_plan(s, case) -> str:
    names = {}
    lines = []
    for step in case.steps:
        a = step.args or {}
        for key in ("to", "from"):
            if a.get(key) and a[key] not in names:
                e = s.get(Employee, a[key])
                names[a[key]] = e.name if e else f"#{a[key]}"
        if step.tool == "send_message":
            lines.append(("спросить " if a.get("purpose") == "ask" else "попросить документ у ") + names[a["to"]])
        elif step.tool == "wait_reply":
            lines.append(f"дождаться {'ответа' if a.get('expects') == 'answer' else 'документа'} от {names[a['from']]}")
        elif step.tool == "forward_document":
            lines.append(f"передать документ {names[a['to']]}")
        elif step.tool == "zip_pack":
            lines.append(f"упаковать {len(a.get('document_ids') or [])} файл(ов) в ZIP «{a.get('archive_name', '')}»")
        elif step.tool == "send_files":
            lines.append(f"отправить {'архив' if a.get('from_step') is not None else 'файлы'} {names[a['to']]}")
        elif step.tool == "report":
            lines.append("доложить результат")
    return "; ".join(lines)


# ----------------------------------------------------------------------------- creating a case
def start_for_task(task_id: int):
    """Create (once) and start the case of an assistant task. Returns the case id or None."""
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        if task is None:
            return None
        existing = s.query(Case).filter_by(task_id=task_id).first()
        if existing is not None:
            case_id = existing.id
        else:
            kind = task.action_type if task.action_type in KINDS else "ask"
            people = _people(s, task)
            case = Case(task_id=task.id, meeting_id=task.meeting_id, title=task.title[:500], kind=kind,
                        autonomy=autonomy())
            try:
                steps, planner = make_plan(task, kind, people)
            except PlanError as exc:
                case.status, case.note, case.closed_at = "failed", str(exc), now()
                task.status = "assistant_failed"
                s.add(case)
                s.flush()
                _event(s, case.id, "failed", f"План не составлен: {exc}")
                s.add(Note(task_id=task.id, meeting_id=task.meeting_id, kind="status", source="system",
                           author="ассистент", text=f"Не удалось начать дело «{task.title}»: {exc}"[:800]))
                s.commit()
                _tell_leaders(case.id, "failure", {"reason": str(exc)})
                return case.id
            case.planner = planner
            refs = {e.id: e.telegram_chat_id or "" for _, e in people}
            for position, step in enumerate(steps):
                args = {k: v for k, v in step.items() if k != "tool"}
                if "to" in args:
                    args["to_ref"] = refs.get(args["to"], "")  # the address the plan was checked against
                case.steps.append(CaseStep(position=position, tool=step["tool"], args=args))
            case.status = {"auto": "running", "approve": "awaiting_approval", "suggest": "suggested"}[case.autonomy]
            task.status = "assistant_waiting"
            s.add(case)
            s.flush()
            _event(s, case.id, "planned", f"План ({'модель' if planner == 'model' else 'шаблон'}): {describe_plan(s, case)}")
            s.commit()
            case_id = case.id
            if case.autonomy != "auto":
                _tell_leaders(case_id, "approval", {"plan": describe_plan(s, case), "autonomy": case.autonomy})
                return case_id
    advance(case_id)
    return case_id


# ----------------------------------------------------------------------------- the model writes every text
def _write(purpose: str, facts: dict) -> str:
    instructions = {
        "ask": "Напиши сотруднику короткое сообщение: тебе нужно узнать у него то, что сказано в task. Задай вопрос прямо "
               "и попроси ответить в этом чате. Не пиши так, будто задача поручена ему.",
        "request": "Напиши сотруднику короткое сообщение: попроси прислать в этот чат документ или данные из task. Если "
                   "есть forward_to, скажи, что передашь документ им. Не пиши так, будто задача поручена ему.",
        "remind": "Вежливо и коротко напомни сотруднику о своей просьбе (what_was_asked). reminder_number — какое это "
                  "напоминание по счёту; не дави и не упрекай.",
        "deliver": "Напиши короткую подпись к документу для получателя: от кого документ (from) и по какому поручению (task).",
        "report": "Коротко сообщи руководителю, чем закончилось дело: что спросил или попросил, у кого, что получил "
                  "(answers, documents), кому передал (forwarded, sent). Если ответ — передай его суть точно. Если в ответе "
                  "человек просит что-то прислать или сделать, предложи следующий шаг, который ты сделаешь сам: например, "
                  "«пришлите сюда файлы — упакую в ZIP и передам ему».",
        "escalate": "Сообщи руководителю, что сотрудник (person) не отвечает по делу уже hours часов, несмотря на "
                    "напоминания (reminders). Предложи вмешаться. Без упрёков в адрес сотрудника.",
        "approval": "Сообщи руководителю план дела (plan) и попроси разрешения начать: он может ответить «да» в этом "
                    "чате или нажать «Одобрить» на странице совещания. Если autonomy=suggest, это только предложение.",
        "doc_approval": "Попроси руководителя разрешить пересылку документа: файл (filename) от from для to. Объясни "
                        "причину (reason). Он может ответить «да» в этом чате или одобрить в панели.",
        "attention": "Сообщи руководителю, что дело остановилось и нужно его решение: reason.",
        "failure": "Сообщи руководителю, что дело не удалось начать или выполнить: reason. Предложи, что можно сделать.",
        "handover": "Напиши сотруднику (person), что руководитель передаёт ему поручение (task), которое вёл ты. Коротко "
                    "перечисли, что уже сделано (done_so_far) и что получено (documents), и что осталось сделать.",
    }[purpose]
    system = voice.system_prompt("Ты ведёшь дело по поручению с совещания. " + instructions + ' JSON: {"text":"..."}')
    facts = {"purpose": purpose, **facts}
    if purpose in ("ask", "request", "remind", "deliver", "handover"):
        from . import memory
        found = memory.skills_for(f"{purpose} {json.dumps(facts.get('task') or {}, ensure_ascii=False)}")
        if found:
            facts["skills"] = found  # the company's scenarios and templates for such messages
            system += " Если в skills есть подходящий навык или шаблон, следуй ему."
    try:
        raw = llm.chat(voice.model(), [{"role": "system", "content": system},
                                       {"role": "user", "content": "<data>" + json.dumps(facts, ensure_ascii=False, default=str) + "</data>"}],
                       json_mode=True, temperature=0.4, max_tokens=700, timeout=90)
        data = llm.parse_json(raw)
    except llm.LLMError as exc:
        raise Retry(f"модель недоступна: {str(exc)[:200]}") from exc
    text = str((data or {}).get("text") or "").strip() if isinstance(data, dict) else ""
    if not text or len(text) > 3000:
        raise Retry("модель не написала текст сообщения")
    stiff = voice.officialese(text)
    if stiff:  # one rewrite without office-letter phrases; if it fails, the first text is good enough
        try:
            raw = llm.chat(voice.model(), [{"role": "system", "content": system},
                                           {"role": "user", "content": "<data>" + json.dumps(facts, ensure_ascii=False, default=str) +
                                            f"</data>\nПрошлый вариант: «{text}». Перепиши проще, как коллега в мессенджере, "
                                            f"без канцелярита ({', '.join(stiff)}); смысл сохрани."}],
                           json_mode=True, temperature=0.4, max_tokens=700, timeout=90)
            better = str((llm.parse_json(raw) or {}).get("text") or "").strip()
            if better and len(better) <= 3000:
                text = better
        except llm.LLMError:
            pass
    return text


def _send_text(ref: str, text: str) -> dict:
    sent = userbot.send_message(ref, text)
    if not isinstance(sent, dict) or not sent.get("message_id"):
        raise CaseError("Telegram не подтвердил отправку")
    try:
        from . import agent
        agent.record_outgoing(ref, text, sent.get("message_id"), sent.get("user_id"))
    except Exception:  # noqa: BLE001
        log.exception("sent message is not in the conversation history")
    return sent


def _tell_leaders(case_id: int, purpose: str, extra: dict, document=None) -> int:
    """A text for the director (or the reviewers) about the case; failures are logged, never raised."""
    leaders = _leaders()
    if not leaders or not userbot.ai_mode():
        return 0
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        facts = {"case": {"id": case.id, "title": case.title, "kind": case.kind}, **extra}
    try:
        text = _write(purpose, facts)
    except Retry as exc:
        log.warning("case %s: %s message not written: %s", case_id, purpose, exc)
        return 0
    delivered = 0
    for ref in leaders:
        try:
            _send_text(ref, text)
            if document is not None:
                userbot.send_document(ref, document["filename"], document["data"], "")
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("case %s: leader %s not told: %s", case_id, ref, exc)
    with SessionLocal() as s:
        _event(s, case_id, "told_leader", f"{purpose}: {text}")
        s.commit()
    return delivered


# ----------------------------------------------------------------------------- running
def advance(case_id: int, moment=None) -> None:
    """Run the case's steps until it has to wait (an answer, an approval, the quiet hours) or ends."""
    with _locks[case_id]:
        for _ in range(MAX_STEPS * 2):
            with SessionLocal() as s:
                case = s.get(Case, case_id)
                if case is None or case.status != "running":
                    return
                if case.retry_at is not None and _aware(case.retry_at) > (moment or now()):
                    return
                step = next((x for x in case.steps if x.status not in ("done", "skipped")), None)
                if step is None:
                    _close(s, case)
                    return
                if step.status != "pending":
                    return
                step_id, tool = step.id, step.tool
            try:
                go_on = _RUN[tool](case_id, step_id)
            except Retry as exc:
                _retry(case_id, step_id, str(exc))
                return
            except CaseError as exc:
                _attention(case_id, step_id, str(exc))
                return
            if not go_on:
                return


def _retry(case_id: int, step_id: int, reason: str) -> None:
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        case.attempts += 1
        if case.attempts >= MAX_ATTEMPTS:
            s.commit()
            _attention(case_id, step_id, f"шаг не удаётся выполнить: {reason}")
            return
        case.retry_at = now() + dt.timedelta(minutes=RETRY_MINUTES * case.attempts)
        _event(s, case_id, "retry", f"Повтор позже: {reason}", step_id)
        s.commit()


def _attention(case_id: int, step_id, reason: str) -> None:
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        case.status, case.note = "attention", reason[:1000]
        step = s.get(CaseStep, step_id) if step_id else None
        if step is not None and step.status == "pending":
            step.status, step.error = "failed", reason[:1000]
        _event(s, case_id, "attention", reason, step_id)
        s.commit()
    _tell_leaders(case_id, "attention", {"reason": reason})


def _quiet(case_id: int) -> bool:
    until = userbot.quiet_until()
    if until is None:
        return False
    with SessionLocal() as s:
        s.get(Case, case_id).retry_at = until
        s.commit()
    return True


def _checked_contact(s, employee_id: int, planned_ref: str) -> Employee:
    """The person must still be in the directory, active, with the very contact the plan was checked against."""
    employee = s.get(Employee, employee_id)
    if employee is None or not employee.active:
        raise CaseError("сотрудника больше нет в справочнике")
    if not employee.telegram_chat_id:
        raise CaseError(f"у {employee.name} нет Telegram-контакта")
    if planned_ref and employee.telegram_chat_id != planned_ref:
        raise CaseError(f"Telegram-контакт {employee.name} изменился после составления плана; проверьте адрес")
    return employee


def _case_facts(s, case) -> dict:
    task = case.task
    facts = {"task": {"title": case.title, "description": (task.description if task else "") or "",
                      "quote": (task.source_quote if task else "") or ""},
             "plan": describe_plan(s, case)}
    return facts


def _history_with(s, person) -> str:
    from .db import PersonMemory
    rows = s.query(PersonMemory).filter_by(employee_id=person.id).all()
    return " ".join(r.summary for r in rows if r.summary)[:1200]


def _send_message(case_id: int, step_id: int) -> bool:
    if _quiet(case_id):
        return False
    with SessionLocal() as s:
        case, step = s.get(Case, case_id), s.get(CaseStep, step_id)
        person = _checked_contact(s, step.args["to"], step.args.get("to_ref", ""))
        facts = _case_facts(s, case)
        facts["person"] = {"name": person.name, "position": person.position or None,
                           "communication_style": getattr(person, "communication_style", "") or None}
        history = _history_with(s, person)
        if history:
            facts["history"] = history  # a short summary of earlier conversations with this person
        if step.args.get("purpose") == "request":
            dests = [s.get(Employee, x.args["to"]) for x in case.steps if x.tool == "forward_document"]
            facts["forward_to"] = [e.name for e in dests if e is not None]
        ref, name = person.telegram_chat_id, person.name
    text = _write(step.args.get("purpose", "ask"), facts)
    with SessionLocal() as s:  # persisted before the side effect: a crash now never sends twice
        step = s.get(CaseStep, step_id)
        step.status, step.started_at = "sending", now()
        s.commit()
    try:
        sent = _send_text(ref, text)
    except userbot.QuietHours:
        with SessionLocal() as s:
            s.get(CaseStep, step_id).status = "pending"
            s.commit()
        _quiet(case_id)
        return False
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as s:
            step = s.get(CaseStep, step_id)
            step.status, step.error = "uncertain", str(exc)[:500]
            s.commit()
        raise CaseError(f"не удалось подтвердить доставку сообщения {name}: {str(exc)[:200]}. Проверьте чат перед повтором")
    with SessionLocal() as s:
        step = s.get(CaseStep, step_id)
        step.status, step.finished_at = "done", now()
        step.result = {"message_id": sent.get("message_id"), "user_id": sent.get("user_id"), "text": text}
        case = s.get(Case, case_id)
        case.attempts, case.retry_at = 0, None
        _event(s, case_id, "message_sent", f"Написал {name}: {text}", step_id)
        s.commit()
    return True


def _wait_reply(case_id: int, step_id: int) -> bool:
    with SessionLocal() as s:
        case, step = s.get(Case, case_id), s.get(CaseStep, step_id)
        person = s.get(Employee, step.args["from"])
        asked = next((x for x in reversed(case.steps) if x.position < step.position and x.tool == "send_message"
                      and x.args.get("to") == step.args["from"] and x.status == "done"), None)
        start = now()
        reminder = dt.timedelta(hours=_hours("case_reminder_hours", 4))
        wait = s.query(CaseWait).filter_by(step_id=step_id).first() or CaseWait(case_id=case_id, step_id=step_id)
        wait.employee_id = person.id if person else None
        wait.chat_ref = person.telegram_chat_id if person else ""
        wait.expects = step.args.get("expects", "answer")
        wait.asked_message_id = (asked.result or {}).get("message_id") if asked else None
        wait.started_at, wait.next_reminder_at = start, start + reminder
        wait.escalate_at = start + dt.timedelta(hours=_hours("case_escalate_hours", 24))
        s.add(wait)
        step.status, step.started_at = "waiting", start
        case.status = "waiting"
        _event(s, case_id, "waiting", f"Жду {'ответа' if wait.expects == 'answer' else 'документа'} от "
                                      f"{person.name if person else '?'}", step_id)
        s.commit()
    return False


def _sha(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def _forward_document(case_id: int, step_id: int) -> bool:
    if _quiet(case_id):
        return False
    with SessionLocal() as s:
        case, step = s.get(Case, case_id), s.get(CaseStep, step_id)
        wait = (s.query(CaseWait).join(CaseStep, CaseWait.step_id == CaseStep.id)
                .filter(CaseWait.case_id == case_id, CaseWait.employee_id == step.args["from"],
                        CaseWait.expects == "document", CaseWait.status == "resolved").order_by(CaseWait.id.desc()).first())
        document = s.get(Document, wait.document_id) if wait and wait.document_id else None
        if document is None or document.data is None:
            raise CaseError("документ для пересылки не найден")
        # 1. the document is the one received in this case from the planned person
        if document.case_id != case_id or document.employee_id != step.args["from"]:
            raise CaseError("документ пришёл не от того сотрудника или не по этому делу; пересылка остановлена")
        recorded = (s.get(CaseStep, wait.step_id).result or {}).get("sha256")
        actual = _sha(bytes(document.data))
        if actual != document.sha256 or (recorded and recorded != actual):
            raise CaseError("содержимое документа изменилось после получения (хеш не совпал); пересылка остановлена")
        # 2. the recipient is the planned one, from the task, still with the planned contact, not the sender
        task_people = {c.get("employee_id"): c.get("role") for c in ((case.task.counterparts if case.task else None) or [])
                       if isinstance(c, dict)}
        if case.task is not None and case.task.target_employee_id:
            task_people.setdefault(case.task.target_employee_id, "target")
        if step.args["to"] not in task_people or step.args["to"] == document.employee_id:
            raise CaseError("получатель не указан в поручении; пересылка остановлена")
        recipient = _checked_contact(s, step.args["to"], step.args.get("to_ref", ""))
        sender = s.get(Employee, document.employee_id)
        transfer = (s.query(DocumentTransfer).filter_by(step_id=step_id).order_by(DocumentTransfer.id.desc()).first())
        if transfer is None:
            transfer = DocumentTransfer(document_id=document.id, case_id=case_id, step_id=step_id,
                                        to_employee_id=recipient.id, to_ref=recipient.telegram_chat_id, sha256=actual)
            if document.sensitive:
                transfer.status, transfer.reason = "awaiting_approval", "документ похож на конфиденциальный"
            s.add(transfer)
            s.flush()
        if transfer.status == "awaiting_approval":
            step.status = "awaiting_approval"
            case.status, case.note = "attention", f"Пересылка «{document.filename}» ждёт разрешения руководителя"
            _event(s, case_id, "document_approval", case.note, step_id)
            s.commit()
            _tell_leaders(case_id, "doc_approval", {"filename": document.filename, "from": sender.name if sender else "",
                                                    "to": recipient.name, "reason": transfer.reason})
            return False
        if transfer.status not in ("approved",):
            raise CaseError(f"пересылка в состоянии «{transfer.status}»; нужен человек")
        if transfer.to_ref != recipient.telegram_chat_id or transfer.sha256 != actual:
            raise CaseError("адресат или содержимое изменились после разрешения; пересылка остановлена")
        facts = _case_facts(s, case)
        facts.update({"from": sender.name if sender else "", "to": recipient.name, "filename": document.filename})
        ref, name, filename, data, transfer_id = (recipient.telegram_chat_id, recipient.name, document.filename,
                                                  bytes(document.data), transfer.id)
        s.commit()
    caption = _write("deliver", facts)
    with SessionLocal() as s:
        s.get(CaseStep, step_id).status = "sending"
        s.get(DocumentTransfer, transfer_id).status = "sending"
        s.commit()
    try:
        sent = userbot.send_document(ref, filename, data, caption[:1000])
        if not isinstance(sent, dict) or not sent.get("message_id"):
            raise CaseError("Telegram не подтвердил отправку файла")
    except userbot.QuietHours:
        with SessionLocal() as s:
            s.get(CaseStep, step_id).status = "pending"
            s.get(DocumentTransfer, transfer_id).status = "approved"
            s.commit()
        _quiet(case_id)
        return False
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as s:
            s.get(CaseStep, step_id).status = "uncertain"
            transfer = s.get(DocumentTransfer, transfer_id)
            transfer.status, transfer.reason = "uncertain", str(exc)[:500]
            s.commit()
        raise CaseError(f"не удалось подтвердить пересылку файла {name}: {str(exc)[:200]}. Проверьте чат перед повтором")
    try:
        from . import agent
        agent.record_outgoing(ref, f"[файл «{filename}»] {caption}", sent.get("message_id"), sent.get("user_id"))
    except Exception:  # noqa: BLE001
        log.exception("forwarded file is not in the conversation history")
    with SessionLocal() as s:
        step = s.get(CaseStep, step_id)
        step.status, step.finished_at = "done", now()
        step.result = {"message_id": sent.get("message_id"), "document_id": document.id, "sha256": actual, "to": name}
        transfer = s.get(DocumentTransfer, transfer_id)
        transfer.status, transfer.message_id, transfer.sent_at = "sent", sent.get("message_id"), now()
        case = s.get(Case, case_id)
        case.attempts, case.retry_at = 0, None
        _event(s, case_id, "document_forwarded", f"Передал «{filename}» {name} (SHA-256 {actual[:12]}…)", step_id)
        s.commit()
    return True


def _report(case_id: int, step_id: int) -> bool:
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        answers, documents, forwarded, sent = [], [], [], []
        for step in case.steps:
            if step.tool == "wait_reply" and step.status == "done":
                who = s.get(Employee, step.args["from"])
                result = step.result or {}
                if result.get("answer"):
                    answers.append({"from": who.name if who else "", "text": result["answer"]})
                if result.get("document_id"):
                    doc = s.get(Document, result["document_id"])
                    documents.append({"from": who.name if who else "", "filename": doc.filename if doc else "",
                                      "id": result["document_id"]})
            if step.tool == "forward_document" and step.status == "done":
                forwarded.append((step.result or {}).get("to"))
            if step.tool == "send_files" and step.status == "done":
                res = step.result or {}
                forwarded.append(res.get("to"))
                sent.append({"to": res.get("to"), "files": res.get("files"), "archive": res.get("archive")})
        attach = None
        if documents and not forwarded:  # «запросить у Ани акт»: the document goes to whoever asked for it
            doc = s.get(Document, documents[0]["id"])
            attach = {"filename": doc.filename, "data": bytes(doc.data)} if doc and doc.data else None
        facts = {**_case_facts(s, case), "answers": answers, "documents": documents, "forwarded": forwarded,
                 "sent": sent}
    _tell_leaders(case_id, "report", facts, document=attach)
    with SessionLocal() as s:
        step = s.get(CaseStep, step_id)
        step.status, step.finished_at, step.result = "done", now(), {"answers": len(answers), "documents": len(documents)}
        s.commit()
    return True


def _close_step(case_id: int, step_id: int) -> bool:
    with SessionLocal() as s:
        step = s.get(CaseStep, step_id)
        step.status, step.finished_at = "done", now()
        s.commit()
    return True


# ----------------------------------------------------------------------------- delivery: «запакуй и отправь ему»
def _safe_name(name: str, fallback: str = "файл") -> str:
    name = re.sub(r"[\\/\x00-\x1f]+", "_", str(name or "")).strip(" .") or fallback
    return name[:150]


def _unique(name: str, taken: set) -> str:
    """фото.jpg, фото (2).jpg, фото (3).jpg: files with the same name must not overwrite each other in the archive."""
    if name.lower() not in taken:
        taken.add(name.lower())
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}){'.' + ext if ext else ''}"
        if candidate.lower() not in taken:
            taken.add(candidate.lower())
            return candidate
        n += 1


def _checked_documents(s, ids) -> list:
    docs = []
    for doc_id in ids:
        doc = s.get(Document, doc_id)
        if doc is None or doc.data is None:
            raise CaseError(f"файл #{doc_id} не найден в библиотеке документов")
        if _sha(bytes(doc.data)) != doc.sha256:
            raise CaseError(f"содержимое «{doc.filename}» изменилось после получения (хеш не совпал)")
        docs.append(doc)
    return docs


def start_delivery(requested_by: str, to_employee_id: int, document_ids: list, as_zip: bool,
                   archive_name: str = "", note: str = "", requested_chat: str = "") -> int:
    """An order in the chat («перешли ему», «запакуй и отправь зипом»): a case of code-built steps
    zip_pack → send_files → report → close. It ends only when Telegram confirmed the sending."""
    ids = list(dict.fromkeys(int(x) for x in document_ids))
    if not ids or len(ids) > MAX_DELIVERY_FILES:
        raise CaseError(f"нужно от 1 до {MAX_DELIVERY_FILES} файлов")
    with SessionLocal() as s:
        recipient = s.get(Employee, to_employee_id)
        if recipient is None or not recipient.active or not recipient.telegram_chat_id:
            raise CaseError("у получателя нет Telegram-контакта в справочнике")
        docs = _checked_documents(s, ids)
        as_zip = bool(as_zip) or len(docs) > 10
        name = _safe_name(archive_name or f"Файлы для {recipient.name}") if as_zip else ""
        if as_zip and not name.lower().endswith(".zip"):
            name += ".zip"
        title = (note or "").strip() or (f"Упаковать и отправить {len(docs)} файл(ов) {recipient.name}" if as_zip
                                         else f"Отправить {len(docs)} файл(ов) {recipient.name}")
        case = Case(title=title[:500], kind="deliver", autonomy="auto", planner="order", status="running")
        steps = []
        if as_zip:
            steps.append(CaseStep(position=0, tool="zip_pack", args={"document_ids": ids, "archive_name": name,
                                                                     "requested_by": requested_by[:120],
                                                                     "requested_chat": str(requested_chat)}))
        steps.append(CaseStep(position=len(steps), tool="send_files",
                              args={"to": recipient.id, "to_ref": recipient.telegram_chat_id,
                                    "requested_by": requested_by[:120], "requested_chat": str(requested_chat),
                                    **({"from_step": 0} if as_zip else {"document_ids": ids})}))
        steps += [CaseStep(position=len(steps), tool="report", args={}),
                  CaseStep(position=len(steps) + 1, tool="close", args={})]
        case.steps.extend(steps)
        s.add(case)
        s.flush()
        for doc in docs:
            if doc.case_id is None:
                doc.case_id = case.id
        _event(s, case.id, "planned", f"Поручение от {requested_by or 'руководителя'}: {describe_plan(s, case)}")
        s.commit()
        return case.id


def _zip_pack(case_id: int, step_id: int) -> bool:
    with SessionLocal() as s:
        case, step = s.get(Case, case_id), s.get(CaseStep, step_id)
        docs = _checked_documents(s, step.args.get("document_ids") or [])
        buf, taken = io.BytesIO(), set()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
            names = [_unique(_safe_name(d.filename), taken) for d in docs]
            for name, doc in zip(names, docs):
                archive.writestr(name, bytes(doc.data))
        data = buf.getvalue()
        if len(data) > MAX_ZIP_BYTES:
            raise CaseError(f"архив получился {len(data) // (1024 * 1024)} МБ — больше лимита {MAX_ZIP_BYTES // (1024 * 1024)} МБ")
        with zipfile.ZipFile(io.BytesIO(data)) as check:  # the archive opens and holds exactly the ordered files
            if check.testzip() is not None or sorted(check.namelist()) != sorted(names):
                raise CaseError("архив собран с ошибкой, отправка остановлена")
        filename = step.args.get("archive_name") or "Файлы.zip"
        art = Artifact(title=case.title[:300], filename=filename[:200], mime="application/zip", data=data)
        s.add(art)
        s.flush()
        step.status, step.finished_at = "done", now()
        step.result = {"artifact_id": art.id, "filename": filename, "sha256": _sha(data), "files": names, "size": len(data)}
        _event(s, case_id, "zip_packed", f"Упаковал {len(names)} файл(ов) в «{filename}» ({len(data) // 1024} КБ)", step_id)
        s.commit()
    return True


def _send_files(case_id: int, step_id: int) -> bool:
    if _quiet(case_id):
        return False
    with SessionLocal() as s:
        case, step = s.get(Case, case_id), s.get(CaseStep, step_id)
        recipient = _checked_contact(s, step.args["to"], step.args.get("to_ref", ""))
        archive = None
        if step.args.get("from_step") is not None:
            packed = next((x for x in case.steps if x.position == step.args["from_step"]), None)
            res = (packed.result or {}) if packed is not None else {}
            art = s.get(Artifact, res.get("artifact_id")) if res.get("artifact_id") else None
            if art is None or art.data is None or _sha(bytes(art.data)) != res.get("sha256"):
                raise CaseError("архив для отправки не найден или изменился")
            payload = [(art.filename, bytes(art.data))]
            archive, listed = art.filename, res.get("files") or []
        else:
            docs = _checked_documents(s, step.args.get("document_ids") or [])
            payload = [(d.filename, bytes(d.data)) for d in docs]
            listed = [d.filename for d in docs]
        facts = _case_facts(s, case)
        facts.update({"from": step.args.get("requested_by") or "руководителя", "to": recipient.name,
                      "filename": archive or ", ".join(listed[:5]), "files_count": len(listed)})
        ref, name = recipient.telegram_chat_id, recipient.name
    caption = _write("deliver", facts)
    with SessionLocal() as s:
        s.get(CaseStep, step_id).status = "sending"
        s.commit()
    message_ids = []
    try:
        for i, (filename, data) in enumerate(payload):
            sent = userbot.send_document(ref, filename, data, caption[:1000] if i == 0 else "")
            if not isinstance(sent, dict) or not sent.get("message_id"):
                raise CaseError("Telegram не подтвердил отправку файла")
            message_ids.append(sent["message_id"])
    except userbot.QuietHours:
        if not message_ids:
            with SessionLocal() as s:
                s.get(CaseStep, step_id).status = "pending"
                s.commit()
            _quiet(case_id)
            return False
        raise CaseError("тихие часы начались посреди отправки; часть файлов ушла — проверьте чат") from None
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as s:
            step = s.get(CaseStep, step_id)
            step.status, step.result = "uncertain", {"message_ids": message_ids, "to": name}
            s.commit()
        raise CaseError(f"не удалось подтвердить отправку {name}: {str(exc)[:200]}. Проверьте чат перед повтором") from exc
    try:
        from . import agent
        what = f"архив «{archive}»" if archive else ", ".join(f"«{n}»" for n in listed[:5])
        agent.record_outgoing(ref, f"[файл {what}] {caption}", message_ids[0], None)
    except Exception:  # noqa: BLE001
        log.exception("sent files are not in the conversation history")
    with SessionLocal() as s:
        step = s.get(CaseStep, step_id)
        step.status, step.finished_at = "done", now()
        step.result = {"message_ids": message_ids, "to": name, "files": listed, "archive": archive}
        case = s.get(Case, case_id)
        case.attempts, case.retry_at = 0, None
        what = f"архив «{archive}» ({len(listed)} файл(ов))" if archive else f"{len(listed)} файл(ов)"
        _event(s, case_id, "files_sent", f"Отправил {name} {what}", step_id)
        s.commit()
    return True


def _local_time(moment) -> str:
    from zoneinfo import ZoneInfo
    if moment is None:
        return ""
    local = _aware(moment).astimezone(ZoneInfo(config.TIMEZONE))
    today = now().astimezone(ZoneInfo(config.TIMEZONE)).date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%d.%m %H:%M")


EVENT_SKIP = ("retry", "told_leader")  # internal noise, not what a person wants to hear about


def _case_story(s, case, events: int = 5) -> dict:
    """A case in plain words: what it is about, what happened lately (with times), who it is waiting for."""
    waits = s.query(CaseWait).filter_by(case_id=case.id, status="open").all()
    waiting_for = []
    for w in waits:
        e = s.get(Employee, w.employee_id) if w.employee_id else None
        waiting_for.append({"person": e.name if e else w.chat_ref, "expects": "ответ" if w.expects == "answer" else "файл",
                            "since": _local_time(w.started_at)})
    story = [{"at": _local_time(ev.created_at), "text": ev.text[:300]}
             for ev in case.events if ev.kind not in EVENT_SKIP][-events:]
    return {"id": case.id, "title": case.title, "status": STATUS_RU.get(case.status, case.status),
            "open": case.status in OPEN, "waiting_for": waiting_for, "last_events": story,
            "requested_by": next((x.args.get("requested_by") for x in case.steps if (x.args or {}).get("requested_by")), None)}


def person_view(employee_id=None, chat: str = "", hours: int = 72, limit: int = 5) -> list:
    """Cases this person takes part in (asked by the assistant, receiving files) or ordered, open first and then the
    latest. The conversation answers «ну что там?» from this instead of guessing."""
    if not employee_id and not chat:
        return []
    since = now() - dt.timedelta(hours=hours)
    with SessionLocal() as s:
        found = []
        for case in (s.query(Case).filter((Case.updated_at >= since) | Case.status.in_(OPEN))
                     .order_by(Case.id.desc()).limit(60)):
            role = None
            for step in case.steps:
                a = step.args or {}
                if employee_id and a.get("from") == employee_id:
                    role = role or "у тебя что-то запросили"
                if employee_id and a.get("to") == employee_id:
                    role = role or ("тебе передают файлы" if step.tool in ("send_files", "forward_document")
                                    else "тебя спрашивали")
                if chat and str(a.get("requested_chat") or "") == str(chat):
                    role = "ты поручил это ассистенту"
            if role:
                found.append({**_case_story(s, case), "your_role": role})
        found.sort(key=lambda c: (not c["open"], -c["id"]))
        return found[:limit]


def recent_view(hours: int = 48, limit: int = 8) -> list:
    """Cases of the last days for people with full rights: who was asked, what they answered, what is left.
    Lets the conversation understand «перешли ему» right after a case."""
    since = now() - dt.timedelta(hours=hours)
    with SessionLocal() as s:
        out = []
        for case in s.query(Case).filter(Case.updated_at >= since).order_by(Case.id.desc()).limit(limit):
            people, answers = set(), []
            for step in case.steps:
                for key in ("to", "from"):
                    e = s.get(Employee, (step.args or {}).get(key)) if (step.args or {}).get(key) else None
                    if e:
                        people.add((e.id, e.name))
                if step.tool == "wait_reply" and (step.result or {}).get("answer"):
                    answers.append(step.result["answer"][:400])
            story = _case_story(s, case, events=3)
            out.append({"id": case.id, "title": case.title, "status": story["status"],
                        "people": [{"id": i, "name": n} for i, n in sorted(people)], "answers": answers[-2:],
                        "waiting_for": story["waiting_for"], "last_events": story["last_events"],
                        "requested_by": story["requested_by"]})
        return out


_RUN = {"send_message": _send_message, "wait_reply": _wait_reply, "forward_document": _forward_document,
        "report": _report, "close": _close_step, "zip_pack": _zip_pack, "send_files": _send_files}


def _close(s, case) -> None:
    case.status, case.closed_at, case.note = "done", now(), ""
    if case.task is not None:
        case.task.status = "assistant_done"
        s.add(Note(task_id=case.task_id, meeting_id=case.meeting_id, kind="done", source="system", author="ассистент",
                   text=f"Ассистент довёл дело «{case.title}» до конца"[:800]))
    _event(s, case.id, "done", "Дело завершено")
    s.commit()


# ----------------------------------------------------------------------------- what comes back from people
def _classify(mime: str, filename: str) -> str:
    low = (filename or "").lower()
    if mime == "application/pdf" or low.endswith(".pdf"):
        return "pdf"
    if "wordprocessingml" in mime or "msword" in mime or low.endswith((".docx", ".doc")):
        return "docx"
    if "spreadsheetml" in mime or "ms-excel" in mime or low.endswith((".xlsx", ".xls", ".csv")):
        return "xlsx"
    if mime.startswith("image/") or low.endswith((".jpg", ".jpeg", ".png", ".heic", ".webp")):
        return "image"
    return "other"


def is_sensitive(filename: str, caption: str) -> bool:
    words = [w.strip().lower() for w in str(settings_store.get("sensitive_keywords") or "").split(",") if w.strip()]
    text = f" {filename} {caption} ".lower()
    return any(w in text for w in words)


def intake_document(msg: dict, employee_id) -> dict:
    """Download and keep a file a person sent. Returns a short description for the conversation."""
    info = msg["document"]
    name = str(info.get("filename") or "файл")[:255]
    if info.get("too_big") or int(info.get("size") or 0) > MAX_DOCUMENT_BYTES:
        return {"problem": "too_big", "filename": name}
    data = info["fetch"]()
    if not isinstance(data, (bytes, bytearray)) or not data:
        return {"problem": "empty", "filename": name}
    data = bytes(data)
    mime = str(info.get("mime") or "")
    caption = str(msg.get("text") or "")[:2000]
    with SessionLocal() as s:
        doc = Document(employee_id=employee_id, chat_id=str(msg["user_id"]), message_id=int(msg["message_id"]),
                       filename=name, mime=mime[:120], size=len(data), sha256=_sha(data), kind=_classify(mime, name),
                       caption=caption, sensitive=is_sensitive(name, caption), data=data)
        s.add(doc)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            doc = s.query(Document).filter_by(chat_id=str(msg["user_id"]), message_id=int(msg["message_id"])).first()
        result = {"document_id": doc.id, "filename": doc.filename, "kind": doc.kind, "size": doc.size}
    read_later(result["document_id"])
    return result


def read_later(document_id: int) -> None:
    """The text of a received document is read in the background (memory search, questions about it)."""
    if _testing():
        return  # tests call doctext.process themselves
    from . import doctext
    _pool().submit(doctext.process, document_id)


def _pick_wait(waits: list, reply_to):
    """Never guesses: the request the person answered (reply), or the only open one."""
    if reply_to:
        hit = [w for w in waits if w.asked_message_id and w.asked_message_id == reply_to]
        if len(hit) == 1:
            return hit[0]
    return waits[0] if len(waits) == 1 else None


_ACK = set("ок окей ok хорошо понял поняла принял приняла сейчас щас минуту секунду посмотрю уточню гляну спасибо "
           "ага угу сделаю отвечу напишу пришлю позже попозже чуть скоро и я вам тебе ладно займусь проверю узнаю "
           "завтра утром вечером сегодня днём".split())


def is_acknowledgement(text: str) -> bool:
    """«Ок, уточню и напишу» is a promise, not the answer: the case keeps waiting."""
    words = re.findall(r"[а-яёa-z]+", (text or "").lower())
    return bool(words) and len(words) <= 7 and all(w in _ACK for w in words) and not re.search(r"\d", text or "")


def on_message(employee_id, text: str, reply_to=None, document_id=None) -> dict:
    """An employee wrote or sent a file: does it answer a case? Returns what the conversation should know."""
    if not employee_id:
        return {}
    with SessionLocal() as s:
        expects = "document" if document_id else "answer"
        if not document_id and is_acknowledgement(text):
            for other in s.query(CaseWait).filter_by(employee_id=employee_id, status="open").all():
                _event(s, other.case_id, "note", f"Ответил(а): «{text[:200]}» — жду ответа по существу", other.step_id)
            s.commit()
            return {}
        waits = (s.query(CaseWait).filter(CaseWait.employee_id == employee_id, CaseWait.status == "open",
                                          CaseWait.expects == expects).order_by(CaseWait.id).all())
        wait = _pick_wait(waits, reply_to)
        if wait is None:
            if waits:
                return {"ambiguous_cases": [s.get(Case, w.case_id).title for w in waits][:5],
                        "kind": expects}
            if not document_id:
                for other in s.query(CaseWait).filter_by(employee_id=employee_id, status="open").all():
                    _event(s, other.case_id, "note", f"Написал(а), но документа пока нет: {text[:300]}", other.step_id)
                s.commit()
            return {}
        s.commit()
        wait_id = wait.id
    return _resolve(wait_id, answer=None if document_id else text, document_id=document_id)


def _resolve(wait_id: int, answer=None, document_id=None, by: str = "") -> dict:
    """The awaited answer or document is here (from Telegram, or entered by a person in the panel)."""
    with SessionLocal() as s:
        wait = s.get(CaseWait, wait_id)
        case = s.get(Case, wait.case_id)
        step = s.get(CaseStep, wait.step_id)
        wait.status, wait.resolved_at = "resolved", now()
        suffix = f" (внёс {by})" if by else ""
        if document_id:
            doc = s.get(Document, document_id)
            doc.case_id = case.id
            wait.document_id = doc.id
            step.result = {"document_id": doc.id, "sha256": doc.sha256, "filename": doc.filename}
            _event(s, case.id, "document_received", f"Получил «{doc.filename}» ({doc.size} байт){suffix}", step.id,
                   {"sha256": doc.sha256})
        else:
            wait.answer = str(answer or "")[:4000]
            step.result = {"answer": wait.answer}
            _event(s, case.id, "answer_received", f"Ответ{suffix}: {wait.answer[:600]}", step.id)
        step.status, step.finished_at = "done", now()
        if case.status in ("waiting", "attention"):
            case.status = "running"
        forward_to = [s.get(Employee, x.args["to"]).name for x in case.steps
                      if x.tool == "forward_document" and s.get(Employee, x.args["to"])]
        s.commit()
        link = {"case_id": case.id, "title": case.title, "kind": "document" if document_id else "answer",
                "forward_to": forward_to}
        case_id = case.id
    if _testing():
        advance(case_id)
    else:
        _pool().submit(advance, case_id)
    return link


_POOL = []


def _pool():
    if not _POOL:
        import concurrent.futures
        _POOL.append(concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="cases"))
    return _POOL[0]


# ----------------------------------------------------------------------------- decisions of the director
def approve(case_id: int, by: str = "") -> bool:
    """The director allows the plan or the pending document transfer."""
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        if case is None or case.status not in ("awaiting_approval", "suggested", "attention"):
            return False
        changed = False
        if case.status in ("awaiting_approval", "suggested"):
            case.status, changed = "running", True
            _event(s, case_id, "approved", f"План одобрил {by or 'руководитель'}")
        for step in case.steps:
            if step.status == "awaiting_approval":
                transfer = s.query(DocumentTransfer).filter_by(step_id=step.id, status="awaiting_approval").first()
                if transfer is not None:
                    transfer.status, transfer.approved_by = "approved", (by or "руководитель")[:120]
                step.status, changed = "pending", True
                case.status, case.note = "running", ""
                _event(s, case_id, "approved", f"Пересылку разрешил {by or 'руководитель'}", step.id)
        if not changed:
            return False
        case.retry_at, case.attempts = None, 0
        s.commit()
    advance(case_id) if _testing() else _pool().submit(advance, case_id)
    return True


def cancel(case_id: int, by: str = "") -> bool:
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        if case is None or case.status not in OPEN:
            return False
        case.status, case.closed_at = "cancelled", now()
        for wait in s.query(CaseWait).filter_by(case_id=case_id, status="open"):
            wait.status = "cancelled"
        for transfer in s.query(DocumentTransfer).filter_by(case_id=case_id, status="awaiting_approval"):
            transfer.status = "blocked"
        if case.task is not None:
            case.task.status = "assistant_failed"
        _event(s, case_id, "cancelled", f"Дело отменил {by or 'руководитель'}")
        s.commit()
    return True


# ----------------------------------------------------------------------------- a person steps in (Progress page)
def pause(case_id: int, by: str = "") -> bool:
    """Nothing is sent, nobody is reminded; answers that come meanwhile are still recorded."""
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        if case is None or case.status not in ("running", "waiting", "attention", "awaiting_approval", "suggested"):
            return False
        case.note = f"на паузе (было: {STATUS_RU.get(case.status, case.status)})"
        case.status = "paused"
        _event(s, case_id, "paused", f"Пауза: {by or 'руководитель'}")
        s.commit()
    return True


def resume(case_id: int, by: str = "") -> bool:
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        if case is None or case.status != "paused":
            return False
        waits = s.query(CaseWait).filter_by(case_id=case_id, status="open").all()
        moment = now()
        for wait in waits:  # the pause does not count as silence
            wait.next_reminder_at = moment + dt.timedelta(hours=_hours("case_reminder_hours", 4))
            if not wait.escalated:
                wait.escalate_at = max(_aware(wait.escalate_at) or moment, moment + dt.timedelta(hours=1))
        blocked = any(x.status in ("uncertain", "failed", "awaiting_approval") for x in case.steps)
        case.status = "waiting" if waits else ("attention" if blocked else "running")
        case.note, case.retry_at, case.attempts = "", None, 0
        _event(s, case_id, "resumed", f"Продолжено: {by or 'руководитель'}")
        s.commit()
        go = case.status == "running"
    if go:
        advance(case_id) if _testing() else _pool().submit(advance, case_id)
    return True


def _open_wait(s, case_id: int, expects=None):
    query = s.query(CaseWait).filter_by(case_id=case_id, status="open")
    if expects:
        query = query.filter_by(expects=expects)
    return query.order_by(CaseWait.id).first()


def record_answer(case_id: int, text: str, by: str = "") -> bool:
    """The answer came by phone or in person: a person enters it, the case goes on as if it came in Telegram."""
    text = str(text or "").strip()
    if not text:
        return False
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        wait = _open_wait(s, case_id, "answer") if case is not None and case.status in OPEN else None
        if wait is None:
            return False
        wait_id = wait.id
    _resolve(wait_id, answer=text[:4000], by=by or "руководитель")
    return True


def attach_document(case_id: int, filename: str, mime: str, data: bytes, by: str = "") -> bool:
    """The document came another way (email, paper scan): a person uploads it in the panel. It is kept as if the
    awaited person sent it, so every later check (hash, recipient, approval of sensitive files) still applies."""
    if not data or len(data) > MAX_DOCUMENT_BYTES:
        return False
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        wait = _open_wait(s, case_id, "document") if case is not None and case.status in OPEN else None
        if wait is None:
            return False
        name = str(filename or "файл")[:255]
        caption = f"загружен в панели: {by or 'руководитель'}"
        doc = Document(employee_id=wait.employee_id, chat_id="panel",
                       message_id=int(dt.datetime.now().timestamp() * 1000) % 2_000_000_000, filename=name,
                       mime=str(mime or "")[:120], size=len(data), sha256=_sha(bytes(data)), kind=_classify(mime or "", name),
                       caption=caption, sensitive=is_sensitive(name, ""), data=bytes(data))
        s.add(doc)
        s.commit()
        wait_id, doc_id = wait.id, doc.id
    read_later(doc_id)
    _resolve(wait_id, document_id=doc_id, by=by or "руководитель")
    return True


def unlinked_documents(s, employee_id) -> list:
    """Files this person sent that belong to no case (e.g. sent without a reply while two requests were open)."""
    if not employee_id:
        return []
    return [{"id": d.id, "filename": d.filename, "at": _aware(d.created_at)} for d in
            s.query(Document).filter(Document.employee_id == employee_id, Document.case_id.is_(None))
            .order_by(Document.id.desc()).limit(10)]


def link_document(case_id: int, document_id: int, by: str = "") -> bool:
    """A person says which case a file belongs to. Only a file of the awaited person, not used by another case."""
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        wait = _open_wait(s, case_id, "document") if case is not None and case.status in OPEN else None
        doc = s.get(Document, document_id)
        if wait is None or doc is None or doc.case_id is not None or doc.employee_id != wait.employee_id:
            return False
        wait_id = wait.id
    _resolve(wait_id, document_id=document_id, by=by or "руководитель")
    return True


def send_manual(case_id: int, text: str, by: str = "") -> bool:
    """A person writes in the assistant's chat with the awaited person: these exact words, from the AI account."""
    text = str(text or "").strip()
    if not text or len(text) > 3500 or not userbot.ai_mode():
        return False
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        if case is None or case.status not in OPEN:
            return False
        wait = _open_wait(s, case_id)
        person_id = wait.employee_id if wait else next((x.args.get("to") for x in case.steps if x.tool == "send_message"), None)
        person = s.get(Employee, person_id) if person_id else None
        if person is None or not person.telegram_chat_id:
            return False
        ref, name = person.telegram_chat_id, person.name
    try:
        _send_text(ref, text)
    except Exception as exc:  # noqa: BLE001
        with SessionLocal() as s:
            _event(s, case_id, "manual_failed", f"Сообщение {name} не отправлено: {str(exc)[:200]}")
            s.commit()
        return False
    with SessionLocal() as s:
        _event(s, case_id, "manual_message", f"{by or 'Руководитель'} написал {name} от имени ассистента: {text}")
        s.commit()
    return True


def hand_over(case_id: int, employee_id: int, by: str = "") -> bool:
    """The case goes to a person: the assistant stops, the task becomes theirs, they get a short handover note."""
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        person = s.get(Employee, employee_id)
        if case is None or case.status not in OPEN or person is None or not person.active:
            return False
        done_so_far = [e.text for e in case.events if e.kind in ("message_sent", "answer_received", "document_received",
                                                                 "document_forwarded", "reminder")][-6:]
        documents = [d.filename for d in s.query(Document).filter_by(case_id=case_id).all()]
        for wait in s.query(CaseWait).filter_by(case_id=case_id, status="open"):
            wait.status = "cancelled"
        for transfer in s.query(DocumentTransfer).filter_by(case_id=case_id, status="awaiting_approval"):
            transfer.status = "blocked"
        case.status, case.closed_at, case.note = "handed_over", now(), f"передано: {person.name}"
        task = case.task
        if task is not None:
            task.executor, task.assignee_id, task.assignee_name = "person", person.id, person.name
            task.status = "in_progress"
        s.add(Note(task_id=case.task_id, meeting_id=case.meeting_id, employee_id=person.id, kind="commitment",
                   source="system", author=(by or "руководитель")[:80],
                   text=f"Дело «{case.title}» передано: {person.name}"[:800]))
        _event(s, case_id, "handed_over", f"{by or 'Руководитель'} передал дело: {person.name}")
        s.commit()
        facts = {"task": {"title": case.title}, "person": {"name": person.name}, "done_so_far": done_so_far,
                 "documents": documents}
        ref = person.telegram_chat_id
    if ref and userbot.ai_mode():
        try:
            _send_text(ref, _write("handover", facts))
        except Exception as exc:  # noqa: BLE001
            with SessionLocal() as s:
                _event(s, case_id, "manual_failed", f"{facts['person']['name']} не получил(а) сообщение о передаче: "
                                                    f"{str(exc)[:200]}")
                s.commit()
    return True


def close_manually(case_id: int, by: str = "", note: str = "") -> bool:
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        if case is None or case.status not in OPEN:
            return False
        for wait in s.query(CaseWait).filter_by(case_id=case_id, status="open"):
            wait.status = "cancelled"
        for transfer in s.query(DocumentTransfer).filter_by(case_id=case_id, status="awaiting_approval"):
            transfer.status = "blocked"
        case.status, case.closed_at = "done", now()
        case.note = f"закрыто вручную{': ' + note.strip()[:300] if note and note.strip() else ''}"
        if case.task is not None:
            case.task.status = "assistant_done"
        _event(s, case_id, "closed", f"{by or 'Руководитель'} закрыл дело вручную" + (f": {note.strip()[:300]}" if note and note.strip() else ""))
        s.commit()
    return True


# ----------------------------------------------------------------------------- the clock
def run_due(now_utc=None) -> None:
    """Background loop: reminders, escalation, retries after the quiet hours or a model failure."""
    if not userbot.ai_mode():
        return
    moment = now_utc or now()
    quiet = userbot.quiet_until(now_utc) if now_utc is not None else userbot.quiet_until()
    with SessionLocal() as s:
        retry = [c.id for c in s.query(Case).filter(Case.status == "running").all()
                 if c.retry_at is None or _aware(c.retry_at) <= moment]
        due = [w.id for w in s.query(CaseWait).filter(CaseWait.status == "open").all()
               if w.next_reminder_at is not None and _aware(w.next_reminder_at) <= moment]
    if quiet is not None:
        return
    for case_id in retry:
        advance(case_id, moment)
    for wait_id in due:
        try:
            _nudge(wait_id, moment)
        except Exception:  # noqa: BLE001
            log.exception("case wait %s: reminder failed", wait_id)


def _nudge(wait_id: int, moment) -> None:
    max_reminders = int(_hours("case_max_reminders", 2))
    with SessionLocal() as s:
        wait = s.get(CaseWait, wait_id)
        if wait is None or wait.status != "open":
            return
        case = s.get(Case, wait.case_id)
        if case.status != "waiting":
            return  # paused, stopped or closed: no reminders
        person = s.get(Employee, wait.employee_id) if wait.employee_id else None
        asked = s.query(CaseStep).filter(CaseStep.case_id == case.id, CaseStep.tool == "send_message",
                                         CaseStep.status == "done").order_by(CaseStep.position.desc()).first()
        facts = {**_case_facts(s, case), "person": {"name": person.name if person else ""},
                 "what_was_asked": ((asked.result or {}).get("text") if asked else "") or case.title,
                 "reminder_number": wait.reminders_sent + 1}
        hours = round((moment - _aware(wait.started_at)).total_seconds() / 3600, 1)
        if wait.reminders_sent < max_reminders and person is not None:
            try:
                _checked_contact(s, person.id, wait.chat_ref)
            except CaseError as exc:
                s.commit()
                _attention(case.id, wait.step_id, str(exc))
                return
            ref, kind = person.telegram_chat_id, "remind"
        elif not wait.escalated and moment >= _aware(wait.escalate_at):
            ref, kind = None, "escalate"
        else:
            wait.next_reminder_at = wait.escalate_at if not wait.escalated else None
            s.commit()
            return
    if kind == "remind":
        try:
            text = _write("remind", facts)
            _send_text(ref, text)
        except (Retry, userbot.QuietHours) as exc:
            log.info("case wait %s: reminder later: %s", wait_id, exc)
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("case wait %s: reminder not sent: %s", wait_id, exc)
            return
        with SessionLocal() as s:
            wait = s.get(CaseWait, wait_id)
            wait.reminders_sent += 1
            wait.next_reminder_at = moment + dt.timedelta(hours=_hours("case_reminder_hours", 4))
            if wait.reminders_sent >= max_reminders:
                wait.next_reminder_at = max(wait.next_reminder_at, _aware(wait.escalate_at))
            _event(s, wait.case_id, "reminder", f"Напоминание №{wait.reminders_sent}: {text}", wait.step_id)
            s.commit()
        return
    told = _tell_leaders(case.id, "escalate", {"person": facts["person"], "hours": hours,
                                               "reminders": max_reminders, "what_was_asked": facts["what_was_asked"]})
    with SessionLocal() as s:
        wait = s.get(CaseWait, wait_id)
        wait.escalated, wait.next_reminder_at = True, None
        s.get(Case, wait.case_id).note = f"Нет ответа {hours} ч, руководитель уведомлён"
        _event(s, wait.case_id, "escalated", f"Нет ответа {hours} ч — сообщил руководителю" + ("" if told else
                                                                                             " (не доставлено)"),
               wait.step_id)
        s.commit()


def recover() -> None:
    """After a restart: a send that may have happened is never repeated automatically."""
    with SessionLocal() as s:
        for step in s.query(CaseStep).filter(CaseStep.status == "sending").all():
            step.status = "uncertain"
            case = s.get(Case, step.case_id)
            case.status, case.note = "attention", "отправка прервалась перезапуском; проверьте чат перед повтором"
            _event(s, case.id, "attention", case.note, step.id)
        for transfer in s.query(DocumentTransfer).filter_by(status="sending").all():
            transfer.status = "uncertain"
        s.commit()


def retry_step(case_id: int, by: str = "") -> bool:
    """A person checked the chat and asks to repeat the stopped step."""
    with SessionLocal() as s:
        case = s.get(Case, case_id)
        if case is None or case.status != "attention":
            return False
        step = next((x for x in case.steps if x.status in ("uncertain", "failed")), None)
        if step is None:
            return False
        step.status, step.error = "pending", ""
        for transfer in s.query(DocumentTransfer).filter(DocumentTransfer.step_id == step.id,
                                                         DocumentTransfer.status.in_(("uncertain",))):
            transfer.status = "approved"
        case.status, case.note, case.attempts, case.retry_at = "running", "", 0, None
        _event(s, case_id, "retry", f"Повтор шага разрешил {by or 'руководитель'}", step.id)
        s.commit()
    advance(case_id) if _testing() else _pool().submit(advance, case_id)
    return True


# ----------------------------------------------------------------------------- views
def waiting_view(employee_id) -> list:
    """What the assistant is waiting for from this person (the conversation model sees it)."""
    if not employee_id:
        return []
    with SessionLocal() as s:
        out = []
        for wait in s.query(CaseWait).filter_by(employee_id=employee_id, status="open").order_by(CaseWait.id).limit(5):
            case = s.get(Case, wait.case_id)
            out.append({"case": case.title, "expects": wait.expects,
                        "since_hours": round((now() - _aware(wait.started_at)).total_seconds() / 3600, 1)})
        return out


def decisions_view() -> list:
    """Cases waiting for the director (plans to approve, transfers to allow, stopped cases)."""
    with SessionLocal() as s:
        return [{"id": c.id, "title": c.title, "status": c.status, "status_ru": STATUS_RU.get(c.status, c.status),
                 "plan": describe_plan(s, c), "note": c.note or None}
                for c in s.query(Case).filter(Case.status.in_(("awaiting_approval", "suggested", "attention")))
                .order_by(Case.id).limit(10)]


def for_meeting(meeting_id: int) -> dict:
    """task_id -> a small view of its case for the meeting page."""
    with SessionLocal() as s:
        out = {}
        for case in s.query(Case).filter_by(meeting_id=meeting_id).all():
            out[case.task_id] = {"id": case.id, "status": case.status, "status_ru": STATUS_RU.get(case.status, case.status),
                                 "note": case.note, "plan": describe_plan(s, case), "planner": case.planner,
                                 "events": [{"at": _aware(e.created_at), "text": e.text} for e in case.events[-8:]]}
        return out
