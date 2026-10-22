"""Golden set for task routing: who does a task, the assistant or a person.

Cases are real phrasings from meetings (names replaced) and the director's own examples. Each case gives what a
report model might have proposed (often the typical mistake: the person to be contacted made the assignee) and what
SYSAI must end up with.
"""
from types import SimpleNamespace

import pytest

from app import task_routing


def emp(id, name, aliases=""):
    return SimpleNamespace(id=id, name=name, aliases=aliases,
                           alias_list=lambda a=aliases: [x.strip() for x in a.split(",") if x.strip()])


STAFF = [
    emp(1, "Вадим", "Вадик"),
    emp(2, "Селицкий Андрей Николаевич", "Андрей"),
    emp(3, "Анна Смирнова", "Аня"),
    emp(4, "Пётр Ковалёв", "Петя, Пётр, Петр"),
    emp(5, "Мария Лис", "Маша"),
    emp(6, "Татьяна Руденко", "Таня"),
    emp(7, "Евгения Бойко", "Женя"),
    emp(8, "Русан Дмитрий Михайлович", "Дмитрий"),
    emp(9, "Иван Кот", "Ваня"),
]
A, P = "assistant", "person"

# (quote, title, model executor, model employee_id, expected executor, expected employee_id, expected first contact)
GOLDEN = [
    ("нужно будет написать, э, Вадиму и узнать, э, статус его задач.", "Запросить статус задач у Вадима", P, 1, A, None, 1),
    ("нужно написать Вадиму из отдела программирования, узнать по его задачам, как он справляется", "Уточнить статус задач Вадима", P, None, A, None, 1),
    ("нужно написать, э, Вадиму, уточнить по его задачам, сколько времени ему ещё нужно.", "Написать Вадиму для уточнения статуса задач", P, None, A, None, 1),
    ("Андрей, напиши Ивану по поводу договора.", "Написать Ивану по договору", A, None, P, 2, None),
    ("Иван, ошибки с оплатой нужно закрыть до пятницы.", "Исправить ошибки оплаты на iOS", P, 9, P, 9, None),
    ("Мария, с тебя креативы.", "Подготовить креативы для рекламной кампании", P, 5, P, 5, None),
    ("надо взять у Ани акт и отправить Пете.", "Передать акт от Анны Петру", P, 3, A, None, 3),
    ("Запросите у Евгении договор, он нужен юристу.", "Запросить договор у Евгении", P, 7, A, None, 7),
    ("А сейчас с Татьяной поговорить, может, там брали в аренду.", "Проработать вопрос аренды оборудования", P, 6, A, None, 6),
    ("Кто-то должен поискать замену подрядчику.", "Найти альтернативного подрядчика по дизайну", P, None, P, None, None),
    ("Пока по новостям, которые будут, будет Вадим добавлять.", "Добавлять новости на сайт", P, 1, P, 1, None),
    ("Давайте хорошо, поговорю тогда сегодня с ними, я там чуть позже.", "Запросить демо-версию у поставщика", P, None, P, None, None),
    ("хотелось бы это видеть в виде графика, конечно.", "Подготовить график роста производительности", A, None, A, None, None),
    ("Соответственно, у Селицкого Андрея и у Дмитрия Русана срок согласования будет последняя неделя.", "Откорректировать и согласовать планы-графики работ", P, 2, P, 2, None),
    ("Тогда я предлагаю Вадим, пускай, да, распишет по поводу секретаря.", "Подготовить текстовый материал о внедрении системы", P, 1, P, 1, None),
    ("Поэтому протокол запросите, в любом случае письмом.", "Запросить протокол вскрытия предложений", P, None, P, None, None),
    ("Евгения, узнай у Андрея сроки по графику.", "Узнать сроки по графику", A, None, P, 7, None),
    ("Попроси Петю прислать смету.", "Запросить смету у Петра", P, 4, A, None, 4),
    ("Сообщите Анне, что встреча переносится на четверг.", "Сообщить Анне о переносе встречи", P, 3, A, None, 3),
    ("Напомнить Марии про отчёт в пятницу.", "Напомнить Марии об отчёте", P, 5, A, None, 5),
    ("Петя должен отправить Ане чертежи.", "Отправить чертежи Анне", P, 3, P, 4, None),
    ("Ассистент, сделай сводку по продажам за месяц.", "Сделать сводку по продажам", P, None, A, None, None),
    ("Перешлите Вадиму протокол совещания.", "Переслать Вадиму протокол совещания", P, 1, A, None, 1),
    ("Уточнить у Татьяны, брали ли оборудование в аренду.", "Уточнить у Татьяны про аренду оборудования", P, 6, A, None, 6),
    ("Дмитрий, позвони заказчику.", "Позвонить заказчику", P, 8, P, 8, None),
    ("Позвонить в БелАЗ и согласовать дату.", "Согласовать дату с БелАЗом", P, None, P, None, None),
    ("Нужно узнать у Дмитрия Русана, когда будут графики.", "Узнать у Дмитрия сроки графиков", P, 8, A, None, 8),
    ("Свяжитесь с Евгенией по возврату аванса.", "Связаться с Евгенией по возврату аванса", P, 7, A, None, 7),
    ("Анна подготовит презентацию к понедельнику.", "Подготовить презентацию", P, 3, P, 3, None),
    ("Скинь Пете файл с расчётами, Андрей.", "Отправить Петру файл с расчётами", P, 4, P, 2, None),
    ("Поручить Вадиму обновить сайт.", "Обновить сайт", P, 1, P, 1, None),
    ("Спросить Евгению, подписан ли договор.", "Спросить Евгению о договоре", P, 7, A, None, 7),
    ("Вадим, узнай у Ани, где акт, и перешли Пете.", "Найти акт и переслать Петру", A, None, P, 1, None),
    ("Надо Ивану напомнить про оплату счёта.", "Напомнить Ивану про оплату", P, 9, A, None, 9),
    ("Мария, напомни всем про планёрку.", "Напомнить всем про планёрку", A, None, P, 5, None),
    ("Передать Андрею документы от Евгении.", "Передать Андрею документы от Евгении", P, 2, A, None, 7),
]


def _route(case):
    quote, title, model_executor, model_employee, *_ = case
    return task_routing.route({"title": title, "description": "", "quote": quote, "executor": model_executor,
                               "employee_id": model_employee}, STAFF)


@pytest.mark.parametrize("case", GOLDEN, ids=[c[1] for c in GOLDEN])
def test_golden_case(case):
    quote, title, _, _, executor, employee_id, first_contact = case
    result = _route(case)
    assert result["executor"] == executor, (title, result)
    assert result["employee_id"] == employee_id, (title, result)
    if executor == A:
        assert result["target_employee_id"] == first_contact, (title, result)


def test_golden_accuracy_reaches_sprint_target():
    correct = sum(1 for c in GOLDEN if (lambda r: r["executor"] == c[4] and r["employee_id"] == c[5])(_route(c)))
    assert correct / len(GOLDEN) >= 0.9
    model_correct = sum(1 for c in GOLDEN if c[2] == c[4] and c[3] == c[5])
    # The set is built from the model's real mistakes: without routing it is far below the target.
    assert model_correct / len(GOLDEN) < 0.6


def test_forward_keeps_both_counterparts_and_explains():
    result = _route(GOLDEN[6])
    roles = {c["employee_id"]: c["role"] for c in result["counterparts"]}
    assert roles == {3: "source", 4: "recipient"}
    assert result["action_type"] == "forward"
    assert "ассистент" in result["routing_note"]
    assert task_routing.describe(result["action_type"], result["counterparts"]) == \
        "взять у Анна Смирнова и передать Пётр Ковалёв"


def test_unknown_and_ambiguous_names_are_never_guessed():
    staff = STAFF + [emp(10, "Андрей Котов")]
    result = task_routing.route({"title": "Узнать у Андрея сроки", "quote": "Нужно узнать у Андрея сроки.",
                                 "executor": "person", "employee_id": None}, staff)
    assert result["target_employee_id"] is None
    result = task_routing.route({"title": "Написать Олегу", "quote": "Нужно написать Олегу.", "executor": "person",
                                 "employee_id": None}, STAFF)
    assert result["executor"] == "person" and result["employee_id"] is None
