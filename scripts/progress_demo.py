"""Seed a local database with realistic cases for a visual check of the Progress page (fake model, fake Telegram).

    python3 scripts/progress_demo.py /tmp/demo.db
    SYSAI_TESTING=1 DATABASE_URL=sqlite:////tmp/demo.db SECRET_KEY=demo ADMIN_PASSWORD=pw uvicorn app.main:app
"""
import datetime as dt
import json
import os
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sysai_demo.db"
if os.path.exists(path):
    os.remove(path)
os.environ.update(SYSAI_TESTING="1", DATABASE_URL=f"sqlite:///{path}", SECRET_KEY="demo", ADMIN_PASSWORD="pw",
                  DATA_DIR=os.path.dirname(path) or "/tmp", TELEGRAM_BOT_TOKEN="")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

from app import agent, assistant, cases, db, llm, settings_store, userbot  # noqa: E402
from app.db import Employee, Meeting, SessionLocal, Task, now  # noqa: E402
from test_ai_mode import FakeBackend  # noqa: E402

TEXTS = {"ask": "{name}, добрый день! Подскажите, пожалуйста, что с договором поставки?",
         "request": "{name}, добрый день! Пришлите, пожалуйста, документ — передам его коллегам.",
         "remind": "{name}, напомню про мою просьбу, когда будет минутка.",
         "deliver": "Передаю документ по поручению с планёрки.",
         "report": "Готово: документ передан, ответ получен.", "escalate": "Сотрудник не отвечает, нужна ваша помощь.",
         "approval": "План готов, можно начинать?", "doc_approval": "Файл похож на конфиденциальный, переслать?",
         "attention": "Дело остановилось.", "failure": "Не получилось.", "handover": "Передаю вам дело."}


def fake_chat(model, messages, **kwargs):
    system = messages[0]["content"]
    facts = json.loads(messages[1]["content"].split("<data>")[1].split("</data>")[0])
    if "Составь план дела" in system:
        raise llm.LLMError("template plan in the demo")
    if "Ты ведёшь дело" in system:
        name = (facts.get("person") or {}).get("name", "").split(" ")[0]
        return json.dumps({"text": TEXTS[facts["purpose"]].format(name=name)}, ensure_ascii=False)
    return json.dumps({"reply": "Спасибо!", "actions": []}, ensure_ascii=False)


def main():
    db.init_db()
    llm.chat = fake_chat
    fake = FakeBackend()
    fake.users.update({"@anna": 43, "@petya": 44, "@ivan": 42, "@olga": 45, "@boss": 7})
    userbot.backend = fake
    settings_store.set_many({"ai_employee": True, "approver_chat_ids": ["@boss"], "director_chat_ids": ["@boss"],
                             "quiet_from_hour": 0, "quiet_to_hour": 0, "case_autonomy": "auto",
                             "case_reminder_hours": 4, "case_max_reminders": 2, "case_escalate_hours": 24})
    userbot.login_start("1234567", "a" * 32, "+375291234567")
    userbot.login_code("12345")
    with SessionLocal() as s:
        people = {k: Employee(name=n, telegram_chat_id=f"@{k}", position=p) for k, n, p in (
            ("anna", "Анна Смирнова", "Бухгалтер"), ("petya", "Пётр Иванов", "Юрист"),
            ("ivan", "Иван Петров", "Инженер"), ("olga", "Ольга Кузнецова", "Снабжение"))}
        s.add_all(people.values())
        s.flush()
        p = {k: (v.id, v.name) for k, v in people.items()}
        meeting = Meeting(title="Планёрка 22.10", status="done", report={"title": "Планёрка 22.10"}, options={})

        def t(title, kind, cps, target):
            return Task(title=title, executor="assistant", action_type=kind, status="assistant_todo",
                        target_employee_id=p[target][0], source_quote=title,
                        counterparts=[{"role": r, "employee_id": p[k][0], "name": p[k][1]} for r, k in cps])
        meeting.tasks += [
            t("Взять у Ани акт выполненных работ и отправить Пете", "forward", [("source", "anna"), ("recipient", "petya")], "anna"),
            t("Узнать у Ивана статус договора поставки", "ask", [("recipient", "ivan")], "ivan"),
            t("Запросить у Ольги счёт на материалы", "request", [("source", "olga")], "olga"),
            t("Взять у Ани ведомость и передать Ольге", "forward", [("source", "anna"), ("recipient", "olga")], "anna"),
            Task(title="Подготовить смету по объекту", assignee_id=p["petya"][0], status="blocked"),
            Task(title="Сдать отчёт за квартал", assignee_id=p["anna"][0], status="sent",
                 deadline=dt.date.today() - dt.timedelta(days=1)),
            Task(title="Согласовать график поставок", assignee_id=p["olga"][0], status="in_progress"),
        ]
        s.add(meeting)
        s.commit()
        ids = [x.id for x in meeting.tasks if x.executor == "assistant"]
    agent.invalidate_roster()
    for task_id in ids:
        assistant.run_task(task_id)
    pdf = b"%PDF-1.4 demo act"
    # 1: the act came and went to Petya
    agent.handle_incoming({"user_id": 43, "username": "anna", "phone": "", "name": "Анна", "text": "", "message_id": 1,
                           "document": {"fetch": lambda: pdf, "size": len(pdf), "mime": "application/pdf",
                                        "filename": "Акт №17.pdf"}})
    # 2: Ivan is silent: two reminders and the director is told
    cases.run_due(now() + dt.timedelta(hours=5))
    cases.run_due(now() + dt.timedelta(hours=10))
    cases.run_due(now() + dt.timedelta(hours=25))
    # 4: Anna's second file is sensitive, waiting for approval (she answers the right request by reply)
    with SessionLocal() as s:
        from app.db import Case, CaseWait
        wait = s.query(CaseWait).join(Case, Case.id == CaseWait.case_id).filter(Case.task_id == ids[3]).one()
        asked = wait.asked_message_id
    agent.handle_incoming({"user_id": 43, "username": "anna", "phone": "", "name": "Анна", "text": "",
                           "message_id": 2, "reply_to": asked,
                           "document": {"fetch": lambda: b"salary", "size": 6, "mime": "application/vnd.ms-excel",
                                        "filename": "Зарплатная ведомость.xlsx"}})
    print("demo ready:", path)


if __name__ == "__main__":
    main()
