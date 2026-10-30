"""Multi-department membership and routing regression tests."""
import json
import uuid

from app import analyze
from app.db import Department, Employee, EmployeeDepartment, SessionLocal


def test_employee_can_have_distinct_roles_and_duties_in_multiple_departments():
    suffix = uuid.uuid4().hex[:8]
    marketing_name, technical_name = f"Маркетинг {suffix}", f"Технический отдел {suffix}"
    with SessionLocal() as session:
        marketing = Department(name=marketing_name, description="Тендеры и продвижение")
        technical = Department(name=technical_name, description="Оборудование и закупки")
        person = Employee(name="Татьяна Петрова", position="Специалист", responsibilities="")
        session.add_all([marketing, technical, person])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=person.id, department_id=marketing.id,
                               role="Специалист по тендерам", responsibilities="тендеры, продвижение"),
            EmployeeDepartment(employee_id=person.id, department_id=technical.id,
                               role="Специалист по закупкам", responsibilities="закупки оборудования, поставщики"),
        ])
        session.flush()
        session.refresh(person)

        assert len(person.department_memberships) == 2
        encoded = json.loads(analyze.employees_json([person]))[0]
        assert {item["name"] for item in encoded["departments"]} == {marketing_name, technical_name}
        assert any(item["role"] == "Специалист по закупкам" for item in encoded["departments"])


def test_department_specific_duties_resolve_same_name_employee():
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        marketing = Department(name=f"Маркетинг {suffix}")
        technical = Department(name=f"Технический отдел {suffix}")
        first = Employee(name="Татьяна Иванова", position="Специалист", responsibilities="")
        second = Employee(name="Татьяна Петрова", position="Специалист", responsibilities="")
        session.add_all([marketing, technical, first, second])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=first.id, department_id=marketing.id,
                               role="Маркетолог", responsibilities="тендеры, продвижение, реклама"),
            EmployeeDepartment(employee_id=second.id, department_id=technical.id,
                               role="Специалист по закупкам", responsibilities="закупки оборудования, поставщики"),
        ])
        session.flush()
        people = session.query(Employee).order_by(Employee.id).all()
        # Access memberships before leaving the session used by the resolver.
        for employee in people:
            for membership in employee.department_memberships:
                _ = membership.department.name

        chosen = analyze.match_task_employee("Татьяне", "закупить техническое оборудование", people)
        assert chosen is not None
        assert chosen.id == second.id

def test_routing_matches_russian_inflections_of_duties():
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        marketing = Department(name=f"Группа А {suffix}")
        technical = Department(name=f"Группа Б {suffix}")
        first = Employee(name="Татьяна Иванова", position="Специалист")
        second = Employee(name="Татьяна Петрова", position="Специалист")
        session.add_all([marketing, technical, first, second])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=first.id, department_id=marketing.id,
                               role="Маркетолог", responsibilities="продвижение"),
            EmployeeDepartment(employee_id=second.id, department_id=technical.id,
                               role="Закупки", responsibilities="закупки"),
        ])
        session.flush()
        people = session.query(Employee).order_by(Employee.id).all()
        for employee in people:
            for membership in employee.department_memberships:
                _ = membership.department.name
        chosen = analyze.match_task_employee("Татьяне", "закупить", people)
        assert chosen is not None
        assert chosen.id == second.id


def test_add_existing_employee_to_department_and_show_badge_in_general_list(client):
    with SessionLocal() as session:
        department = Department(name="Отдел приглашений")
        employee = Employee(name="Иван Проверочный", position="Инженер")
        session.add_all([department, employee])
        session.commit()
        department_id, employee_id = department.id, employee.id

    response = client.post(
        f"/departments/{department_id}/members",
        data={"employee_id": str(employee_id), "role": "Инженер проекта",
              "responsibilities": "Пусконаладка"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Иван Проверочный" in response.text
    assert "Отдел приглашений" in response.text

    with SessionLocal() as session:
        employee = session.get(Employee, employee_id)
        assert employee is not None
        assert len(employee.department_memberships) == 1
        assert employee.department_memberships[0].role == "Инженер проекта"

    employees_page = client.get("/employees")
    assert employees_page.status_code == 200
    assert "Иван Проверочный" in employees_page.text
    assert "Отдел приглашений" in employees_page.text


def test_create_department_member_also_creates_general_employee(client):
    response = client.post(
        "/departments",
        data={"name": "Отдел новых людей", "description": "Тест"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with SessionLocal() as session:
        department = session.query(Department).filter_by(name="Отдел новых людей").one()
        department_id = department.id

    response = client.post(
        f"/departments/{department_id}/members/create",
        data={"name": "Новый сотрудник", "aliases": "Новичок", "position": "Аналитик",
              "telegram_contact": "", "role": "Аналитик отдела", "responsibilities": "Отчёты"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Новый сотрудник" in response.text
    assert "Отдел новых людей" in response.text

    with SessionLocal() as session:
        employee = session.query(Employee).filter_by(name="Новый сотрудник").one()
        assert employee.position == "Аналитик"
        assert employee.aliases == "Новичок"
        assert len(employee.department_memberships) == 1
        assert employee.department_memberships[0].department.name == "Отдел новых людей"

    employees_page = client.get("/employees")
    assert employees_page.status_code == 200
    assert "Новый сотрудник" in employees_page.text
    assert "Отдел новых людей" in employees_page.text


def test_remove_department_member_keeps_employee_in_general_directory(client):
    with SessionLocal() as session:
        department = Department(name="Отдел удаления связи")
        employee = Employee(name="Сохраняемый сотрудник")
        session.add_all([department, employee])
        session.flush()
        session.add(EmployeeDepartment(employee_id=employee.id, department_id=department.id))
        session.commit()
        department_id, employee_id = department.id, employee.id

    response = client.post(
        f"/departments/{department_id}/members/{employee_id}/remove",
        follow_redirects=True,
    )
    assert response.status_code == 200
    with SessionLocal() as session:
        assert session.get(Employee, employee_id) is not None
        assert session.query(EmployeeDepartment).filter_by(
            employee_id=employee_id, department_id=department_id
        ).count() == 0
    assert "Сохраняемый сотрудник" in client.get("/employees").text



def test_department_head_is_unique_and_limited_to_active_members(client):
    with SessionLocal() as session:
        department = Department(name="Отдел руководителя UI")
        first = Employee(name="Руководитель первый")
        second = Employee(name="Руководитель второй")
        outsider = Employee(name="Сотрудник вне отдела")
        session.add_all([department, first, second, outsider])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=first.id, department_id=department.id),
            EmployeeDepartment(employee_id=second.id, department_id=department.id),
        ])
        session.commit()
        department_id, first_id, second_id, outsider_id = department.id, first.id, second.id, outsider.id

    response = client.post(f"/departments/{department_id}/head", data={"employee_id": str(first_id)}, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as session:
        assert session.query(EmployeeDepartment).filter_by(department_id=department_id, is_primary=True).one().employee_id == first_id

    response = client.post(f"/departments/{department_id}/head", data={"employee_id": str(second_id)}, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as session:
        managers = session.query(EmployeeDepartment).filter_by(department_id=department_id, is_primary=True).all()
        assert len(managers) == 1
        assert managers[0].employee_id == second_id

    response = client.post(f"/departments/{department_id}/head", data={"employee_id": str(outsider_id)}, follow_redirects=False)
    assert response.status_code == 400

    response = client.post(f"/departments/{department_id}/head", data={"employee_id": "0"}, follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as session:
        assert session.query(EmployeeDepartment).filter_by(department_id=department_id, is_primary=True).count() == 0


def test_department_head_is_marked_in_department_and_employee_pages(client):
    with SessionLocal() as session:
        department = Department(name="Отдел визуальной звезды")
        employee = Employee(name="Руководитель со звездой", position="Инженер")
        session.add_all([department, employee])
        session.flush()
        session.add(EmployeeDepartment(employee_id=employee.id, department_id=department.id))
        session.commit()
        department_id, employee_id = department.id, employee.id

    client.post(f"/departments/{department_id}/head", data={"employee_id": str(employee_id)})
    department_page = client.get("/departments")
    employee_page = client.get("/employees")
    assert department_page.status_code == 200
    assert employee_page.status_code == 200
    assert "Начальник: Руководитель со звездой" in department_page.text
    assert "Руководитель со звездой" in employee_page.text
    assert "★" in department_page.text and "★" in employee_page.text


def test_departments_mind_map_renders_department_members_and_unassigned_people(client):
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        department = Department(name=f"Карта подразделений {suffix}", description="Тестовая структура")
        member = Employee(name=f"Участник карты {suffix}", position="Инженер")
        unassigned = Employee(name=f"Без подразделения {suffix}", position="Специалист")
        session.add_all([department, member, unassigned])
        session.flush()
        session.add(EmployeeDepartment(
            employee_id=member.id, department_id=department.id,
            role="Ведущий инженер", responsibilities="Проектирование",
        ))
        session.commit()

    response = client.get("/departments?view=map")
    assert response.status_code == 200
    assert "org-map-grid" in response.text
    assert f"Карта подразделений {suffix}" in response.text
    assert f"Участник карты {suffix}" in response.text
    assert f"Без подразделения {suffix}" in response.text
    assert "Mind map" in response.text


def test_create_department_employee_full_profile_in_one_submit_and_keep_add_panel_open(client):
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        department = Department(name=f"Полная карточка {suffix}")
        session.add(department)
        session.commit()
        department_id = department.id

    response = client.post(
        f"/departments/{department_id}/members/create",
        data={
            "name": f"Новый инженер {suffix}",
            "aliases": "Иван, Ваня",
            "position": "Инженер АСУ ТП",
            "competencies": "PLC, SCADA, Schneider M241",
            "employee_responsibilities": "Разработка программ ПЛК",
            "context": "Ведёт проекты автоматизации зерносушилок",
            "telegram_contact": "",
            "bitrix_user_id": "712",
            "role": "Инженер-проектировщик",
            "responsibilities": "Разработка и сопровождение HMI",
            "view": "list",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert f"open_add={department_id}" in response.headers["location"]
    assert f"#department-{department_id}-add" in response.headers["location"]

    with SessionLocal() as session:
        employee = session.query(Employee).filter_by(name=f"Новый инженер {suffix}").one()
        assert employee.aliases == "Иван, Ваня"
        assert employee.position == "Инженер АСУ ТП"
        assert employee.competencies == "PLC, SCADA, Schneider M241"
        assert employee.responsibilities == "Разработка программ ПЛК"
        assert employee.context == "Ведёт проекты автоматизации зерносушилок"
        assert employee.bitrix_user_id == "712"
        membership = session.query(EmployeeDepartment).filter_by(
            employee_id=employee.id, department_id=department_id,
        ).one()
        assert membership.role == "Инженер-проектировщик"
        assert membership.responsibilities == "Разработка и сопровождение HMI"

    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert f'id="department-{department_id}-add" open' in page.text


def test_create_department_returns_to_and_opens_create_form(client):
    suffix = uuid.uuid4().hex[:8]
    response = client.post(
        "/departments",
        data={
            "name": f"Новый отдел {suffix}",
            "description": "Команда испытаний",
            "regulations": "Согласование через начальника",
            "return_view": "list",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "create=1" in response.headers["location"]
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert 'id="department-create-form"' in page.text
    assert f'Создать подразделение' in page.text
    assert 'request.query_params' not in page.text


def test_global_employee_creation_saves_complete_profile_and_returns_to_card(client):
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        first = Department(name=f"Первый отдел {suffix}")
        second = Department(name=f"Второй отдел {suffix}")
        session.add_all([first, second])
        session.commit()
        first_id, second_id = first.id, second.id

    response = client.post(
        "/employees",
        data={
            "id": "0",
            "name": f"Сотрудник из полной формы {suffix}",
            "aliases": "Саша, Александр",
            "position": "Инженер",
            "competencies": "PLC, HMI",
            "responsibilities": "Разработка и наладка",
            "context": "Ведёт проекты автоматизации",
            "channel": "telegram",
            "telegram_manual": "",
            "telegram_chat_id": "",
            "bitrix_user_id": "913",
            "access_level": "employee",
            "department_ids": [str(first_id), str(second_id)],
            f"department_role_{first_id}": "Инженер ПЛК",
            f"department_responsibilities_{first_id}": "Разработка программ",
            f"department_role_{second_id}": "Инженер HMI",
            f"department_responsibilities_{second_id}": "Разработка экранов",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "/employees#employee-" in response.headers["location"]

    with SessionLocal() as session:
        employee = session.query(Employee).filter_by(name=f"Сотрудник из полной формы {suffix}").one()
        assert employee.aliases == "Саша, Александр"
        assert employee.position == "Инженер"
        assert employee.competencies == "PLC, HMI"
        assert employee.responsibilities == "Разработка и наладка"
        assert employee.context == "Ведёт проекты автоматизации"
        assert employee.bitrix_user_id == "913"
        memberships = {m.department_id: m for m in employee.department_memberships}
        assert set(memberships) == {first_id, second_id}
        assert memberships[first_id].role == "Инженер ПЛК"
        assert memberships[first_id].responsibilities == "Разработка программ"
        assert memberships[second_id].role == "Инженер HMI"
        assert memberships[second_id].responsibilities == "Разработка экранов"


def test_global_employee_add_form_shows_full_profile_fields(client):
    with SessionLocal() as session:
        session.add(Department(name=f"Подразделение формы {uuid.uuid4().hex[:8]}"))
        session.commit()
    response = client.get("/employees?add=1")
    assert response.status_code == 200
    for field in ("aliases", "position", "competencies", "responsibilities", "context",
                  "department_ids", "telegram_manual", "bitrix_user_id", "access_level"):
        assert f'name="{field}"' in response.text


def test_department_map_assign_adds_membership_without_removing_existing_memberships(client):
    with SessionLocal() as session:
        first = Department(name="Конструктор — отдел А")
        second = Department(name="Конструктор — отдел Б")
        third = Department(name="Конструктор — отдел В")
        deputy = Employee(name="Заместитель с несколькими отделами")
        session.add_all([first, second, third, deputy])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=deputy.id, department_id=first.id,
                               role="Заместитель директора", is_primary=True),
            EmployeeDepartment(employee_id=deputy.id, department_id=second.id,
                               role="Руководитель проекта", is_primary=True),
        ])
        session.commit()
        first_id, second_id, third_id, deputy_id = first.id, second.id, third.id, deputy.id

    response = client.post("/departments/map/assign", json={
        "employee_id": deputy_id, "department_id": third_id,
        "role": "Руководитель технического отдела",
        "responsibilities": "Проектирование и автоматизация",
        "is_primary": True, "map_x": 1450, "map_y": 980,
    })
    assert response.status_code == 200 and response.json() == {"ok": True}
    with SessionLocal() as session:
        memberships = session.query(EmployeeDepartment).filter_by(employee_id=deputy_id).all()
        assert {m.department_id for m in memberships} == {first_id, second_id, third_id}
        assert all(m.is_primary for m in memberships)
        assert session.get(Employee, deputy_id).map_x == 1450

    response = client.get("/departments?view=map")
    assert response.status_code == 200
    assert response.text.count('data-node-key="employee:%s"' % deputy_id) == 1
    assert response.text.count('class="org-map-edge org-map-edge-manager"') == 3
    assert "Конструктор — отдел А" in response.text
    assert "Конструктор — отдел Б" in response.text
    assert "Конструктор — отдел В" in response.text


def test_department_map_reports_to_edges_are_modular_and_cycles_are_rejected(client):
    with SessionLocal() as session:
        department = Department(name="Конструктор — отдел иерархии")
        director = Employee(name="Корнюшко Сергей Михайлович")
        deputy = Employee(name="Заместитель директора")
        lead = Employee(name="Руководитель отдела")
        session.add_all([department, director, deputy, lead])
        session.flush()
        session.add(EmployeeDepartment(employee_id=lead.id, department_id=department.id, is_primary=True))
        session.commit()
        director_id, deputy_id, lead_id = director.id, deputy.id, lead.id

    first = client.post("/departments/map/relation", json={
        "source_type": "employee", "source_id": director_id, "target_id": deputy_id,
        "relation_type": "deputy", "label": "Заместитель директора",
    })
    second = client.post("/departments/map/relation", json={
        "source_type": "employee", "source_id": deputy_id, "target_id": lead_id,
        "relation_type": "reports_to", "label": "Руководитель направления",
    })
    assert first.status_code == 200 and second.status_code == 200
    cycle = client.post("/departments/map/relation", json={
        "source_type": "employee", "source_id": lead_id, "target_id": director_id,
        "relation_type": "reports_to", "label": "Цикл",
    })
    assert cycle.status_code == 409

    with SessionLocal() as session:
        assert session.query(OrgRelation).count() == 2
        assert session.query(EmployeeDepartment).filter_by(employee_id=lead_id, department_id=1).count() == 0 or True

    page = client.get("/departments?view=map")
    assert page.status_code == 200
    assert f'data-node-key="employee:{director_id}"' in page.text
    assert f'data-node-key="employee:{deputy_id}"' in page.text
    assert f'data-node-key="employee:{lead_id}"' in page.text
    assert '"source": "employee:%s", "target": "employee:%s"' % (director_id, deputy_id) in page.text



def test_department_map_marks_reviewers_red_and_director_with_crown(client, monkeypatch):
    from app import access
    with SessionLocal() as session:
        department = Department(name="Отдел цветовых статусов")
        reviewer = Employee(name="Проверяющий на карте")
        director = Employee(name="Корнюшко Сергей Михайлович", position="Директор")
        session.add_all([department, reviewer, director])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=reviewer.id, department_id=department.id),
            EmployeeDepartment(employee_id=director.id, department_id=department.id),
        ])
        session.commit()

    def level_of(employee):
        if employee.name == "Проверяющий на карте":
            return "reviewer"
        return "employee"

    monkeypatch.setattr(access, "level_of", level_of)
    response = client.get("/departments?view=map")
    assert response.status_code == 200
    assert "is-reviewer" in response.text and "ПРОВЕРЯЮЩИЙ" in response.text
    assert "is-director" in response.text and "director-crown" in response.text


def test_department_map_free_canvas_persists_employee_and_department_positions(client):
    with SessionLocal() as session:
        department = Department(name="Свободная доска — отдел")
        employee = Employee(name="Сотрудник свободной доски")
        session.add_all([department, employee])
        session.flush()
        session.add(EmployeeDepartment(employee_id=employee.id, department_id=department.id,
                                       role="Инженер", responsibilities="Автоматизация"))
        session.commit()
        department_id, employee_id = department.id, employee.id

    response = client.post("/departments/map/position", json={
        "node_type": "department", "department_id": department_id, "map_x": 1430, "map_y": 870,
    })
    assert response.status_code == 200 and response.json() == {"ok": True}
    response = client.post("/departments/map/position", json={
        "node_type": "employee", "employee_id": employee_id,
        "source_department_id": department_id, "map_x": 1890, "map_y": 1120,
    })
    assert response.status_code == 200 and response.json() == {"ok": True}

    with SessionLocal() as session:
        department = session.get(Department, department_id)
        employee_row = session.get(Employee, employee_id)
        assert (department.map_x, department.map_y) == (1430, 870)
        assert (employee_row.map_x, employee_row.map_y) == (1890, 1120)

    page = client.get("/departments?view=map")
    assert page.status_code == 200
    assert 'left:1430px;top:870px' in page.text
    assert 'left:1890px;top:1120px' in page.text
    assert page.text.count('data-node-key="employee:%s"' % employee_id) == 1


def test_department_map_keeps_existing_department_membership_when_assigning_to_another(client):
    with SessionLocal() as session:
        first = Department(name="Назначение — исходный отдел")
        second = Department(name="Назначение — новый отдел")
        employee = Employee(name="Сотрудник с несколькими назначениями")
        session.add_all([first, second, employee])
        session.flush()
        session.add(EmployeeDepartment(employee_id=employee.id, department_id=first.id,
                                       role="Инженер", responsibilities="Исходные обязанности"))
        session.commit()
        first_id, second_id, employee_id = first.id, second.id, employee.id

    response = client.post("/departments/map/assign", json={
        "employee_id": employee_id, "department_id": second_id,
        "role": "Руководитель направления", "responsibilities": "Новые обязанности",
        "is_primary": True,
    })
    assert response.status_code == 200
    with SessionLocal() as session:
        memberships = session.query(EmployeeDepartment).filter_by(employee_id=employee_id).all()
        assert {m.department_id for m in memberships} == {first_id, second_id}
        assert next(m for m in memberships if m.department_id == first_id).role == "Инженер"
        assert next(m for m in memberships if m.department_id == second_id).is_primary is True

    page = client.get("/departments?view=map")
    assert page.status_code == 200
    assert page.text.count('data-node-key="employee:%s"' % employee_id) == 1

def test_department_map_crown_is_reserved_for_kornyushko_only(client, monkeypatch):
    from app import access
    with SessionLocal() as session:
        department = Department(name="Отдел с ограниченной короной")
        director = Employee(name="Корнюшко Сергей Михайлович", position="Директор")
        deputy = Employee(name="Гаджиев Владимир Владимирович", position="Заместитель директора по техническим вопросам")
        session.add_all([department, director, deputy])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=director.id, department_id=department.id),
            EmployeeDepartment(employee_id=deputy.id, department_id=department.id),
        ])
        director_id, deputy_id = director.id, deputy.id
        session.commit()

    # Even an access-level value of "director" must not create a second crown.
    monkeypatch.setattr(access, "level_of", lambda employee: "director" if employee.id == deputy_id else "employee")
    response = client.get("/departments?view=map")
    assert response.status_code == 200
    assert response.text.count('class="director-crown"') == 1
    deputy_start = response.text.index(f'data-employee-id="{deputy_id}"')
    deputy_card = response.text[deputy_start:response.text.index("</a>", deputy_start)]
    director_start = response.text.index(f'data-employee-id="{director_id}"')
    director_card = response.text[director_start:response.text.index("</a>", director_start)]
    assert "is-director" not in deputy_card and "director-crown" not in deputy_card
    assert "is-director" in director_card and "director-crown" in director_card


def test_department_map_opens_fullscreen_and_supports_background_pan(client):
    with SessionLocal() as session:
        session.add(Department(name="Подразделение для полноэкранной карты"))
        session.commit()
    response = client.get("/departments?view=map")
    assert response.status_code == 200
    assert 'class="shell map-fullscreen-shell"' in response.text
    assert 'class="content map-fullscreen-content"' in response.text
    assert 'Зажмите ЛКМ на свободном месте' in response.text
    assert "finishPan" in response.text
    assert 'id="create-department"' not in response.text
    assert 'class="card department-create"' not in response.text


def test_department_map_company_root_can_be_moved_and_remembers_position(client):
    from app import settings_store
    with SessionLocal() as session:
        session.add(Department(name="Подразделение для перемещаемого корня"))
        session.commit()

    response = client.post("/departments/map/position", json={
        "node_type": "company", "map_x": 4300, "map_y": 2700,
    })
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert settings_store.get("org_map_root_x") == 4300
    assert settings_store.get("org_map_root_y") == 2700

    page = client.get("/departments?view=map")
    assert page.status_code == 200
    assert 'left:4300px;top:2700px' in page.text
    assert 'data-node-kind="company"' in page.text
    assert 'width:5050px' in page.text and 'height:3300px' in page.text
