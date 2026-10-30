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


def test_department_map_drag_drop_moves_reorders_and_places_employee_at_company_root(client):
    with SessionLocal() as session:
        first = Department(name="Переносимый отдел А")
        second = Department(name="Переносимый отдел Б")
        a = Employee(name="Сотрудник А")
        b = Employee(name="Сотрудник Б")
        c = Employee(name="Сотрудник В")
        root = Employee(name="Сотрудник на уровне компании")
        session.add_all([first, second, a, b, c, root])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=a.id, department_id=first.id, role="Инженер", responsibilities="Проекты"),
            EmployeeDepartment(employee_id=b.id, department_id=second.id, role="Начальник группы", sort_order=0),
            EmployeeDepartment(employee_id=c.id, department_id=second.id, role="Аналитик", sort_order=1),
        ])
        session.commit()
        first_id, second_id, a_id, b_id, c_id, root_id = first.id, second.id, a.id, b.id, c.id, root.id

    response = client.post("/departments/map/move", json={
        "employee_id": a_id, "source_department_id": first_id,
        "target_department_id": second_id, "before_employee_id": b_id,
    })
    assert response.status_code == 200 and response.json() == {"ok": True}
    with SessionLocal() as session:
        assert session.query(EmployeeDepartment).filter_by(employee_id=a_id, department_id=first_id).count() == 0
        moved = session.query(EmployeeDepartment).filter_by(employee_id=a_id, department_id=second_id).one()
        assert moved.role == "Инженер" and moved.responsibilities == "Проекты"
        ordered = session.query(EmployeeDepartment).filter_by(department_id=second_id).order_by(
            EmployeeDepartment.sort_order, EmployeeDepartment.id
        ).all()
        assert [row.employee_id for row in ordered] == [a_id, b_id, c_id]

    response = client.post("/departments/map/move", json={
        "employee_id": c_id, "source_department_id": second_id,
        "target_department_id": second_id, "before_employee_id": a_id,
    })
    assert response.status_code == 200
    with SessionLocal() as session:
        ordered = session.query(EmployeeDepartment).filter_by(department_id=second_id).order_by(
            EmployeeDepartment.sort_order, EmployeeDepartment.id
        ).all()
        assert [row.employee_id for row in ordered] == [c_id, a_id, b_id]

    response = client.post("/departments/map/move", json={
        "employee_id": a_id, "source_department_id": second_id,
        "target_department_id": 0, "before_employee_id": root_id,
    })
    assert response.status_code == 200
    with SessionLocal() as session:
        assert session.query(EmployeeDepartment).filter_by(employee_id=a_id).count() == 0
        ordered_root = session.query(Employee).filter(
            Employee.active.is_(True), ~Employee.department_memberships.any()
        ).order_by(Employee.org_order, Employee.name, Employee.id).all()
        assert [person.id for person in ordered_root][:2] == [a_id, root_id]
    map_page = client.get("/departments?view=map")
    assert map_page.status_code == 200
    assert "Без подразделения" in map_page.text
    assert "Сотрудник А" in map_page.text


def test_department_map_marks_reviewers_red_and_director_with_crown(client, monkeypatch):
    from app import access
    with SessionLocal() as session:
        department = Department(name="Отдел цветовых статусов")
        reviewer = Employee(name="Проверяющий на карте")
        director = Employee(name="Директор на карте", position="Директор")
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
        membership = session.query(EmployeeDepartment).filter_by(
            employee_id=employee_id, department_id=department_id
        ).one()
        assert (department.map_x, department.map_y) == (1430, 870)
        assert (membership.map_x, membership.map_y) == (1890, 1120)

    page = client.get("/departments?view=map")
    assert page.status_code == 200
    assert 'left:1430px;top:870px' in page.text
    assert 'left:1890px;top:1120px' in page.text


def test_department_map_drop_moves_one_membership_and_preserves_other_departments(client):
    with SessionLocal() as session:
        first = Department(name="Свободная доска — исходный отдел")
        second = Department(name="Свободная доска — целевой отдел")
        third = Department(name="Свободная доска — дополнительный отдел")
        employee = Employee(name="Сотрудник с несколькими отделами")
        session.add_all([first, second, third, employee])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=employee.id, department_id=first.id,
                               role="Исходная роль", responsibilities="Исходные обязанности"),
            EmployeeDepartment(employee_id=employee.id, department_id=third.id,
                               role="Дополнительная роль", responsibilities="Дополнительные обязанности"),
        ])
        session.commit()
        first_id, second_id, third_id, employee_id = first.id, second.id, third.id, employee.id

    response = client.post("/departments/map/position", json={
        "node_type": "employee", "employee_id": employee_id, "source_department_id": first_id,
        "target_department_id": second_id, "map_x": 1250, "map_y": 620,
    })
    assert response.status_code == 200
    with SessionLocal() as session:
        assert session.query(EmployeeDepartment).filter_by(employee_id=employee_id, department_id=first_id).count() == 0
        moved = session.query(EmployeeDepartment).filter_by(employee_id=employee_id, department_id=second_id).one()
        retained = session.query(EmployeeDepartment).filter_by(employee_id=employee_id, department_id=third_id).one()
        assert (moved.role, moved.responsibilities) == ("Исходная роль", "Исходные обязанности")
        assert (moved.map_x, moved.map_y) == (1250, 620)
        assert retained.role == "Дополнительная роль"

    # Removing one membership to the company level does not wipe the employee's other department link.
    response = client.post("/departments/map/position", json={
        "node_type": "employee", "employee_id": employee_id, "source_department_id": second_id,
        "target_department_id": 0, "map_x": 380, "map_y": 970,
    })
    assert response.status_code == 200
    with SessionLocal() as session:
        assert session.query(EmployeeDepartment).filter_by(employee_id=employee_id, department_id=second_id).count() == 0
        assert session.query(EmployeeDepartment).filter_by(employee_id=employee_id, department_id=third_id).count() == 1

    # The person is intentionally visible both as a direct company-level report
    # and as a member of the remaining department.
    page = client.get("/departments?view=map")
    assert page.status_code == 200
    assert page.text.count("Сотрудник с несколькими отделами") == 2
    assert 'data-source-department-id="0"' in page.text


def test_department_map_crown_uses_director_position_even_without_director_access_level(client, monkeypatch):
    from app import access
    with SessionLocal() as session:
        department = Department(name="Отдел директора с должностью")
        director = Employee(name="Корнюшко Сергей Михайлович", position="Директор")
        session.add_all([department, director])
        session.flush()
        session.add(EmployeeDepartment(employee_id=director.id, department_id=department.id))
        session.commit()

    monkeypatch.setattr(access, "level_of", lambda employee: "employee")
    response = client.get("/departments?view=map")
    assert response.status_code == 200
    assert "Корнюшко Сергей Михайлович" in response.text
    assert "director-crown" in response.text and "♛" in response.text


def test_department_map_company_root_can_be_moved_and_remembers_position(client):
    from app import settings_store
    with SessionLocal() as session:
        session.add(Department(name="Подразделение для перемещаемого корня"))
        session.commit()

    response = client.post("/departments/map/position", json={
        "node_type": "company", "map_x": 730, "map_y": 540,
    })
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert settings_store.get("org_map_root_x") == 730
    assert settings_store.get("org_map_root_y") == 540

    page = client.get("/departments?view=map")
    assert page.status_code == 200
    assert 'left:730px;top:540px' in page.text
    assert 'data-node-kind="company"' in page.text
