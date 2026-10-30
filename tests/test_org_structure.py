import uuid

from app.db import Department, Employee, EmployeeDepartment, SessionLocal


def test_organization_canvas_groups_all_departments_under_one_manager(client):
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        first = Department(name=f"Технический отдел {suffix}")
        second = Department(name=f"ОПО {suffix}")
        manager = Employee(name=f"Заместитель {suffix}", position="Заместитель директора")
        session.add_all([first, second, manager])
        session.flush()
        session.add_all([
            EmployeeDepartment(employee_id=manager.id, department_id=first.id,
                               role="Руководитель", is_primary=True),
            EmployeeDepartment(employee_id=manager.id, department_id=second.id,
                               role="Руководитель", is_primary=True),
        ])
        session.commit()

    response = client.get("/organization")
    assert response.status_code == 200
    assert f"Технический отдел {suffix}" in response.text
    assert f"ОПО {suffix}" in response.text
    # The manager is rendered once as a graph node, not duplicated per department.
    assert response.text.count('class="org-manager-node"') == 1


def test_drag_assignment_creates_membership_without_duplicating_employee(client):
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        first = Department(name=f"Отдел A {suffix}")
        second = Department(name=f"Отдел B {suffix}")
        employee = Employee(name=f"Сотрудник {suffix}")
        session.add_all([first, second, employee])
        session.commit()
        first_id, second_id, employee_id = first.id, second.id, employee.id

    first_response = client.post(
        f"/api/org/departments/{first_id}/members/{employee_id}",
        json={"is_head": True, "role": "Заместитель директора"},
    )
    second_response = client.post(
        f"/api/org/departments/{second_id}/members/{employee_id}",
        json={"is_head": True},
    )
    assert first_response.status_code == 200
    assert second_response.status_code == 200

    with SessionLocal() as session:
        employee = session.get(Employee, employee_id)
        assert employee is not None
        assert len(employee.department_memberships) == 2
        assert all(item.is_primary for item in employee.department_memberships)
        assert session.query(Employee).filter_by(name=f"Сотрудник {suffix}").count() == 1


def test_quick_create_saves_full_employee_profile_and_department_assignment(client):
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        department = Department(name=f"Отдел быстрого добавления {suffix}")
        session.add(department)
        session.commit()
        department_id = department.id

    response = client.post("/api/org/employees", json={
        "name": f"Новый инженер {suffix}",
        "position": "Инженер АСУ ТП",
        "aliases": "Инженер, Иван",
        "competencies": "PLC, SCADA",
        "responsibilities": "Разработка и сопровождение",
        "context": "Согласует изменения с руководителем",
        "department_id": department_id,
        "department_role": "Ведущий инженер",
        "department_responsibilities": "Пусконаладка",
        "is_head": True,
    })
    assert response.status_code == 201
    with SessionLocal() as session:
        employee = session.query(Employee).filter_by(name=f"Новый инженер {suffix}").one()
        assert employee.position == "Инженер АСУ ТП"
        assert employee.competencies == "PLC, SCADA"
        assert employee.responsibilities == "Разработка и сопровождение"
        assert employee.context == "Согласует изменения с руководителем"
        assert len(employee.department_memberships) == 1
        membership = employee.department_memberships[0]
        assert membership.department_id == department_id
        assert membership.role == "Ведущий инженер"
        assert membership.responsibilities == "Пусконаладка"
        assert membership.is_primary is True
