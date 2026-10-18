import uuid

from app import pipeline
from app.db import Department, Employee, EmployeeDepartment, SessionLocal


def test_glossary_contains_department_role_and_responsibilities():
    suffix = uuid.uuid4().hex[:8]
    department_name = f"Технический отдел {suffix}"
    role = f"Специалист по закупкам {suffix}"
    duty = f"настройка сушилки {suffix}"
    with SessionLocal() as session:
        department = Department(name=department_name, active=True)
        employee = Employee(name=f"Татьяна {suffix}", position="Специалист")
        session.add_all([department, employee])
        session.flush()
        session.add(EmployeeDepartment(employee_id=employee.id, department_id=department.id,
                                       role=role, responsibilities=f"закупка запчастей; {duty}"))
        session.commit()
    terms = pipeline._directory_terms(limit=500)
    assert department_name in terms
    assert role in terms
    assert "закупка запчастей" in terms
    assert duty in terms
