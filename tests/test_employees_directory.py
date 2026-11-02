"""Employee directory: list rows for search and filters, the side card and saving it in place."""
from app.db import Department, Employee, EmployeeDepartment, SessionLocal

AJAX = {"X-Requested-With": "fetch"}


def _person(**fields):
    with SessionLocal() as s:
        person = Employee(**fields)
        s.add(person)
        s.commit()
        return person.id


def _row(html, employee_id):
    return html.split(f'id="employee-{employee_id}"')[1].split("</article>")[0]


def test_directory_rows_carry_filter_data_and_groups(client):
    with SessionLocal() as s:
        sales = Department(name="Отдел продаж")
        s.add(sales)
        s.flush()
        head = Employee(name="Пётр Ёлкин", position="Руководитель продаж", aliases="Петя", telegram_chat_id="@petr")
        s.add(head)
        s.flush()
        s.add(EmployeeDepartment(employee_id=head.id, department_id=sales.id, role="Начальник", is_primary=True))
        s.commit()
        head_id, sales_id = head.id, sales.id
    loner_id = _person(name="Анна Смирнова", position="Бухгалтер")
    retired_id = _person(name="Олег Старый", active=False)

    html = client.get("/employees").text
    assert 'id="employee-search"' in html and 'data-chip="no-tg"' in html and "employees.js" in html
    head_row = _row(html, head_id)
    assert f'data-departments="{sales_id}"' in head_row and 'data-telegram="1"' in head_row
    assert "петр елкин" in head_row and "петя" in head_row and "отдел продаж" in head_row  # ё folded for search
    assert "★" in head_row and ">ПЁ</span>" in head_row
    loner_row = _row(html, loner_id)
    assert 'data-telegram="0"' in loner_row and 'data-voice="none"' in loner_row and 'data-active="1"' in loner_row
    retired_row = _row(html, retired_id)
    assert 'data-active="0"' in retired_row and 'data-voice="off"' in retired_row
    # Department groups first, people without one at the end; inactive people last inside a group.
    assert html.index(f'data-group="d{sales_id}"') < html.index('data-group="none"')
    assert html.index(f'id="employee-{loner_id}"') < html.index(f'id="employee-{retired_id}"')
    assert "2 человека · 1 с Telegram · голос готов у 0 · неактивных: 1" in html
    assert "Выберите сотрудника" in html


def test_panel_fragment_opens_card_add_form_and_404(client):
    person_id = _person(name="Ирина Котова", position="Юрист")
    card = client.get(f"/employees/panel?edit={person_id}")
    assert card.status_code == 200
    assert 'id="employee-card"' in card.text and "Ирина Котова" in card.text
    assert 'name="access_level"' in card.text and 'data-tab="voice"' in card.text
    assert "<html" not in card.text and "emp-toolbar" not in card.text  # only the card, not the whole page
    assert 'data-active-tab="voice"' in client.get(f"/employees/panel?edit={person_id}&tab=voice").text
    assert 'data-active-tab="profile"' in client.get(f"/employees/panel?edit={person_id}&tab=bogus").text
    blank = client.get("/employees/panel?add=1").text
    assert 'id="employee-create"' in blank and "Создать карточку" in blank and 'data-tab="voice"' not in blank
    assert "Выберите сотрудника" in client.get("/employees/panel").text
    assert client.get("/employees/panel?edit=999999").status_code == 404
    page = client.get(f"/employees?edit={person_id}").text
    assert f'selected" id="employee-{person_id}"' in page and "panel-open" in page


def test_ajax_save_returns_fresh_list_and_card(client):
    created = client.post("/employees", data={"name": "  Мария Белова ", "position": "Аналитик", "next": ""},
                          headers=AJAX)
    assert created.status_code == 200
    payload = created.json()
    assert payload["ok"] and payload["created"] and payload["next"] == ""
    assert payload["message"] == "Сотрудник добавлен."
    new_id = payload["id"]
    assert 'id="employee-list-region"' in payload["list_html"]
    assert f'selected" id="employee-{new_id}"' in payload["list_html"]
    assert 'id="employee-card"' in payload["panel_html"] and "Мария Белова" in payload["panel_html"]
    assert payload["summary"].startswith("1 человек ·")

    edited = client.post("/employees", headers=AJAX, data={
        "id": new_id, "name": "Мария Белова", "position": "Ведущий аналитик", "active": "on", "tab": "contacts"}).json()
    assert edited["ok"] and not edited["created"] and edited["id"] == new_id
    assert edited["message"] == "Изменения сохранены."
    assert "Ведущий аналитик" in edited["panel_html"] and 'data-active-tab="contacts"' in edited["panel_html"]
    with SessionLocal() as s:
        assert s.get(Employee, new_id).position == "Ведущий аналитик"


def test_save_and_add_next_returns_blank_form(client):
    payload = client.post("/employees", data={"name": "Глеб Орлов", "next": "add"}, headers=AJAX).json()
    assert payload["ok"] and payload["next"] == "add"
    assert 'id="employee-create"' in payload["panel_html"] and "Сохранено: Глеб Орлов" in payload["panel_html"]
    assert 'value="Глеб Орлов"' not in payload["panel_html"]  # the form is empty for the next person
    assert f'id="employee-{payload["id"]}"' in payload["list_html"]

    plain = client.post("/employees", data={"name": "Ника Лис", "next": "add"}, follow_redirects=False)
    assert plain.status_code == 303
    location = plain.headers["location"]
    assert location.startswith("/employees?add=1&saved=") and location.endswith("#employee-create")
    page = client.get(location.split("#")[0]).text
    assert "Сохранено: Ника Лис" in page and 'id="employee-create"' in page


def test_ajax_save_errors_come_back_as_json(client):
    for name in ("", "   "):
        blank = client.post("/employees", data={"name": name}, headers=AJAX)
        assert blank.status_code == 400
        assert blank.json() == {"ok": False, "error": "Укажите имя и фамилию сотрудника"}
    person_id = _person(name="Без Телеграма")
    denied = client.post("/employees", headers=AJAX,
                         data={"id": person_id, "name": "Без Телеграма", "active": "on", "access_level": "full"})
    assert denied.status_code == 400 and "Telegram" in denied.json()["error"]
    missing = client.post("/employees", data={"id": 999999, "name": "Призрак"}, headers=AJAX)
    assert missing.status_code == 404 and missing.json()["ok"] is False
    # Without JavaScript the same problems stay ordinary HTTP errors.
    assert client.post("/employees", data={"name": "   "}).status_code == 400
    with SessionLocal() as s:
        assert s.query(Employee).count() == 1


def test_voice_actions_return_to_the_voice_tab(client):
    person_id = _person(name="Вера Голосова")
    response = client.post(f"/employees/{person_id}/voice-sample/delete", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/employees?edit={person_id}&tab=voice&voice_msg=") and location.endswith("#voice")
    page = client.get(location.split("#")[0]).text
    assert 'data-active-tab="voice"' in page and "emp-voice-msg" in page and "Образцы и признаки отозваны" in page
