import uuid

from app import pipeline
from app.db import Employee, SessionLocal


def test_directory_terms_include_position_and_employee_duties():
    suffix = uuid.uuid4().hex[:8]
    with SessionLocal() as session:
        session.add(Employee(
            name=f"Инженер {suffix}",
            aliases=f"Иван {suffix}",
            position="Инженер КИПиА",
            responsibilities="диагностика датчиков; закупка запчастей, настройка преобразователей",
        ))
        session.commit()
    terms = pipeline._directory_terms(limit=500)
    assert f"Инженер {suffix}" in terms
    assert f"Иван {suffix}" in terms
    assert "Инженер КИПиА" in terms
    assert "диагностика датчиков" in terms
    assert "закупка запчастей" in terms
    assert "настройка преобразователей" in terms
