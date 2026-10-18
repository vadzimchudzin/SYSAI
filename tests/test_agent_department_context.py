"""The AI employee must receive department-specific roles and responsibilities for routing."""
from types import SimpleNamespace
import uuid

from app import agent
from app.db import Department, Employee, EmployeeDepartment, SessionLocal


def test_team_context_contains_department_specific_duties():
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        department = Department(name=f"Технический отдел {suffix}")
        person = Employee(name=f"Татьяна {suffix}", position="Специалист", responsibilities="")
        session.add_all([department, person])
        session.flush()
        session.add(EmployeeDepartment(
            employee_id=person.id,
            department_id=department.id,
            role="Специалист по закупкам",
            responsibilities="закупки оборудования, поставщики",
        ))
        session.flush()

        row = SimpleNamespace(chat_id=f"department-context-{suffix}", id=999999)
        context = agent._context(
            session, row, {"name": "Проверяющий"}, None,
            reviewer=True, full=True, director=True,
        )
        entry = next(item for item in context["team"] if item["id"] == person.id)
        assert entry["departments"] == [{
            "name": department.name,
            "role": "Специалист по закупкам",
            "responsibilities": "закупки оборудования, поставщики",
        }]
