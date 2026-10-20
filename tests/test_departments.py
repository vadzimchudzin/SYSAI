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