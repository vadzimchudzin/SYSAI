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
