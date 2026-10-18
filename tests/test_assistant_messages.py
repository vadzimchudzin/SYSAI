"""Validation for direct-message tasks carried out by the AI employee."""
import pytest

from app import assistant


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
