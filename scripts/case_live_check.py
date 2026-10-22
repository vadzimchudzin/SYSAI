"""Manual check of Sprint 2 with a real model and a fake Telegram account (nothing is sent to real people).

    OPENROUTER_API_KEY=... python3 scripts/case_live_check.py [model]

Runs «взять у Ани акт и отправить Пете» and «узнать у Ивана статус договора» on a temporary SQLite database and prints
every text the assistant wrote, the plan, and the result checks.
"""
import hashlib
import os
import sys
import tempfile

tmp = tempfile.mkdtemp(prefix="sysai_live_")
os.environ.update(SYSAI_TESTING="1", DATA_DIR=tmp, DATABASE_URL=f"sqlite:///{tmp}/live.db", SECRET_KEY="live-check",
                  ADMIN_PASSWORD="pw", TELEGRAM_BOT_TOKEN="")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

from app import agent, assistant, db, settings_store, userbot  # noqa: E402
from app.db import Case, CaseEvent, DocumentTransfer, Employee, Meeting, SessionLocal, Task  # noqa: E402
from test_ai_mode import FakeBackend  # noqa: E402

PDF = b"%PDF-1.4 act of works no. 17"


def main(model: str) -> int:
    db.init_db()
    fake = FakeBackend()
    fake.users.update({"@anna": 43, "@petya": 44, "@ivan": 42, "@boss": 7})
    userbot.backend = fake
    settings_store.set_many({"ai_employee": True, "approver_chat_ids": ["@boss"], "director_chat_ids": ["@boss"],
                             "agent_model": model, "quiet_from_hour": 0, "quiet_to_hour": 0, "case_autonomy": "auto",
                             "company_name": "СисЭйТи"})
    userbot.login_start("1234567", "a" * 32, "+375291234567")
    userbot.login_code("12345")
    with SessionLocal() as s:
        anna = Employee(name="Анна Смирнова", telegram_chat_id="@anna", position="Бухгалтер")
        petya = Employee(name="Пётр Иванов", telegram_chat_id="@petya", position="Юрист")
        ivan = Employee(name="Иван Петров", telegram_chat_id="@ivan", position="Инженер")
        s.add_all([anna, petya, ivan])
        s.flush()
        meeting = Meeting(title="Планёрка 22.10", status="done", report={"title": "Планёрка"}, options={})
        meeting.tasks.append(Task(title="Взять у Ани акт выполненных работ и отправить Пете", executor="assistant",
                                  action_type="forward", status="assistant_todo", target_employee_id=anna.id,
                                  source_quote="Надо взять у Ани акт выполненных работ и отправить Пете на проверку",
                                  counterparts=[{"role": "source", "employee_id": anna.id, "name": anna.name},
                                                {"role": "recipient", "employee_id": petya.id, "name": petya.name}]))
        meeting.tasks.append(Task(title="Узнать у Ивана статус договора поставки", executor="assistant",
                                  action_type="ask", status="assistant_todo", target_employee_id=ivan.id,
                                  source_quote="Напишите Ивану, узнайте, что с договором поставки",
                                  counterparts=[{"role": "recipient", "employee_id": ivan.id, "name": ivan.name}]))
        s.add(meeting)
        s.commit()
        forward_id, ask_id = meeting.tasks[0].id, meeting.tasks[1].id
    agent.invalidate_roster()

    assistant.run_task(forward_id)
    assistant.run_task(ask_id)
    agent.handle_incoming({"user_id": 43, "username": "anna", "phone": "", "name": "Анна", "text": "Вот акт",
                           "message_id": 501, "document": {"fetch": lambda: PDF, "size": len(PDF),
                                                           "mime": "application/pdf", "filename": "Акт №17.pdf"}})
    agent.handle_incoming({"user_id": 42, "username": "ivan", "phone": "", "name": "Иван",
                           "text": "Договор подписали вчера, оригинал отправили курьером, будет в пятницу",
                           "message_id": 601})

    print("=== Сообщения ассистента ===")
    for ref, text in fake.sent:
        print(f"→ {ref}: {text}\n")
    for ref, name, caption in fake.files:
        print(f"→ {ref}: [файл {name}] {caption}\n")
    ok = True
    with SessionLocal() as s:
        for case in s.query(Case).order_by(Case.id):
            print(f"=== Дело «{case.title}»: {case.status}, план: {case.planner} ===")
            for e in s.query(CaseEvent).filter_by(case_id=case.id).order_by(CaseEvent.id):
                print(f"  - {e.kind}: {e.text[:160]}")
            ok &= case.status == "done"
        transfer = s.query(DocumentTransfer).one_or_none()
        ok &= bool(transfer and transfer.to_ref == "@petya" and transfer.sha256 == hashlib.sha256(PDF).hexdigest())
    ok &= bool(fake.file_data) and fake.file_data[0] == PDF
    print("\nИТОГ:", "оба дела завершены, файл ушёл Пете без изменений" if ok else "ПРОВЕРКА НЕ ПРОЙДЕНА")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "google/gemini-2.5-flash"))
