"""Validation for direct-message tasks carried out by the AI employee."""
import pytest

from app import assistant, config, telegram
from app.db import Employee, Meeting, SessionLocal, Task


def test_message_action_requires_a_reachable_target_and_text():
    facts = {"target_employee": {"id": 4, "name": "Андрей", "reachable": True}, "_allowed": set()}
    data = {"kind": "message", "message": "Отправил сообщение.", "message_to_send": "Уточни, пожалуйста, срок поставки."}
    validated = assistant._validated(data, facts)
    assert validated["message_to_send"] == "Уточни, пожалуйста, срок поставки."


def test_message_action_is_rejected_without_a_reachable_target():
    data = {"kind": "message", "message": "Не могу отправить.", "message_to_send": "Уточни срок."}
    with pytest.raises(assistant.AssistantError, match="адресата"):
        assistant._validated(data, {"target_employee": None, "_allowed": set()})


def test_message_action_is_rejected_when_message_text_is_empty():
    data = {"kind": "message", "message": "Не могу отправить.", "message_to_send": " "}
    facts = {"target_employee": {"id": 4, "name": "Андрей", "reachable": True}, "_allowed": set()}
    with pytest.raises(assistant.AssistantError, match="текст сообщения"):
        assistant._validated(data, facts)

def create_message_task():
    with SessionLocal() as session:
        target = Employee(name="Андрей Орлов", telegram_chat_id="12345", position="Инженер")
        meeting = Meeting(title="Проверка оборудования", status="done", report={"title": "Проверка"},
                          transcript=[], options={})
        task = Task(title="Написать Андрею и уточнить срок поставки", description="Уточнить срок поставки",
                    executor="assistant", assignee_name="ассистент", target_employee=target,
                    target_employee_name="Андрею", status="assistant_todo")
        meeting.tasks = [task]
        session.add(meeting)
        session.commit()
        return task.id


def test_assistant_sends_message_to_target_employee(monkeypatch):
    monkeypatch.setattr(assistant.userbot, "quiet_until", lambda: None)
    monkeypatch.setattr(assistant.userbot, "ai_mode", lambda: False)
    original_get = assistant.settings_store.get
    monkeypatch.setattr(assistant.settings_store, "get",
                        lambda key, default=None: [] if key == "approver_chat_ids" else original_get(key, default))
    task_id = create_message_task()
    sent = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(assistant, "_ask", lambda facts, feedback="": {
        "kind": "message", "title": "Уточнение", "message": "Сообщение отправлено.",
        "message_to_send": "Подскажите, пожалуйста, ожидаемый срок поставки."
    })
    monkeypatch.setattr(telegram, "send_message", lambda chat_id, text: sent.append((chat_id, text)) or {
        "message_id": 77, "chat": {"id": int(chat_id)}
    })

    assistant.run_task(task_id)

    with SessionLocal() as session:
        task = session.get(Task, task_id)
        assert task.status == "assistant_done"
    assert sent == [("12345", "Подскажите, пожалуйста, ожидаемый срок поставки.")]


def test_uncertain_message_delivery_is_not_automatically_retried(monkeypatch):
    monkeypatch.setattr(assistant.userbot, "quiet_until", lambda: None)
    monkeypatch.setattr(assistant.userbot, "ai_mode", lambda: False)
    original_get = assistant.settings_store.get
    monkeypatch.setattr(assistant.settings_store, "get",
                        lambda key, default=None: [] if key == "approver_chat_ids" else original_get(key, default))
    task_id = create_message_task()
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(assistant, "_ask", lambda facts, feedback="": {
        "kind": "message", "title": "Уточнение", "message": "Пробую отправить.",
        "message_to_send": "Подскажите срок поставки."
    })
    def timeout(*args, **kwargs):
        raise TimeoutError("connection lost after request")
    monkeypatch.setattr(telegram, "send_message", timeout)

    assistant.run_task(task_id)

    with SessionLocal() as session:
        task = session.get(Task, task_id)
        assert task.status == "assistant_message_uncertain"
        assert task.status != "assistant_todo"