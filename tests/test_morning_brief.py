"""№21: the morning brief — one screen by importance: what is burning, who is silent, what waits for a decision; every
line with its page in the panel; the time and the people who get it are chosen in the settings."""
import datetime as dt

from app import config, digest, settings_store
from app.db import (Case, CaseWait, ConversationEscalation, DeadlineRequest, Employee, Meeting, Note, SessionLocal, Task,
                    now)
from test_ai_mode import FakeBackend, tgacc  # noqa: F401
from test_cases import CaseModel, cmodel, make_task  # noqa: F401
from test_delivery_cases import team  # noqa: F401
from test_digest_and_feed import at


def seed(team):
    today = now().astimezone(digest._tz()).date()
    with SessionLocal() as s:
        viktor = Employee(name="Виктор Иванов", telegram_chat_id="@viktor")
        anna = Employee(name="Анна Петрова", telegram_chat_id="@anna")
        s.add_all([viktor, anna])
        s.flush()
        m = Meeting(title="Планёрка", status="done", options={})
        m.tasks += [Task(title="Купить перфоратор", assignee_id=viktor.id, status="sent", deadline=today - dt.timedelta(days=2)),
                    Task(title="Акт сверки с Криницей", assignee_id=anna.id, status="in_progress", deadline=today),
                    Task(title="Смета на кровлю", assignee_name="Борис", status="blocked"),
                    Task(title="Счёт Монолиту", assignee_id=anna.id, status="sent", deadline=today + dt.timedelta(days=1)),
                    Task(title="Далёкая задача", assignee_id=anna.id, status="sent", deadline=today + dt.timedelta(days=9)),
                    Task(title="Сделанная задача", assignee_id=anna.id, status="done", deadline=today - dt.timedelta(days=5)),
                    Task(title="Свод от ассистента", executor="assistant", status="assistant_todo",
                         deadline=today - dt.timedelta(days=3)),
                    Task(title="Исполнительная на Ленина 12", assignee_id=viktor.id, status="awaiting_deadline"),
                    Task(title="Задача без Telegram", assignee_name="Пётр", status="no_recipient")]
        s.add(m)
        s.add(Meeting(title="Планёрка 10 ноября", status="awaiting_approval", options={}))
        s.flush()
        blocked = next(t for t in m.tasks if t.status == "blocked")
        s.add(Note(task_id=blocked.id, kind="status", source="conversation", text="нет размеров от заказчика"))
        waiting = next(t for t in m.tasks if t.status == "awaiting_deadline")
        s.add(DeadlineRequest(task_id=waiting.id, chat_id="@viktor", asked_at=now() - dt.timedelta(hours=30)))
        s.add(Case(title="Акт от подрядчика", kind="request", status="attention", note="нужно разрешение на пересылку"))
        s.add(ConversationEscalation(meeting_id=m.id, chat_id="@viktor", reason="Можно выехать в 7:00?"))
        s.commit()
        return m.id


def test_the_brief_is_one_screen_by_importance(team, tgacc, cmodel):
    meeting_id = seed(team)
    from app import assistant
    task_id = make_task({"vadim": team["vadim"]}, kind="ask", counterparts=(("recipient", "vadim"),), target="vadim",
                        title="Спросить у Вадима сроки")
    assistant.run_task(task_id)                      # the assistant waits for Вадим's answer since 30 hours
    with SessionLocal() as s:
        for wait in s.query(CaseWait).all():
            wait.started_at = now() - dt.timedelta(hours=30)
        s.commit()
    text = digest.brief()["text"]
    burning, silent, decide = (text.index(x) for x in ("🔥 Горит (4)", "🤐 Молчат (2)", "⚠️ Ждёт решения (4)"))
    assert text.startswith("Утренняя сводка") and burning < silent < decide
    lines = text.splitlines()
    first = lines.index("🔥 Горит (4)")
    assert lines[first + 1] == "• Борис — Смета на кровлю: мешает: нет размеров от заказчика"
    assert lines[first + 2].startswith("• Виктор Иванов — Купить перфоратор: просрочено на 2 дня (срок ")
    assert "• Анна Петрова — Акт сверки с Криницей: срок сегодня" in lines[first + 3]
    assert "Счёт Монолиту: срок завтра" in lines[first + 4]
    assert "Далёкая задача" not in text and "Сделанная задача" not in text and "Свод от ассистента" not in text
    assert "Вадим — нет ответа 30 часов: «Спросить у Вадима сроки»" in text
    assert "Виктор Иванов — не назвал срок, спросили 30 часов назад: «Исполнительная на Ленина 12»" in text
    assert "Протокол «Планёрка 10 ноября» ждёт утверждения\n" in text
    assert "Акт от подрядчика — нужно разрешение на пересылку" in text
    assert "Виктор Иванов спрашивает: «Можно выехать в 7:00?»" in text and "Задачи без получателя: 1" in text
    assert "За сутки закрыто дел: 0. В работе: 2." in text
    assert len(text) < 2000  # read in a minute
    assert meeting_id


def test_every_line_links_to_its_page_when_the_address_is_known(team, tgacc, cmodel, monkeypatch):
    meeting_id = seed(team)
    monkeypatch.setattr(config, "PUBLIC_URL", "https://sysai.example")
    made = digest.brief()
    assert f'<a href="https://sysai.example/meetings/{meeting_id}#task-' in made["html"]
    assert '<a href="https://sysai.example/progress#case-' in made["html"]
    assert "Все дела — в кабинете" in made["html"] and "<a" not in made["text"]
    settings_store.set_many({"digest_frequency": "daily", "digest_last_sent": ""})
    assert digest.send() == 1
    assert [r for r, t in tgacc.html] == ["@omg"] and tgacc.html[0][1].startswith("Утренняя сводка")


def test_nothing_burning_says_so(team, tgacc, cmodel):
    text = digest.brief()["text"]
    assert "Ничего не горит, никто не молчит, решений не ждёт." in text and "🔥" not in text


def test_the_chosen_people_get_it_at_the_chosen_minute(team, tgacc, cmodel, client):
    seed(team)
    with SessionLocal() as s:
        anna = s.query(Employee).filter_by(name="Анна Петрова").one().id
        nobody = Employee(name="Без телеграма")
        s.add(nobody)
        s.commit()
        nobody = nobody.id
    page = client.get("/settings").text
    assert 'name="digest_time" value="09:00"' in page and 'name="digest_recipients"' in page and "Анна Петрова" in page
    assert "Без телеграма" not in page  # nobody to write to
    client.post("/settings", data={"digest_frequency": "daily", "digest_time": "08:30", "present_digest_recipients": "1",
                                   "digest_recipients": [str(anna), str(nobody)]})
    s_ = settings_store.all_settings()
    assert (s_["digest_hour"], s_["digest_minute"], s_["digest_recipients"]) == (8, 30, sorted([anna, nobody]))
    assert digest.recipients() == ["@anna"]
    settings_store.set_many({"digest_last_sent": ""})
    assert not digest.due(at(2026, 10, 26, 8, 29)) and digest.due(at(2026, 10, 26, 8, 30))
    assert client.post("/settings", data={"digest_time": "25:00"}).status_code == 400
    assert digest.send() == 1 and tgacc.sent[-1][0] == "@anna"
    client.post("/settings", data={"present_digest_recipients": "1"})
    assert digest.recipients() == ["@omg"]  # nobody chosen: the reviewers


def test_weekly_stays_the_summary_of_the_cases(team, tgacc, cmodel):
    settings_store.set_many({"digest_frequency": "weekly", "digest_last_sent": ""})
    assert digest.send() == 1
    assert tgacc.sent[-1][1].startswith("Сводка по делам за неделю")
