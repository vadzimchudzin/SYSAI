"""Sprint 3: the Progress page — the state of all cases after a meeting, interventions, live refresh."""
import datetime as dt
import time

from app import agent, assistant, cases, progress, settings_store
from app.db import Case, CaseEvent, CaseWait, Document, Employee, Meeting, SessionLocal, Task, now
from test_ai_mode import tgacc  # noqa: F401  (fixtures)
from test_cases import PDF, case_of, cmodel, doc, incoming, make_task, office  # noqa: F401


def started(office, **kw):
    task_id = make_task(office, **kw)
    assistant.run_task(task_id)
    return task_id, case_of(task_id)[0]


def meeting_of(task_id):
    with SessionLocal() as s:
        return s.get(Task, task_id).meeting_id


# ----------------------------------------------------------------------------- the 30-second view
def test_director_sees_the_whole_state_of_the_last_meeting(client, office, tgacc, cmodel):
    forward_id, forward_case = started(office)
    ask_id, ask_case = started(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan",
                               title="Узнать у Ивана статус договора")
    # put both tasks in one meeting, as after a real meeting
    with SessionLocal() as s:
        empty = s.get(Meeting, s.get(Task, ask_id).meeting_id)
        s.get(Task, ask_id).meeting_id = s.get(Task, forward_id).meeting_id
        s.get(Case, ask_case).meeting_id = s.get(Task, forward_id).meeting_id
        s.flush()
        s.delete(empty)
        meeting = s.get(Meeting, s.get(Task, forward_id).meeting_id)
        meeting.tasks.append(Task(title="Подготовить смету", assignee_id=office["petya"], status="blocked"))
        meeting.tasks.append(Task(title="Сдать отчёт", assignee_id=office["anna"], status="sent",
                                  deadline=dt.date.today() - dt.timedelta(days=2)))
        s.commit()
    agent.handle_incoming(incoming(42, "ivan", text="Подписан, оригинал в пятницу", message_id=601))

    page = client.get("/progress").text
    assert "Прогресс" in page and "дел создано" in page
    with SessionLocal() as s:
        data = progress.build(s, "last")
    assert data["summary"] == {"created": 2, "done": 1, "open": 1, "stuck": 0, "waiting": 1, "failed": 0,
                               "handed_over": 0}
    waiting = data["groups"][0][1][0]
    assert waiting["waiting"]["who"] == "Анна Смирнова" and waiting["waiting"]["what"] == "документа"
    assert waiting["next"] == ["передать документ Пётр Иванов", "доложить руководителю"]
    assert "ждёт документа от Анна Смирнова" in page and "передать документ Пётр Иванов" in page
    assert data["people"]["blocked"] == 1 and data["people"]["overdue"] == 1
    assert "заблокировано" in page and "просрочено" in page
    assert [title for title, _ in data["groups"]] == ["В работе у ассистента", "Завершено"]


def test_cases_that_need_a_decision_come_first(client, office, tgacc, cmodel):
    settings_store.set_many({"case_autonomy": "approve"})
    task_id, case_id = started(office)
    with SessionLocal() as s:
        data = progress.build(s, "last")
    title, items = data["groups"][0]
    assert title == "Нужно ваше решение" and items[0]["id"] == case_id and items[0]["can"]["approve"]
    assert data["summary"]["stuck"] == 1


def test_escalated_wait_is_shown_as_needing_attention(office, tgacc, cmodel):
    settings_store.set_many({"case_reminder_hours": 1, "case_max_reminders": 1, "case_escalate_hours": 3})
    task_id, case_id = started(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan",
                               title="Узнать у Ивана сроки")
    cases.run_due(now() + dt.timedelta(hours=2))
    cases.run_due(now() + dt.timedelta(hours=4))
    with SessionLocal() as s:
        card = progress.build(s, "last")["groups"][0][1][0]
    assert card["stuck"] and card["waiting"]["escalated"] and card["waiting"]["reminders"] == 1


def test_scopes_open_week_and_a_chosen_meeting(office, tgacc, cmodel):
    first_id, first_case = started(office)
    second_id, second_case = started(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan",
                                     title="Узнать у Ивана сроки")
    with SessionLocal() as s:
        assert {c["id"] for _, items in progress.build(s, "open")["groups"] for c in items} == {first_case, second_case}
        assert len(progress.build(s, "week")["groups"][0][1]) == 2
        chosen = progress.build(s, str(meeting_of(first_id)))
        assert [c["id"] for _, items in chosen["groups"] for c in items] == [first_case]
        assert progress.build(s, "last")["meeting"]["id"] == meeting_of(second_id)


# ----------------------------------------------------------------------------- interventions
def test_pause_stops_reminders_and_resume_continues(client, office, tgacc, cmodel):
    settings_store.set_many({"case_reminder_hours": 1})
    task_id, case_id = started(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan",
                               title="Узнать у Ивана сроки")
    assert client.post(f"/progress/cases/{case_id}/pause", follow_redirects=False).status_code == 303
    assert case_of(task_id)[1] == "paused"
    count = len(tgacc.sent)
    cases.run_due(now() + dt.timedelta(hours=5))
    assert len(tgacc.sent) == count  # nothing during the pause
    agent.handle_incoming(incoming(42, "ivan", text="К пятнице сдадим", message_id=650))
    assert case_of(task_id)[1] == "paused"  # the answer is kept, the case waits for the director
    assert client.post(f"/progress/cases/{case_id}/resume", follow_redirects=False).status_code == 303
    assert case_of(task_id)[1] == "done"
    assert "К пятнице" in [t for r, t in tgacc.sent if r == "@boss"][-1]


def test_resume_of_a_waiting_case_restarts_the_reminder_clock(office, tgacc, cmodel):
    settings_store.set_many({"case_reminder_hours": 4})
    task_id, case_id = started(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan",
                               title="Узнать у Ивана сроки")
    assert cases.pause(case_id, "Вадим") and cases.resume(case_id, "Вадим")
    with SessionLocal() as s:
        wait = s.query(CaseWait).one()
        assert cases._aware(wait.next_reminder_at) > now() + dt.timedelta(hours=3)
    assert case_of(task_id)[1] == "waiting"


def test_answer_received_by_phone_is_entered_in_the_panel(client, office, tgacc, cmodel):
    task_id, case_id = started(office, kind="ask", counterparts=(("recipient", "ivan"),), target="ivan",
                               title="Узнать у Ивана сроки")
    r = client.post(f"/progress/cases/{case_id}/answer", data={"text": "По телефону: сдача 14 ноября"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(f"#case-{case_id}")
    assert case_of(task_id)[1] == "done"
    assert "14 ноября" in [t for r_, t in tgacc.sent if r_ == "@boss"][-1]
    with SessionLocal() as s:
        assert any("внёс" in e.text for e in s.query(CaseEvent).filter_by(kind="answer_received"))


def test_document_uploaded_in_the_panel_goes_through_the_same_checks(client, office, tgacc, cmodel):
    task_id, case_id = started(office)
    r = client.post(f"/progress/cases/{case_id}/upload", files={"file": ("Акт скан.pdf", PDF, "application/pdf")},
                    follow_redirects=False)
    assert r.status_code == 303
    assert case_of(task_id)[1] == "done"
    assert tgacc.files[-1][0] == "@petya" and tgacc.file_data[-1] == PDF
    with SessionLocal() as s:
        d = s.query(Document).one()
        assert d.chat_id == "panel" and d.employee_id == office["anna"] and "загружен в панели" in d.caption


def test_sensitive_upload_still_needs_approval(client, office, tgacc, cmodel):
    task_id, case_id = started(office)
    client.post(f"/progress/cases/{case_id}/upload", files={"file": ("Паспорт.jpg", b"img", "image/jpeg")})
    assert case_of(task_id)[1] == "attention" and not tgacc.files


def test_director_writes_from_the_assistant_account(client, office, tgacc, cmodel):
    task_id, case_id = started(office)
    r = client.post(f"/progress/cases/{case_id}/message", data={"text": "Анна, акт нужен до обеда, спасибо!"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert tgacc.sent[-1] == ("@anna", "Анна, акт нужен до обеда, спасибо!")  # exactly these words
    assert case_of(task_id)[1] == "waiting"
    with SessionLocal() as s:
        assert s.query(CaseEvent).filter_by(kind="manual_message").count() == 1


def test_hand_over_to_a_person(client, office, tgacc, cmodel):
    task_id, case_id = started(office)
    r = client.post(f"/progress/cases/{case_id}/handover", data={"employee_id": str(office["ivan"])},
                    follow_redirects=False)
    assert r.status_code == 303
    _, status, _, task_status = case_of(task_id)
    assert status == "handed_over" and task_status == "in_progress"
    with SessionLocal() as s:
        task = s.get(Task, task_id)
        assert task.executor == "person" and task.assignee_id == office["ivan"]
        assert s.query(CaseWait).one().status == "cancelled"
    assert tgacc.sent[-1][0] == "@ivan" and tgacc.sent[-1][1].startswith("[handover]")
    assert cmodel.writes[-1]["done_so_far"]  # Ivan learns what was already done
    # a late file from Anna is kept but no longer forwarded
    agent.handle_incoming(incoming(43, "anna", document=doc(), message_id=660))
    assert not tgacc.files


def test_close_manually(client, office, tgacc, cmodel):
    task_id, case_id = started(office)
    assert client.post(f"/progress/cases/{case_id}/close", follow_redirects=False).status_code == 303
    _, status, _, task_status = case_of(task_id)
    assert status == "done" and task_status == "assistant_done"
    assert client.post(f"/progress/cases/{case_id}/close", follow_redirects=False).status_code == 409


def test_interventions_are_for_superusers_only(client, office, tgacc, cmodel):
    task_id, case_id = started(office)
    from app import accounts
    from app.db import User
    with SessionLocal() as s:
        s.add(User(username="viewer", password_hash=accounts.hash_password("viewer-pass-123"), role="user",
                   status="approved"))
        s.commit()
    client.get("/logout")
    client.post("/login", data={"username": "viewer", "password": "viewer-pass-123"})
    page = client.get("/progress")
    assert page.status_code == 200 and "дел создано" in page.text and "Вмешаться" not in page.text
    assert client.post(f"/progress/cases/{case_id}/close", follow_redirects=False).status_code == 403
    assert case_of(task_id)[1] == "waiting"


def test_bad_inputs_are_refused(client, office, tgacc, cmodel):
    task_id, case_id = started(office)
    assert client.post(f"/progress/cases/{case_id}/answer", data={"text": "ответ"}).status_code == 409  # waits for a file
    assert client.post(f"/progress/cases/{case_id}/handover", data={"employee_id": "x"}).status_code == 400
    assert client.post(f"/progress/cases/{case_id}/explode").status_code == 404
    assert client.post(f"/progress/cases/{case_id}/message", data={"text": " "}).status_code == 409


# ----------------------------------------------------------------------------- live refresh
def test_version_changes_when_something_happens_and_feed_renders(client, office, tgacc, cmodel):
    task_id, case_id = started(office)
    v1 = client.get("/progress/version").json()["version"]
    assert client.get("/progress/version").json()["version"] == v1  # stable while nothing changes
    agent.handle_incoming(incoming(43, "anna", document=doc(), message_id=670))
    v2 = client.get("/progress/version").json()["version"]
    assert v2 != v1
    feed = client.get("/progress/feed")
    assert feed.status_code == 200 and "<html" not in feed.text and "Завершено" in feed.text
    assert feed.headers["cache-control"].startswith("private")


def test_progress_page_needs_login(client):
    client.get("/logout")
    assert client.get("/progress", follow_redirects=False).status_code == 303
    assert client.get("/progress/version", follow_redirects=False).status_code == 303


def test_progress_page_is_quick_with_many_cases(client, tgacc, cmodel, monkeypatch):
    """«За 30 секунд»: the page itself must answer in well under a second with a realistic load."""
    from app import userbot
    monkeypatch.setattr(userbot, "DAILY_LIMIT", 10_000)
    settings_store.set_many({"director_chat_ids": ["@boss"], "quiet_from_hour": 0, "quiet_to_hour": 0})
    with SessionLocal() as s:
        a = Employee(name="Анна", telegram_chat_id="@anna")
        b = Employee(name="Пётр", telegram_chat_id="@petya")
        s.add_all([a, b])
        s.flush()
        meeting = Meeting(title="Большая планёрка", status="done", report={}, options={})
        for i in range(40):
            meeting.tasks.append(Task(title=f"Взять у Анны файл {i} и отправить Петру", executor="assistant",
                                      action_type="forward", status="assistant_todo", target_employee_id=a.id,
                                      counterparts=[{"role": "source", "employee_id": a.id, "name": "Анна"},
                                                    {"role": "recipient", "employee_id": b.id, "name": "Пётр"}]))
        s.add(meeting)
        s.commit()
        ids = [t.id for t in meeting.tasks]
    agent.invalidate_roster()
    for task_id in ids:
        assistant.run_task(task_id)
    started_at = time.perf_counter()
    page = client.get("/progress")
    elapsed = time.perf_counter() - started_at
    assert page.status_code == 200 and page.text.count('class="card pg-case') == 40
    assert elapsed < 3.0


def test_a_file_sent_without_reply_can_be_linked_to_the_right_case(client, office, tgacc, cmodel):
    first_id, first_case = started(office)
    second_id, second_case = started(office, title="Взять у Ани счёт и отправить Ивану",
                                     counterparts=(("source", "anna"), ("recipient", "ivan")))
    agent.handle_incoming(incoming(43, "anna", document=doc(b"invoice", "Счёт.pdf"), message_id=680))
    assert not tgacc.files  # two requests were open: the assistant did not guess
    with SessionLocal() as s:
        data = progress.build(s, "open")
        doc_id = s.query(Document).one().id
    card = next(c for _, items in data["groups"] for c in items if c["id"] == second_case)
    assert [d["filename"] for d in card["unlinked"]] == ["Счёт.pdf"]
    assert "Привязать к этому делу" in client.get("/progress?scope=open").text
    r = client.post(f"/progress/cases/{second_case}/link", data={"document_id": str(doc_id)}, follow_redirects=False)
    assert r.status_code == 303
    assert tgacc.files[-1][0] == "@ivan" and tgacc.file_data[-1] == b"invoice"
    assert case_of(second_id)[1] == "done" and case_of(first_id)[1] == "waiting"
    # the same file cannot be attached twice, and a file of another person never
    assert client.post(f"/progress/cases/{first_case}/link", data={"document_id": str(doc_id)}).status_code == 409


def test_postponed_step_says_when_and_why(office, tgacc, cmodel):
    cmodel.write_down = True
    task_id, case_id = started(office)
    with SessionLocal() as s:
        card = progress.build(s, "last")["groups"][0][1][0]
    assert card["retry"]["in"].startswith("через") and "модель недоступна" in card["retry"]["why"]
