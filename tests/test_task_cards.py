"""The meeting page's task cards: fields only while the report can be edited, ▶ only with a kept recording,
the case buttons keep working after sending, and what saving the cards does."""
import datetime as dt
from html.parser import HTMLParser

from app.db import Case, Employee, Meeting, SessionLocal, Task
from app.main import stamp_seconds

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
TRANSCRIPT = [{"speaker": "Анна", "start": 0, "end": 30, "text": "Начнём"},
              {"speaker": "Борис", "start": 30, "end": 4000, "text": "Нужна ведомость по объекту"}]


class Page(HTMLParser):
    """Every element with its ancestors, like the browser builds the tree (the template has no unclosed tags)."""
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.stack, self.nodes = [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = {"tag": tag, "attrs": dict(attrs), "parents": list(self.stack), "text": ""}
        self.nodes.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.nodes.append({"tag": tag, "attrs": dict(attrs), "parents": list(self.stack), "text": ""})

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        for node in self.stack:
            node["text"] += data

    def tasks(self, *tags):
        """Elements inside the task section."""
        return [n for n in self.nodes if n["tag"] in tags and any(p["attrs"].get("id") == "meeting-tasks" for p in n["parents"])]

    def by_id(self, element_id):
        return next(n for n in self.nodes if n["attrs"].get("id") == element_id)


def disabled(node):
    return any(p["tag"] == "fieldset" and "disabled" in p["attrs"] for p in node["parents"])


def forms(node):
    return [p["attrs"].get("id") or p["attrs"].get("action") for p in node["parents"] if p["tag"] == "form"]


def card(node):
    return next((p["attrs"].get("id") for p in node["parents"] if p["tag"] == "article"), None)


def make_meeting(status="awaiting_approval", **fields):
    with SessionLocal() as s:
        anna = Employee(name="Анна Смирнова", position="Экономист", telegram_chat_id="111")
        boris = Employee(name="Борис Ким", position="Начальник участка", telegram_chat_id="222")
        s.add_all([anna, boris])
        s.flush()
        m = Meeting(title="Планёрка", status=status, source="web", meeting_date=dt.date(2026, 10, 29), duration_sec=4000,
                    transcript=TRANSCRIPT, report={"title": "Планёрка", "summary": "Итог"}, options={}, **fields)
        s.add(m)
        s.flush()
        tasks = [
            Task(meeting_id=m.id, title="Подготовить ведомость", description="По объекту «Криница»", assignee_id=boris.id,
                 priority="high", deadline=dt.date(2026, 11, 3), deadline_source="stated", deadline_quote="во вторник",
                 source_quote="Нужна ведомость по объекту", source_time="1:02:03"),
            Task(meeting_id=m.id, title="Уточнить остаток по договору", assignee_id=anna.id, priority="low",
                 source_quote="Посмотрю, сколько осталось", source_time="07:45"),
            Task(meeting_id=m.id, title="Позвонить заказчику", priority="medium", assignee_name="Коляга",
                 source_quote="Надо бы позвонить", source_time="около минуты"),
        ]
        s.add_all(tasks)
        s.commit()
        return m.id, [t.id for t in tasks], anna.id, boris.id


def test_stamp_seconds_reads_the_quote_time():
    assert stamp_seconds("19:39") == 1179
    assert stamp_seconds(" 07:45 ") == 465
    assert stamp_seconds("1:02:03") == 3723
    assert stamp_seconds("0:05") == 5
    for bad in ("", None, "около минуты", "19:3", "12"):
        assert stamp_seconds(bad) is None


def test_draft_tasks_are_editable_cards_inside_the_report_form(client):
    mid, (t1, t2, t3), anna, boris = make_meeting()
    response = client.get(f"/meetings/{mid}")
    assert response.status_code == 200
    page = Page(response.text)
    assert [(n["attrs"]["id"], n["attrs"]["class"]) for n in page.tasks("article")] == [
        (f"task-{t1}", "tk p-high"), (f"task-{t2}", "tk p-low"), (f"task-{t3}", "tk p-medium")]  # the stripe of the priority
    fields = {n["attrs"].get("name"): n for n in page.tasks("input", "select", "textarea")}
    for tid in (t1, t2, t3):
        assert {f"title_{tid}", f"desc_{tid}", f"assignee_{tid}", f"deadline_{tid}", f"priority_{tid}", f"del_{tid}"} <= set(fields)
    assert {"new_title", "new_desc", "new_assignee", "new_deadline", "new_priority"} <= set(fields)
    for node in fields.values():  # every field is sent with the report and none of them is switched off
        assert forms(node) == ["report-editor"] and not disabled(node)
    title = fields[f"title_{t1}"]
    assert title["tag"] == "textarea" and title["attrs"]["maxlength"] == "500" and "data-tk-title" in title["attrs"]
    assert title["text"] == "Подготовить ведомость" and fields[f"desc_{t1}"]["text"] == "По объекту «Криница»"
    assert fields[f"deadline_{t1}"]["attrs"]["value"] == "2026-11-03" and fields[f"del_{t1}"]["attrs"]["type"] == "checkbox"
    chosen = [n for n in page.nodes if n["tag"] == "option" and "selected" in n["attrs"] and card(n) == f"task-{t1}"]
    assert [n["text"] for n in chosen] == ["Борис Ким · Начальник участка", "Высокий"]
    note = next(n for n in page.nodes if "data-tk-deadline-note" in n["attrs"] and card(n) == f"task-{t1}")
    assert " ".join(note["text"].split()) == "вт · назван на встрече: «во вторник»"
    assert "Прозвучало «Коляга» — нет в справочнике" in page.by_id(f"task-{t3}")["text"]
    assert "отправлена" not in page.by_id("meeting-tasks")["text"]  # a draft has no delivery status yet
    assert not page.tasks("button") or all("data-vd-play-at" not in b["attrs"] for b in page.tasks("button"))  # no recording
    save = [n for n in page.nodes if n["tag"] == "button" and n["text"].strip() == "Сохранить"]
    assert len(save) == 1 and not disabled(save[0]) and forms(save[0]) == ["report-editor"]
    assert '/static/meeting-tasks.js' in response.text and client.get("/static/meeting-tasks.js").status_code == 200


def test_play_buttons_appear_only_with_a_kept_recording(client):
    until = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mid, (t1, t2, t3), _, _ = make_meeting(status="done", recording_status="stored", recording={"size": 1000, "expires_at": until})
    page = Page(client.get(f"/meetings/{mid}").text)
    play = [n for n in page.tasks("button") if "tk-play" in n["attrs"].get("class", "")]
    assert [(card(b), b["attrs"]["data-vd-play-at"], b["attrs"]["data-vd-play-key"], b["attrs"]["type"]) for b in play] == [
        (f"task-{t1}", "3723", f"task-{t1}", "button"), (f"task-{t2}", "465", f"task-{t2}", "button")]  # «около минуты» is no time
    assert not any(disabled(b) for b in play)
    with SessionLocal() as s:
        s.get(Meeting, mid).recording_status = "deleted"
        s.commit()
    assert "data-vd-play-at" not in client.get(f"/meetings/{mid}").text


def test_sent_tasks_show_text_statuses_and_working_case_buttons(client):
    mid, (t1, t2, t3), anna, boris = make_meeting(status="done")
    with SessionLocal() as s:
        s.get(Task, t1).status = "sent"
        assistant = s.get(Task, t2)
        assistant.executor, assistant.status, assistant.target_employee_id = "assistant", "assistant_message_uncertain", anna
        case = Case(task_id=t3, meeting_id=mid, title="Позвонить заказчику", status="attention", note="Исполнитель не ответил за 2 дня.")
        s.add(case)
        s.commit()
        case_id = case.id
    page = Page(client.get(f"/meetings/{mid}").text)
    assert not page.tasks("input", "select", "textarea")  # nothing to edit after sending: plain text
    assert "Подготовить ведомость" in page.by_id(f"task-{t1}")["text"] and "03.11.2026" in page.by_id(f"task-{t1}")["text"]
    pills = {card(n): n["text"] for n in page.tasks("span") if n["attrs"].get("class", "").startswith("pill ") and
             any("tk-status" in p["attrs"].get("class", "") for p in n["parents"])}
    assert pills[f"task-{t1}"] == "отправлена" and pills[f"task-{t2}"] == "проверить доставку"
    buttons = {b["text"].strip(): b for b in page.tasks("button")}
    assert buttons["Повторить шаг"]["attrs"]["formaction"] == f"/cases/{case_id}/retry"
    assert buttons["Отменить"]["attrs"]["formaction"] == f"/cases/{case_id}/cancel"
    assert buttons["Повторить после проверки"]["attrs"]["formaction"] == f"/meetings/{mid}/tasks/{t2}/retry-assistant-message"
    for b in buttons.values():  # these were dead after sending: inside the disabled fieldset or a nested form
        assert not disabled(b) and forms(b) == ["report-editor"]
    save = next(n for n in page.nodes if n["tag"] == "button" and n["text"].strip() == "Сохранить")
    assert disabled(save)
    response = client.post(buttons["Отменить"]["attrs"]["formaction"], follow_redirects=False)  # what the button sends
    assert response.status_code == 303 and response.headers["location"] == f"/meetings/{mid}"
    with SessionLocal() as s:
        assert s.get(Case, case_id).status == "cancelled"


def test_saving_cards_cleans_titles_keeps_descriptions_and_deletes(client):
    mid, (t1, t2, t3), anna, boris = make_meeting()
    response = client.post(f"/meetings/{mid}/save", data={
        f"title_{t1}": "  Согласовать\r\nсмету   с заказчиком ", f"desc_{t1}": "Строка 1\nСтрока 2",
        f"title_{t2}": "   ",  # an emptied title keeps the old one
        f"title_{t3}": "Позвонить заказчику", f"del_{t3}": "on",  # the browser sends the whole card
        "new_title": "Новая\nзадача", "new_desc": "  Подробности  ", "new_assignee": str(anna),
        "new_deadline": "", "new_priority": "high",
    }, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/meetings/{mid}?saved=1"
    with SessionLocal() as s:
        first = s.get(Task, t1)
        assert first.title == "Согласовать смету с заказчиком" and first.description == "Строка 1\nСтрока 2"
        assert s.get(Task, t2).title == "Уточнить остаток по договору"
        assert s.get(Task, t3) is None
        new = s.query(Task).filter_by(meeting_id=mid, title="Новая задача").one()
        assert (new.description, new.assignee_id, new.priority, new.deadline_source) == ("Подробности", anna, "high", "none")
    client.post(f"/meetings/{mid}/save", data={f"title_{t1}": "Я" * 600, "new_title": "Ю" * 600})
    with SessionLocal() as s:
        assert s.get(Task, t1).title == "Я" * 500
        assert s.query(Task).filter_by(meeting_id=mid, title="Ю" * 500).count() == 1
