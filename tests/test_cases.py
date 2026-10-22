"""Sprint 2: the assistant carries cases by itself — asks, waits, reminds, escalates, receives and forwards documents."""
import datetime as dt
import hashlib
import json
import random

import pytest

from app import agent, assistant, cases, llm, settings_store
from app.db import (Case, CaseEvent, CaseStep, CaseWait, Document, DocumentTransfer, Employee, Meeting, SessionLocal,
                    Task, now)
from test_ai_mode import FakeBackend, FakeModel, tgacc  # noqa: F401  (fixtures)

PDF = b"%PDF-1.4 act of works no. 17"


class CaseModel(FakeModel):
    """The conversation fake of test_ai_mode plus the case writer and the planner."""

    def __init__(self):
        super().__init__()
        self.plans = []
        self.writes = []
        self.write_down = False

    def __call__(self, model_name, messages, **kwargs):
        system = messages[0]["content"]
        facts = json.loads(messages[1]["content"].split("<data>")[1].split("</data>")[0])
        if "Составь план дела" in system:
            self.calls.append({"system": system, "facts": facts})
            if not self.plans:
                raise llm.LLMError("planner is off in this test")
            return json.dumps({"steps": self.plans.pop(0)})
        if "Ты ведёшь дело" in system:
            self.writes.append(facts)
            if self.write_down:
                raise llm.LLMError("модель недоступна")
            extra = ""
            if facts["purpose"] == "report":
                extra = " ответы: " + "; ".join(a["text"] for a in facts.get("answers", []))
            return json.dumps({"text": f"[{facts['purpose']}] {facts.get('case', {}).get('title', '')}{extra}"},
                              ensure_ascii=False)
        if "Тебе пишет человек" in system and not self.decisions:
            self.decisions.append({"reply": "Спасибо, принял!", "actions": []})
        return super().__call__(model_name, messages, **kwargs)


@pytest.fixture
def cmodel(monkeypatch):
    fake = CaseModel()
    monkeypatch.setattr(llm, "chat", fake)
    return fake


@pytest.fixture
def office(tgacc):
    """A small company: the director Вадим (@boss), Анна, Пётр, Иван; the AI employee account is connected."""
    tgacc.users.update({"@petya": 44, "@anna": 43, "@ivan": 42, "@boss": 7})
    settings_store.set_many({"director_chat_ids": ["@boss"], "quiet_from_hour": 0, "quiet_to_hour": 0,
                             "case_autonomy": "auto"})
    with SessionLocal() as s:
        people = {k: Employee(name=n, telegram_chat_id=ref, position=p) for k, n, ref, p in (
            ("anna", "Анна Смирнова", "@anna", "Бухгалтер"), ("petya", "Пётр Иванов", "@petya", "Юрист"),
            ("ivan", "Иван Петров", "@ivan", "Инженер"))}
        s.add_all(people.values())
        s.commit()
        ids = {k: v.id for k, v in people.items()}
    agent.invalidate_roster()
    return ids


def make_task(office, kind="forward", counterparts=(("source", "anna"), ("recipient", "petya")),
              title="Взять у Ани акт и отправить Пете", target="anna"):
    with SessionLocal() as s:
        names = {e.id: e.name for e in s.query(Employee).all()}
        meeting = Meeting(title="Планёрка", status="done", report={"title": "Планёрка"}, options={})
        task = Task(title=title, executor="assistant", action_type=kind, status="assistant_todo",
                    source_quote=title, target_employee_id=office[target] if target else None,
                    counterparts=[{"role": r, "employee_id": office[k], "name": names[office[k]]} for r, k in counterparts])
        meeting.tasks.append(task)
        s.add(meeting)
        s.commit()
        return task.id


def incoming(user_id, username, text="", message_id=500, document=None, reply_to=None):
    msg = {"user_id": user_id, "username": username, "phone": "", "name": username, "text": text,
           "message_id": message_id, "reply_to": reply_to}
    if document is not None:
        msg["document"] = document
    return msg


def doc(data=PDF, filename="Акт №17.pdf", mime="application/pdf"):
    return {"fetch": lambda: data, "size": len(data), "mime": mime, "filename": filename}


def case_of(task_id):
    with SessionLocal() as s:
        case = s.query(Case).filter_by(task_id=task_id).one()
        return case.id, case.status, [(x.tool, x.status) for x in case.steps], s.get(Task, task_id).status


# ----------------------------------------------------------------------------- the scenario of the plan
def test_take_the_act_from_anna_and_send_it_to_petya_without_a_human(office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    case_id, status, steps, task_status = case_of(task_id)
    assert status == "waiting" and task_status == "assistant_waiting"
    assert tgacc.sent[-1][0] == "@anna" and tgacc.sent[-1][1].startswith("[request]")
    assert cmodel.writes[-1]["forward_to"] == ["Пётр Иванов"]  # Anna is told who gets the act

    agent.handle_incoming(incoming(43, "anna", document=doc()))

    case_id, status, steps, task_status = case_of(task_id)
    assert status == "done" and task_status == "assistant_done"
    assert [t for t, _ in steps] == ["send_message", "wait_reply", "forward_document", "report", "close"]
    ref, filename, caption = tgacc.files[-1]
    assert ref == "@petya" and filename == "Акт №17.pdf" and caption.startswith("[deliver]")
    assert tgacc.file_data[-1] == PDF  # the very bytes Anna sent
    assert any(r == "@boss" and t.startswith("[report]") for r, t in tgacc.sent)
    with SessionLocal() as s:
        transfer = s.query(DocumentTransfer).one()
        assert transfer.status == "sent" and transfer.sha256 == hashlib.sha256(PDF).hexdigest()
        document = s.query(Document).one()
        assert document.case_id == case_id and document.kind == "pdf" and document.employee_id == office["anna"]
        kinds = [e.kind for e in s.query(CaseEvent).filter_by(case_id=case_id).order_by(CaseEvent.id)]
    assert kinds[:4] == ["planned", "message_sent", "waiting", "document_received"]
    assert "document_forwarded" in kinds and kinds[-1] == "done"
    # Anna still gets a human answer from the conversation, which knows the file belongs to the case
    decision_call = [c for c in cmodel.calls if "Тебе пишет человек" in c["system"]][-1]
    assert decision_call["facts"]["this_message_for_case"]["forward_to"] == ["Пётр Иванов"]
    assert tgacc.sent[-1] == ("43", "Спасибо, принял!")


def test_ask_case_brings_the_answer_back_to_the_director(office, tgacc, cmodel):
    task_id = make_task(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan",
                        title="Узнать у Ивана статус договора")
    assistant.run_task(task_id)
    assert tgacc.sent[-1][0] == "@ivan" and tgacc.sent[-1][1].startswith("[ask]")
    agent.handle_incoming(incoming(42, "ivan", text="Договор подписан вчера, оригинал у Петра", message_id=601))
    _, status, _, task_status = case_of(task_id)
    assert status == "done" and task_status == "assistant_done"
    report = [t for r, t in tgacc.sent if r == "@boss"][-1]
    assert "Договор подписан вчера" in report


def test_planner_plan_is_used_when_it_passes_the_checks(office, tgacc, cmodel):
    anna, petya = office["anna"], office["petya"]
    cmodel.plans.append([{"tool": "send_message", "to": anna, "purpose": "request"},
                         {"tool": "wait_reply", "from": anna, "expects": "document"},
                         {"tool": "forward_document", "from": anna, "to": petya},
                         {"tool": "report"}, {"tool": "close"}])
    task_id = make_task(office)
    assistant.run_task(task_id)
    with SessionLocal() as s:
        assert s.query(Case).filter_by(task_id=task_id).one().planner == "model"


@pytest.mark.parametrize("bad", [
    "stranger", "skips_recipient", "forward_without_wait", "no_close", "unknown_tool", "asks_the_recipient",
    "no_report",
])
def test_planner_plan_with_a_wrong_person_or_shape_is_replaced_by_the_template(office, tgacc, cmodel, bad):
    anna, petya, ivan = office["anna"], office["petya"], office["ivan"]
    plans = {
        "stranger": [{"tool": "send_message", "to": ivan, "purpose": "request"},
                     {"tool": "wait_reply", "from": ivan, "expects": "document"},
                     {"tool": "forward_document", "from": ivan, "to": petya}, {"tool": "close"}],
        "skips_recipient": [{"tool": "send_message", "to": anna, "purpose": "request"},
                            {"tool": "wait_reply", "from": anna, "expects": "document"}, {"tool": "close"}],
        "forward_without_wait": [{"tool": "forward_document", "from": anna, "to": petya}, {"tool": "close"}],
        "no_close": [{"tool": "send_message", "to": anna, "purpose": "request"},
                     {"tool": "wait_reply", "from": anna, "expects": "document"},
                     {"tool": "forward_document", "from": anna, "to": petya}],
        "unknown_tool": [{"tool": "send_email", "to": "x@y"}, {"tool": "close"}],
        # found by the live check: the model asked Petya (the recipient) to send the act too
        "asks_the_recipient": [{"tool": "send_message", "to": anna, "purpose": "request"},
                               {"tool": "wait_reply", "from": anna, "expects": "document"},
                               {"tool": "send_message", "to": petya, "purpose": "request"},
                               {"tool": "forward_document", "from": anna, "to": petya},
                               {"tool": "report"}, {"tool": "close"}],
        # found by the live check: the director was never told the result
        "no_report": [{"tool": "send_message", "to": anna, "purpose": "request"},
                      {"tool": "wait_reply", "from": anna, "expects": "document"},
                      {"tool": "forward_document", "from": anna, "to": petya}, {"tool": "close"}],
    }
    cmodel.plans.append(plans[bad])
    task_id = make_task(office)
    assistant.run_task(task_id)
    with SessionLocal() as s:
        case = s.query(Case).filter_by(task_id=task_id).one()
        assert case.planner == "template"
        assert [x.tool for x in case.steps] == ["send_message", "wait_reply", "forward_document", "report", "close"]
    assert all(r not in ("@ivan", "@petya") for r, _ in tgacc.sent)


def test_task_without_anybody_from_the_directory_fails_visibly(office, tgacc, cmodel):
    task_id = make_task(office, counterparts=(), target=None, title="Запросить протокол у заказчика")
    assistant.run_task(task_id)
    _, status, _, task_status = case_of(task_id)
    assert status == "failed" and task_status == "assistant_failed"
    assert tgacc.sent[-1][0] == "@boss" and tgacc.sent[-1][1].startswith("[failure]")


# ----------------------------------------------------------------------------- waiting: reminders, escalation
def _shift_wait(task_id, hours):
    with SessionLocal() as s:
        wait = s.query(CaseWait).join(Case, Case.id == CaseWait.case_id).filter(Case.task_id == task_id).one()
        return wait.id, now() + dt.timedelta(hours=hours)


def test_no_answer_gets_polite_reminders_then_the_director_is_told(office, tgacc, cmodel):
    settings_store.set_many({"case_reminder_hours": 4, "case_max_reminders": 2, "case_escalate_hours": 24})
    task_id = make_task(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan", title="Узнать у Ивана сроки")
    assistant.run_task(task_id)
    sent_before = len(tgacc.sent)
    cases.run_due(now() + dt.timedelta(hours=1))
    assert len(tgacc.sent) == sent_before  # too early
    cases.run_due(now() + dt.timedelta(hours=5))
    assert tgacc.sent[-1][0] == "@ivan" and tgacc.sent[-1][1].startswith("[remind]")
    assert cmodel.writes[-1]["reminder_number"] == 1
    cases.run_due(now() + dt.timedelta(hours=10))
    assert cmodel.writes[-1]["reminder_number"] == 2
    count = len(tgacc.sent)
    cases.run_due(now() + dt.timedelta(hours=15))
    assert len(tgacc.sent) == count  # no third reminder, escalation not due yet
    cases.run_due(now() + dt.timedelta(hours=25))
    assert tgacc.sent[-1][0] == "@boss" and tgacc.sent[-1][1].startswith("[escalate]")
    cases.run_due(now() + dt.timedelta(hours=30))
    assert len(tgacc.sent) == count + 1  # the director is told once
    with SessionLocal() as s:
        wait = s.query(CaseWait).one()
        assert wait.reminders_sent == 2 and wait.escalated and wait.status == "open"
    # a late answer still closes the case
    agent.handle_incoming(incoming(42, "ivan", text="К пятнице", message_id=700))
    assert case_of(task_id)[1] == "done"


def test_reminders_wait_for_the_end_of_quiet_hours(office, tgacc, cmodel):
    settings_store.set_many({"quiet_from_hour": 21, "quiet_to_hour": 9, "case_reminder_hours": 1})
    task_id = make_task(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan", title="Узнать у Ивана сроки")
    assistant.run_task(task_id)  # quiet hours are off in tests unless a moment is given
    count = len(tgacc.sent)
    import zoneinfo
    from app import config
    night = dt.datetime.now(zoneinfo.ZoneInfo(config.TIMEZONE)).replace(hour=23, minute=30) + dt.timedelta(days=1)
    cases.run_due(night.astimezone(dt.timezone.utc))
    assert len(tgacc.sent) == count
    morning = night.replace(hour=10) + dt.timedelta(days=1)
    cases.run_due(morning.astimezone(dt.timezone.utc))
    assert tgacc.sent[-1][1].startswith("[remind]")


def test_a_text_while_waiting_for_a_document_does_not_close_the_wait(office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    agent.handle_incoming(incoming(43, "anna", text="Пришлю завтра утром", message_id=510))
    _, status, _, _ = case_of(task_id)
    assert status == "waiting" and not tgacc.files
    with SessionLocal() as s:
        assert s.query(CaseEvent).filter_by(kind="note").count() == 1


# ----------------------------------------------------------------------------- documents: safety
def test_a_document_from_somebody_else_is_kept_but_never_forwarded(office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    agent.handle_incoming(incoming(42, "ivan", document=doc(b"other file", "Счёт.pdf"), message_id=520))
    assert case_of(task_id)[1] == "waiting" and not tgacc.files
    with SessionLocal() as s:
        stray = s.query(Document).one()
        assert stray.case_id is None and stray.employee_id == office["ivan"]


def test_sensitive_file_waits_for_the_director_and_goes_after_approval(office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    agent.handle_incoming(incoming(43, "anna", document=doc(b"salary data", "Зарплатная ведомость.xlsx",
                                                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")))
    case_id, status, steps, _ = case_of(task_id)
    assert status == "attention" and not tgacc.files
    assert ("forward_document", "awaiting_approval") in steps
    assert tgacc.sent[-2][0] == "@boss" and tgacc.sent[-2][1].startswith("[doc_approval]")
    # the director says «да» in Telegram: the conversation model turns it into approve_case
    cmodel.decisions.append(lambda facts: {"reply": "Переслал.", "actions": [
        {"type": "approve_case", "case_id": facts["cases_awaiting_decision"][0]["id"]}]})
    agent.handle_incoming(incoming(7, "boss", text="да, пересылай", message_id=530))
    assert case_of(task_id)[1] == "done"
    assert tgacc.files[-1][0] == "@petya"
    with SessionLocal() as s:
        assert s.query(DocumentTransfer).one().approved_by


def test_an_employee_cannot_approve_a_case(office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    agent.handle_incoming(incoming(43, "anna", document=doc(b"x", "Паспорт.jpg", "image/jpeg")))
    case_id = case_of(task_id)[0]
    ctx = {"cases_awaiting_decision": [{"id": case_id}]}
    assert agent._check({"type": "approve_case", "case_id": case_id}, ctx, None, reviewer=False, full=False)
    assert agent._denied({"type": "approve_case", "case_id": case_id}, None, reviewer=False, full=False)


def test_changed_contact_of_the_recipient_stops_the_transfer(office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    with SessionLocal() as s:
        s.get(Employee, office["petya"]).telegram_chat_id = "@petya_new"
        s.commit()
    agent.handle_incoming(incoming(43, "anna", document=doc()))
    case_id, status, steps, _ = case_of(task_id)
    assert status == "attention" and not tgacc.files
    with SessionLocal() as s:
        assert "изменился" in s.get(Case, case_id).note


def test_document_changed_after_receipt_is_not_forwarded(office, tgacc, cmodel, monkeypatch):
    task_id = make_task(office)
    assistant.run_task(task_id)
    real = cases._forward_document

    def tamper(case_id, step_id):
        with SessionLocal() as s:
            d = s.query(Document).one()
            d.data = b"forged content"
            s.commit()
        return real(case_id, step_id)
    monkeypatch.setitem(cases._RUN, "forward_document", tamper)
    agent.handle_incoming(incoming(43, "anna", document=doc()))
    case_id, status, _, _ = case_of(task_id)
    assert status == "attention" and not tgacc.files
    with SessionLocal() as s:
        assert "хеш" in s.get(Case, case_id).note


def test_interrupted_send_is_never_repeated_by_itself(office, tgacc, cmodel):
    task_id = make_task(office)
    with SessionLocal() as s:
        pass
    tgacc.fail = RuntimeError("connection reset")
    assistant.run_task(task_id)
    case_id, status, steps, _ = case_of(task_id)
    assert status == "attention" and steps[0] == ("send_message", "uncertain")
    tgacc.fail = None
    cases.run_due(now() + dt.timedelta(hours=1))
    assert not [r for r, _ in tgacc.sent if r == "@anna"]
    assert cases.retry_step(case_id, "Вадим")  # after checking the chat, a person repeats it
    assert [r for r, _ in tgacc.sent if r == "@anna"]
    assert case_of(task_id)[1] == "waiting"


def test_crash_while_sending_marks_the_step_uncertain(office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    with SessionLocal() as s:
        case = s.query(Case).one()
        case.steps[2].status = "sending"
        case.status = "running"
        s.commit()
    cases.recover()
    _, status, steps, _ = case_of(task_id)
    assert status == "attention" and steps[2] == ("forward_document", "uncertain")


def test_model_down_postpones_the_step_and_nothing_is_sent(office, tgacc, cmodel):
    cmodel.write_down = True
    task_id = make_task(office)
    assistant.run_task(task_id)
    case_id, status, steps, _ = case_of(task_id)
    assert status == "running" and steps[0] == ("send_message", "pending")
    assert not [r for r, _ in tgacc.sent if r == "@anna"]
    cmodel.write_down = False
    cases.run_due(now() + dt.timedelta(minutes=10))
    assert case_of(task_id)[1] == "waiting"


# ----------------------------------------------------------------------------- autonomy levels
def test_approve_level_waits_for_the_director_before_writing_to_anybody(office, tgacc, cmodel):
    settings_store.set_many({"case_autonomy": "approve"})
    task_id = make_task(office)
    assistant.run_task(task_id)
    case_id, status, _, _ = case_of(task_id)
    assert status == "awaiting_approval"
    assert [r for r, _ in tgacc.sent] == ["@boss"] and tgacc.sent[0][1].startswith("[approval]")
    assert cases.approve(case_id, "Вадим")
    assert case_of(task_id)[1] == "waiting" and tgacc.sent[-1][0] == "@anna"


def test_suggest_level_only_proposes(office, tgacc, cmodel):
    settings_store.set_many({"case_autonomy": "suggest"})
    task_id = make_task(office)
    assistant.run_task(task_id)
    assert case_of(task_id)[1] == "suggested"
    cases.run_due(now() + dt.timedelta(hours=48))
    assert [r for r, _ in tgacc.sent] == ["@boss"]
    assert cases.cancel(case_of(task_id)[0], "Вадим")
    assert case_of(task_id)[1] == "cancelled" and case_of(task_id)[3] == "assistant_failed"


def test_case_is_started_once_even_if_the_task_runs_twice(office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    with SessionLocal() as s:
        s.get(Task, task_id).status = "assistant_todo"
        s.commit()
    assistant.run_task(task_id)
    with SessionLocal() as s:
        assert s.query(Case).count() == 1
    assert len([r for r, _ in tgacc.sent if r == "@anna"]) == 1


def test_two_open_requests_to_one_person_need_a_reply_to_pick_the_case(office, tgacc, cmodel):
    first = make_task(office, title="Взять у Ани акт и отправить Пете")
    second = make_task(office, title="Взять у Ани счёт и отправить Ивану",
                       counterparts=(("source", "anna"), ("recipient", "ivan")))
    assistant.run_task(first)
    assistant.run_task(second)
    agent.handle_incoming(incoming(43, "anna", document=doc(), message_id=540))
    assert not tgacc.files  # never guessed
    decision_call = [c for c in cmodel.calls if "Тебе пишет человек" in c["system"]][-1]
    assert len(decision_call["facts"]["this_message_for_case"]["ambiguous_cases"]) == 2
    with SessionLocal() as s:
        asked = s.query(CaseWait).join(Case, Case.id == CaseWait.case_id).filter(Case.task_id == second).one().asked_message_id
    agent.handle_incoming(incoming(43, "anna", document=doc(b"invoice", "Счёт.pdf"), message_id=541, reply_to=asked))
    assert tgacc.files[-1][0] == "@ivan" and tgacc.file_data[-1] == b"invoice"
    assert case_of(second)[1] == "done" and case_of(first)[1] == "waiting"


# ----------------------------------------------------------------------------- 50 forwardings, zero wrong recipients
def test_fifty_forwardings_never_reach_a_wrong_recipient(tgacc, cmodel, monkeypatch):
    from app import userbot
    monkeypatch.setattr(userbot, "DAILY_LIMIT", 10_000)  # 50 cases send ~250 messages; the real account keeps its limit
    settings_store.set_many({"director_chat_ids": ["@boss"], "quiet_from_hour": 0, "quiet_to_hour": 0,
                             "case_autonomy": "auto", "sensitive_keywords": "секрет"})
    rng = random.Random(2026)
    with SessionLocal() as s:
        staff = [Employee(name=f"Сотрудник {i}", telegram_chat_id=f"@user{i}", position="Специалист") for i in range(12)]
        s.add_all(staff)
        s.commit()
        staff = [(e.id, e.name, e.telegram_chat_id) for e in staff]
    for i, (_, _, ref) in enumerate(staff):
        tgacc.users[ref] = 1000 + i
    agent.invalidate_roster()
    expected, message_id = {}, 9000
    for n in range(50):
        src, dst = rng.sample(range(12), 2)
        with SessionLocal() as s:
            meeting = Meeting(title=f"М{n}", status="done", report={}, options={})
            task = Task(title=f"Взять у {staff[src][1]} файл {n} и отправить {staff[dst][1]}", executor="assistant",
                        action_type="forward", status="assistant_todo", target_employee_id=staff[src][0],
                        counterparts=[{"role": "source", "employee_id": staff[src][0], "name": staff[src][1]},
                                      {"role": "recipient", "employee_id": staff[dst][0], "name": staff[dst][1]}])
            meeting.tasks.append(task)
            s.add(meeting)
            s.commit()
            task_id = task.id
        assistant.run_task(task_id)
        with SessionLocal() as s:
            asked = s.query(CaseWait).join(Case, Case.id == CaseWait.case_id).filter(Case.task_id == task_id).one().asked_message_id
        payload = f"файл-{n}-{rng.random()}".encode()
        expected[hashlib.sha256(payload).hexdigest()] = staff[dst][2]
        # noise: somebody else sends a file meanwhile; it must never be forwarded
        noise = rng.choice([i for i in range(12) if i not in (src,)])
        message_id += 1
        agent.handle_incoming(incoming(1000 + noise, f"user{noise}", document=doc(f"шум {n}".encode(), f"шум{n}.pdf"),
                                       message_id=message_id))
        message_id += 1
        agent.handle_incoming(incoming(1000 + src, f"user{src}", document=doc(payload, f"файл{n}.pdf"),
                                       message_id=message_id, reply_to=asked))
    sent = [(ref, hashlib.sha256(data).hexdigest()) for (ref, _, _), data in zip(tgacc.files, tgacc.file_data)]
    assert len(sent) == 50
    assert all(expected.get(sha) == ref for ref, sha in sent), "a document reached a wrong recipient"
    assert {sha for _, sha in sent} == set(expected)
    with SessionLocal() as s:
        assert s.query(Case).filter_by(status="done").count() == 50
        assert s.query(DocumentTransfer).filter_by(status="sent").count() == 50
        assert s.query(Document).filter(Document.case_id.is_(None)).count() >= 1  # noise stayed in the library


# ----------------------------------------------------------------------------- panel
def test_meeting_page_shows_the_case_and_the_director_can_approve(client, office, tgacc, cmodel):
    settings_store.set_many({"case_autonomy": "approve"})
    task_id = make_task(office)
    assistant.run_task(task_id)
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        case_id, meeting_id = s.query(Case).one().id, task.meeting_id
    page = client.get(f"/meetings/{meeting_id}").text
    assert "ждёт одобрения плана" in page and "попросить документ у Анна Смирнова" in page
    response = client.post(f"/cases/{case_id}/approve", follow_redirects=False)
    assert response.status_code == 303
    assert case_of(task_id)[1] == "waiting"
    assert client.post(f"/cases/{case_id}/approve", follow_redirects=False).status_code == 409


def test_document_library_lists_and_serves_files(client, office, tgacc, cmodel):
    task_id = make_task(office)
    assistant.run_task(task_id)
    agent.handle_incoming(incoming(43, "anna", document=doc()))
    page = client.get("/documents").text
    assert "Акт №17.pdf" in page and "Пётр Иванов" in page and "передан" in page
    with SessionLocal() as s:
        doc_id = s.query(Document).one().id
    response = client.get(f"/documents/{doc_id}")
    assert response.status_code == 200 and response.content == PDF
    assert "Акт" in client.get("/documents?q=Акт").text


def test_case_settings_are_saved(client, tgacc):
    response = client.post("/settings", data={"case_autonomy": "approve", "case_reminder_hours": "2",
                                              "case_max_reminders": "3", "case_escalate_hours": "48"},
                           follow_redirects=False)
    assert response.status_code in (200, 303)
    assert settings_store.get("case_autonomy") == "approve" and settings_store.get("case_max_reminders") == 3
    assert client.post("/settings", data={"case_autonomy": "always"}).status_code == 400
    page = client.get("/settings").text
    assert "Дела ассистента" in page and 'value="approve" selected' in page


@pytest.mark.parametrize("text, ack", [("ок", True), ("Ок, уточню и напишу", True), ("сейчас гляну", True),
                                       ("Подписан вчера", False), ("да", False), ("к 15 числу", False),
                                       ("Пришлю завтра утром", True)])
def test_promises_are_not_answers(text, ack):
    assert cases.is_acknowledgement(text) is ack


def test_a_promise_keeps_the_question_open(office, tgacc, cmodel):
    task_id = make_task(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan", title="Узнать у Ивана сроки")
    assistant.run_task(task_id)
    agent.handle_incoming(incoming(42, "ivan", text="Ок, уточню и напишу", message_id=800))
    assert case_of(task_id)[1] == "waiting"
    agent.handle_incoming(incoming(42, "ivan", text="Сдадим объект 14 ноября", message_id=801))
    assert case_of(task_id)[1] == "done"
    assert "14 ноября" in [t for r, t in tgacc.sent if r == "@boss"][-1]


def test_api_keys_never_appear_in_model_errors():
    error = llm.LLMError("OpenRouter недоступен: Illegal header value b'Bearer sk-or-v1-0123456789abcdef0123456789\\n'")
    assert "0123456789abcdef" not in str(error)
    assert "abcdef0123" not in llm.redact("key sk-or-v1-0123456789abcdef0123456789 leaked")
    assert "secret-token" not in llm.redact("Authorization: Bearer secret-token")
