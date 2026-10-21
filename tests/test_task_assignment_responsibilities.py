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


def test_unnamed_task_is_assigned_by_unique_relevant_responsibilities():
    marketing = person(1, "Татьяна Иванова", "Маркетолог", "тендеры, продвижение, рекламные кампании")
    technical = person(2, "Татьяна Петрова", "Технический специалист", "закупки оборудования, подбор поставщиков")
    report = {
        "tasks": [{
            "title": "Закупить промышленное оборудование для сушилки",
            "description": "Подобрать поставщика оборудования",
            "employee_id": None,
            "assignee_name": None,
            "executor": "person",
            "deadline": None,
            "deadline_quote": "",
            "priority": "medium",
            "time": "01:00",
            "quote": "Нужно закупить промышленное оборудование и подобрать поставщика",
        }]
    }
    normalized = analyze.normalize(report, [marketing, technical])
    assert normalized["tasks"][0]["employee_id"] == 2
    assert normalized["tasks"][0]["assignee_name"] == "Татьяна Петрова"


def test_unnamed_task_remains_unassigned_when_relevance_is_tied():
    a = person(1, "Андрей Орлов", "Инженер", "закупки оборудования, подбор поставщиков")
    b = person(2, "Иван Петров", "Технический специалист", "закупки оборудования, подбор поставщиков")
    report = {
        "tasks": [{
            "title": "Закупить оборудование",
            "description": "Подобрать поставщика",
            "employee_id": None,
            "assignee_name": "",
            "executor": "person",
            "deadline": None,
            "deadline_quote": "",
            "priority": "medium",
            "time": "01:00",
            "quote": "Нужно закупить оборудование и подобрать поставщика",
        }]
    }
    normalized = analyze.normalize(report, [a, b])
    assert normalized["tasks"][0]["employee_id"] is None
    assert normalized["tasks"][0]["assignee_name"] == ""


def test_ai_employee_takes_light_meeting_summary_only_when_enabled():
    report = {
        "notes": [],
        "tasks": [{
            "title": "Подготовить краткую сводку по итогам совещания",
            "description": "Собрать основные решения и поручения из обсуждения",
            "employee_id": None,
            "assignee_name": "",
            "executor": "person",
            "target_employee_id": None,
            "target_employee_name": "",
        }],
    }
    result = analyze.route_assistant_tasks(report, [], assistant_enabled=True)
    task = result["tasks"][0]
    assert task["executor"] == "assistant"
    assert task["employee_id"] is None
    assert task["assignee_name"] == "ассистент"


def test_ai_employee_message_resolves_one_reachable_recipient():
    person_with_contact = person(9, "Андрей Орлов", "Инженер", "чертежи", "@andrey")
    report = {
        "notes": [],
        "tasks": [{
            "title": "Написать Андрею и уточнить срок поставки",
            "description": "Спросить, когда будет оборудование",
            "quote": "Нужно написать Андрею и уточнить срок поставки",
            "employee_id": None,
            "assignee_name": "",
            "executor": "person",
            "target_employee_id": None,
            "target_employee_name": "",
        }],
    }
    result = analyze.route_assistant_tasks(report, [person_with_contact], assistant_enabled=True)
    task = result["tasks"][0]
    assert task["executor"] == "assistant"
    assert task["employee_id"] is None
    assert task["target_employee_id"] == 9
    assert task["target_employee_name"] == "Андрей Орлов"


def test_ai_employee_does_not_take_work_when_ai_mode_is_disabled():
    report = {
        "notes": [],
        "tasks": [{
            "title": "Подготовить краткую сводку по итогам совещания",
            "description": "Собрать основные решения из обсуждения",
            "employee_id": None,
            "assignee_name": "",
            "executor": "person",
            "target_employee_id": None,
            "target_employee_name": "",
        }],
    }
    result = analyze.route_assistant_tasks(report, [], assistant_enabled=False)
    assert result["tasks"][0]["executor"] == "person"
    assert result["tasks"][0]["employee_id"] is None



def test_ai_employee_does_not_treat_technical_writing_as_a_message():
    technical = person(22, "Андрей Орлов", "Инженер-программист", "программирование PLC, разработка управляющих программ", "@andrey")
    report = {
        "notes": [],
        "tasks": [{
            "title": "Написать программу для PLC контроллера",
            "description": "Реализовать логику управления сушилкой",
            "quote": "Нужно написать программу для PLC контроллера",
            "employee_id": None,
            "assignee_name": "",
            "executor": "person",
            "target_employee_id": None,
            "target_employee_name": "",
        }],
    }
    result = analyze.route_assistant_tasks(report, [technical], assistant_enabled=True)
    assert result["tasks"][0]["executor"] == "person"
    assert result["tasks"][0]["target_employee_id"] is None


def test_placeholder_assignee_does_not_block_relevance_assignment():
    marketing = person(31, "Татьяна Иванова", "Маркетолог", "тендеры, продвижение, рекламные кампании")
    technical = person(32, "Татьяна Петрова", "Технический специалист", "закупки оборудования, подбор поставщиков")
    report = {
        "tasks": [{
            "title": "Закупить промышленное оборудование",
            "description": "Подобрать поставщика оборудования",
            "employee_id": None,
            "assignee_name": "не назначено",
            "executor": "person",
            "deadline": None,
            "deadline_quote": "",
            "priority": "medium",
            "time": "01:00",
            "quote": "Нужно закупить промышленное оборудование",
        }]
    }
    normalized = analyze.normalize(report, [marketing, technical])
    assert normalized["tasks"][0]["employee_id"] == 32
    assert normalized["tasks"][0]["assignee_name"] == "Татьяна Петрова"
