"""Task owner resolution uses explicit employee duties, but never guesses on a tie."""
from types import SimpleNamespace

from app import analyze


def person(i, name, position, responsibilities, telegram_chat_id=""):
    aliases = ""
    return SimpleNamespace(
        id=i, name=name, aliases=aliases, position=position, responsibilities=responsibilities,
        telegram_chat_id=telegram_chat_id, alias_list=lambda: [],
    )


def test_same_first_name_uses_duties_to_route_equipment_purchase():
    marketing = person(1, "Татьяна Иванова", "Маркетолог", "тендеры, продвижение, рекламные кампании")
    technical = person(2, "Татьяна Петрова", "Технический специалист", "закупки оборудования, подбор поставщиков")
    assert analyze.match_task_employee("Татьяне", "закупить промышленное оборудование для сушилки", [marketing, technical]).id == 2


def test_same_first_name_without_relevant_duties_remains_ambiguous():
    a = person(1, "Татьяна Иванова", "Маркетолог", "тендеры, продвижение")
    b = person(2, "Татьяна Петрова", "Технический специалист", "чертежи, обслуживание")
    assert analyze.match_task_employee("Татьяне", "уточнить детали", [a, b]) is None


def test_unique_name_still_resolves_without_responsibilities():
    person_one = person(7, "Андрей Орлов", "Инженер", "")
    assert analyze.match_task_employee("Андрею", "подготовить чертёж", [person_one]).id == 7


def test_context_match_overrides_conflicting_model_id():
    marketing = person(1, "Татьяна Иванова", "Маркетолог", "тендеры, продвижение, рекламные кампании")
    technical = person(2, "Татьяна Петрова", "Технический специалист", "закупки оборудования, подбор поставщиков")
    report = {
        "title": "Закупка",
        "summary": "Поручение по оборудованию",
        "participants": [],
        "topics": [],
        "decisions": [],
        "tasks": [{
            "title": "Закупить промышленное оборудование",
            "description": "Подобрать поставщика оборудования",
            "employee_id": 1,
            "assignee_name": "Татьяне",
            "executor": "person",
            "deadline": None,
            "deadline_quote": "",
            "priority": "medium",
            "time": "01:00",
            "quote": "Татьяне нужно закупить оборудование",
        }],
        "open_questions": [],
        "risks": [],
        "notes": [],
        "next_meeting": None,
    }
    normalized = analyze.normalize(report, [marketing, technical])
    assert normalized["tasks"][0]["employee_id"] == 2


def test_ambiguous_name_is_not_assigned_even_if_model_guesses_an_id():
    a = person(1, "Татьяна Иванова", "Маркетолог", "тендеры, продвижение")
    b = person(2, "Татьяна Петрова", "Технический специалист", "чертежи, обслуживание")
    report = {
        "tasks": [{
            "title": "Уточнить детали",
            "description": "",
            "employee_id": 1,
            "assignee_name": "Татьяне",
            "executor": "person",
            "deadline": None,
            "deadline_quote": "",
            "priority": "medium",
            "time": "01:00",
            "quote": "Татьяне уточнить детали",
        }]
    }
    normalized = analyze.normalize(report, [a, b])
    assert normalized["tasks"][0]["employee_id"] is None

def test_assistant_message_target_is_resolved_separately_from_task_owner():
    marketing = person(1, "Татьяна Иванова", "Маркетолог", "тендеры, продвижение", "@tanya_marketing")
    technical = person(2, "Татьяна Петрова", "Технический специалист", "закупки оборудования, подбор поставщиков",
                       "@tanya_technical")
    report = {
        "tasks": [{
            "title": "Написать Татьяне и уточнить закупку оборудования",
            "description": "Уточнить поставщика и сроки поставки",
            "employee_id": None,
            "assignee_name": "ассистент",
            "executor": "assistant",
            "target_employee_id": 1,
            "target_employee_name": "Татьяне",
            "deadline": None,
            "deadline_quote": "",
            "priority": "medium",
            "time": "01:00",
            "quote": "Нужно написать Татьяне и уточнить закупку оборудования",
        }]
    }
    normalized = analyze.normalize(report, [marketing, technical])
    task = normalized["tasks"][0]
    assert task["executor"] == "assistant"
    assert task["employee_id"] is None
    assert task["target_employee_id"] == 2
    assert task["target_employee_name"] == "Татьяне"


def test_assistant_message_target_without_telegram_contact_is_not_routable():
    person_without_contact = person(9, "Андрей Орлов", "Инженер", "чертежи")
    normalized = analyze.normalize({
        "tasks": [{
            "title": "Написать Андрею",
            "description": "Уточнить детали",
            "employee_id": None,
            "assignee_name": "ассистент",
            "executor": "assistant",
            "target_employee_id": 9,
            "target_employee_name": "Андрею",
            "deadline": None,
            "deadline_quote": "",
            "priority": "medium",
            "time": "00:10",
            "quote": "Написать Андрею и уточнить детали",
        }]
    }, [person_without_contact])
    assert normalized["tasks"][0]["target_employee_id"] is None


def test_assistant_target_id_alone_is_not_trusted_without_a_resolved_name():
    person_with_contact = person(11, "Андрей Орлов", "Инженер", "чертежи", "@andrey")
    normalized = analyze.normalize({
        "tasks": [{
            "title": "Написать сотруднику",
            "description": "Уточнить детали",
            "employee_id": None,
            "assignee_name": "ассистент",
            "executor": "assistant",
            "target_employee_id": 11,
            "target_employee_name": None,
            "deadline": None,
            "deadline_quote": "",
            "priority": "medium",
            "time": "00:10",
            "quote": "Нужно написать сотруднику и уточнить детали",
        }]
    }, [person_with_contact])
    task = normalized["tasks"][0]
    assert task["executor"] == "assistant"
    assert task["target_employee_id"] is None
