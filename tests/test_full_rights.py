"""People with full rights (director, reviewer, full access) are recognised and marked red in the directory."""
from app import agent, settings_store
from app.db import Employee, SessionLocal


def add(**kw):
    with SessionLocal() as s:
        e = Employee(**kw)
        s.add(e)
        s.commit()
        return e.id


def test_rights_by_username_id_and_phone():
    settings_store.set_many({"approver_chat_ids": ["@omgmzf"], "full_access_chat_ids": ["5854113570"],
                             "director_chat_ids": ["+375291391080"]})
    omg = Employee(name="OMG", telegram_chat_id="@OmgMzf")
    vadim = Employee(name="Вадим", telegram_chat_id="@someone", telegram_user_id="5854113570")
    boss = Employee(name="Директор", telegram_chat_id="+375 29 139-10-80")
    other = Employee(name="Аня", telegram_chat_id="@anya_work")
    assert agent.rights_of(omg) == "reviewer"
    assert agent.rights_of(vadim) == "full"
    assert agent.rights_of(boss) == "director"
    assert agent.rights_of(other) == ""


def test_directory_marks_full_rights_red(client):
    settings_store.set_many({"approver_chat_ids": ["@omgmzf"]})
    add(name="OMG", position="Разработчик", telegram_chat_id="@omgmzf", telegram_user_id="622744825")
    add(name="Аня", position="Бухгалтер", telegram_chat_id="@anya_work")
    html = client.get("/employees").text
    omg = html[html.index("<b>OMG</b>") - 600: html.index("<b>OMG</b>") + 300]
    anya = html[html.index("<b>Аня</b>") - 600: html.index("<b>Аня</b>") + 300]
    assert "full-rights" in omg and "Полные права" in omg
    assert "full-rights" not in anya and "Полные права" not in anya
