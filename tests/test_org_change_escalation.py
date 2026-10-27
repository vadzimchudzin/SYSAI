"""Organizational change (car/driver earlier) must go to the reviewer, pay jokes must not."""
from app import agent


def test_driver_needed_a_day_earlier_is_an_org_request():
    assert agent.organizational_change_request("Мне он нужен на 1 ноября")
    assert agent.organizational_change_request("Надо 1 собрать вещи, чтобы 2 выехать")
    assert agent.organizational_change_request("Нам надо ехать сутки надо дать раньше")
    assert agent.organizational_change_request("Водителя дайте на 1-е, тачка нужна раньше")


def test_fact_question_is_not_an_org_request():
    assert not agent.organizational_change_request("Так дадут мне водителя?")
    assert not agent.organizational_change_request("Когда перевод бригады?")
    assert not agent.organizational_change_request("Привет, мне дадут водителя отлично")


def test_pay_joke_is_not_an_org_request():
    assert not agent.organizational_change_request("В связи с долгим переездом надо повысить мне зарплату")
    assert not agent.organizational_change_request("поднимите зп и дайте водителя на 1")
