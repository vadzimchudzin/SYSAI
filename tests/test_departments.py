"""Multi-department membership and routing regression tests."""
import json

from app import analyze
from app.db import Department, Employee, EmployeeDepartment, SessionLocal


def test_employee_can_have_distinct_roles_and_duties_in_multiple_departments():
    with SessionLocal() as session:
        marketing = Department(name="Маркетинг", description="Тендеры и продвижение")
        technical = Department(name="Технический отдел", description="Оборудование и закупки")
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
        assert {item["name"] for item in encoded["departments"]} == {"Маркетинг", "Технический отдел"}
        assert any(item["role"] == "Специалист по закупкам" for item in encoded["departments"])


def test_department_specific_duties_resolve_same_name_employee():
    with SessionLocal() as session:
        marketing = Department(name="Маркетинг")
        technical = Department(name="Технический отдел")
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
