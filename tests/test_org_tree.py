"""The departments map is a top-down tree built by app.org_tree.build."""
from app import org_tree
from app.org_tree import COMPANY, FREE


def dept(dept_id, name=None):
    return {"id": dept_id, "name": name or f"Отдел {dept_id}"}


def person(emp_id, *memberships):
    """memberships: (membership_id, department_id, is_primary[, role])"""
    rows = []
    for item in memberships:
        membership_id, department_id, primary = item[:3]
        rows.append({"id": membership_id, "department_id": department_id, "is_primary": primary,
                     "role": item[3] if len(item) > 3 else ""})
    return {"id": emp_id, "name": f"Сотрудник {emp_id}", "memberships": rows}


def relation(rel_id, source, target, label=""):
    return {"id": rel_id, "source": source, "target": target, "label": label}


def check_tree(result):
    """Every card appears once, parents come before children and nothing loops."""
    nodes = result["nodes"]
    keys = [node["key"] for node in nodes]
    assert len(keys) == len(set(keys))
    assert nodes[0] == {"key": COMPANY, "parent": None, "kind": "company", "label": ""}
    seen = set()
    for node in nodes:
        assert node["parent"] is None or node["parent"] in seen, node
        seen.add(node["key"])
    return {node["key"]: node for node in nodes}


def children(result, key):
    return [node["key"] for node in result["nodes"] if node["parent"] == key]


def test_director_on_top_then_departments_with_head_first():
    result = org_tree.build(
        [dept(1, "Продажи"), dept(2, "Бухгалтерия")],
        [person(10), person(11, (1, 1, False)), person(12, (2, 1, True, "Начальник")), person(13, (3, 2, False))],
        [], director_id=10)
    nodes = check_tree(result)
    assert children(result, COMPANY) == ["employee:10"]
    assert nodes["employee:10"]["kind"] == "director"
    assert children(result, "employee:10") == ["department:1", "department:2"]
    assert nodes["department:1"]["kind"] == "lead"
    # The head goes first, then the rest of the staff in display order.
    assert children(result, "department:1") == ["employee:12", "employee:11"]
    assert nodes["employee:12"] == {"key": "employee:12", "parent": "department:1", "kind": "manager",
                                    "label": "Начальник", "employee_id": 12, "department_id": 1}
    assert nodes["employee:11"]["kind"] == "membership" and nodes["employee:11"]["label"] == "Сотрудник"
    assert result["cross"] == [] and result["secondary"] == {}


def test_without_director_departments_hang_from_the_company():
    result = org_tree.build([dept(1)], [person(11, (1, 1, False))], [], director_id=None)
    nodes = check_tree(result)
    assert nodes["department:1"]["parent"] == COMPANY and nodes["department:1"]["kind"] == "root"
    assert nodes["employee:11"]["parent"] == "department:1"


def test_sub_department_hangs_from_its_parent_block():
    result = org_tree.build(
        [dept(1, "Продажи"), dept(2, "Склад")], [person(10)],
        [relation(5, "department:1", "department:2", "")], director_id=10)
    nodes = check_tree(result)
    assert nodes["department:2"] == {"key": "department:2", "parent": "department:1", "kind": "block",
                                     "label": "Связь отделов", "relation_id": 5}
    assert nodes["department:1"]["parent"] == "employee:10"


def test_company_block_link_keeps_department_right_under_the_company():
    result = org_tree.build([dept(1)], [person(10)], [relation(5, COMPANY, "department:1")], director_id=10)
    nodes = check_tree(result)
    assert nodes["department:1"]["parent"] == COMPANY
    assert nodes["department:1"]["kind"] == "block" and nodes["department:1"]["label"] == "Подразделение"


def test_deputy_with_a_reporting_line_carries_the_department_he_heads():
    result = org_tree.build(
        [dept(1, "Технический отдел")],
        [person(10), person(20, (1, 1, True, "Руководитель")), person(21, (2, 1, False))],
        [relation(7, "employee:10", "employee:20", "Заместитель директора")], director_id=10)
    nodes = check_tree(result)
    assert nodes["employee:20"]["parent"] == "employee:10"
    assert nodes["employee:20"]["kind"] == "reporting" and nodes["employee:20"]["relation_id"] == 7
    assert nodes["department:1"]["parent"] == "employee:20" and nodes["department:1"]["kind"] == "heads"
    assert children(result, "department:1") == ["employee:21"]
    # The head membership is drawn by the department link, so it is not repeated as a secondary line.
    assert result["cross"] == [] and result["secondary"] == {}


def test_director_heading_a_department_carries_it():
    result = org_tree.build([dept(1), dept(2)], [person(10, (1, 1, True))], [], director_id=10)
    nodes = check_tree(result)
    assert nodes["employee:10"]["parent"] == COMPANY
    assert nodes["department:1"]["kind"] == "heads" and nodes["department:1"]["parent"] == "employee:10"
    assert nodes["department:2"]["kind"] == "lead"
    # The department led by the director goes before the other departments.
    assert children(result, "employee:10") == ["department:1", "department:2"]


def test_person_in_two_departments_appears_once_and_keeps_a_secondary_line():
    result = org_tree.build(
        [dept(1), dept(2)],
        # Joined department 2 first (smaller membership id), then department 1.
        [person(10), person(30, (9, 1, False, "Инженер"), (4, 2, False, "Наладчик"))],
        [], director_id=10)
    nodes = check_tree(result)
    assert nodes["employee:30"]["parent"] == "department:2"
    assert result["secondary"] == {30: [1]}
    assert result["cross"] == [{"source": "department:1", "target": "employee:30", "kind": "membership",
                                "label": "Инженер", "employee_id": 30, "department_id": 1}]


def test_reporting_line_beats_department_membership():
    result = org_tree.build(
        [dept(1), dept(2)], [person(10), person(20, (1, 2, True)), person(21, (2, 1, False))],
        [relation(3, "employee:20", "employee:21", "Помощник")], director_id=10)
    nodes = check_tree(result)
    assert nodes["employee:21"]["parent"] == "employee:20" and nodes["employee:21"]["label"] == "Помощник"
    # The boss is from another department, so the own department comes back as a secondary line.
    assert result["secondary"] == {21: [1]}


def test_membership_inside_its_own_branch_is_not_repeated():
    # Under the head of the same department: the card already sits in that department's branch.
    result = org_tree.build(
        [dept(1)], [person(10), person(20, (1, 1, True)), person(21, (2, 1, False))],
        [relation(3, "employee:20", "employee:21", "Помощник")], director_id=10)
    nodes = check_tree(result)
    assert nodes["employee:21"]["parent"] == "employee:20"
    assert nodes["employee:20"]["parent"] == "department:1"
    assert result["secondary"] == {} and result["cross"] == []


def test_people_without_department_gather_under_the_director_last():
    result = org_tree.build([dept(1)], [person(10), person(40), person(41), person(11, (1, 1, False))],
                            [], director_id=10)
    nodes = check_tree(result)
    assert nodes[FREE]["parent"] == "employee:10" and nodes[FREE]["kind"] == "group"
    assert children(result, FREE) == ["employee:40", "employee:41"]
    assert children(result, "employee:10")[-1] == FREE
    without_director = org_tree.build([dept(1)], [person(40)], [], director_id=None)
    assert check_tree(without_director)[FREE]["parent"] == COMPANY


def test_no_empty_group_when_everybody_has_a_place():
    result = org_tree.build([dept(1)], [person(10), person(11, (1, 1, False))], [], director_id=10)
    assert FREE not in check_tree(result)


def test_a_line_into_the_director_does_not_lose_anybody():
    # The director is put under a person from a department that itself hangs from the director.
    result = org_tree.build(
        [dept(1)], [person(10), person(11, (1, 1, False))],
        [relation(2, "employee:11", "employee:10")], director_id=10)
    nodes = check_tree(result)
    assert set(nodes) == {COMPANY, "department:1", "employee:10", "employee:11"}
    # The explicit line wins; the department falls back to the company.
    assert nodes["employee:10"]["parent"] == "employee:11"
    assert nodes["department:1"]["parent"] == COMPANY


def test_loop_through_a_head_is_broken_at_the_weakest_link():
    # Department 1 would hang from its head (who has a line), the head from a member of department 1.
    result = org_tree.build(
        [dept(1)], [person(10), person(20, (1, 1, True)), person(21, (2, 1, False))],
        [relation(3, "employee:21", "employee:20")], director_id=10)
    nodes = check_tree(result)
    assert set(nodes) == {COMPANY, "department:1", "employee:10", "employee:20", "employee:21"}
    assert nodes["department:1"]["parent"] == "employee:10"
    assert nodes["employee:21"]["parent"] == "department:1"
    assert nodes["employee:20"]["parent"] == "employee:21"


def test_boss_without_department_stays_in_the_hierarchy():
    result = org_tree.build([dept(1)], [person(10), person(60), person(61)],
                            [relation(1, "employee:60", "employee:61", "Помощник")], director_id=10)
    nodes = check_tree(result)
    assert nodes["employee:60"]["parent"] == "employee:10"
    assert nodes["employee:61"]["parent"] == "employee:60" and nodes["employee:61"]["label"] == "Помощник"
    assert FREE not in nodes


def test_owner_above_the_director_tops_the_chart():
    result = org_tree.build([dept(1)], [person(10), person(50), person(51)],
                            [relation(1, "employee:50", "employee:10", "Директор")], director_id=10)
    nodes = check_tree(result)
    assert children(result, COMPANY) == ["employee:50"]
    assert nodes["employee:10"]["parent"] == "employee:50"
    assert nodes["department:1"]["parent"] == "employee:10"
    assert nodes[FREE]["parent"] == "employee:10" and children(result, FREE) == ["employee:51"]


def test_links_to_unknown_cards_are_ignored_and_a_looped_pair_is_drawn_once():
    result = org_tree.build(
        [dept(1), dept(2)], [person(10), person(11, (1, 1, False))],
        [relation(1, "employee:99", "employee:11"), relation(2, "department:1", "department:2"),
         relation(3, "department:2", "department:1", "обратная")], director_id=10)
    nodes = check_tree(result)
    assert nodes["employee:11"]["parent"] == "department:1"
    # 1 → 2 and 2 → 1 would loop: one link gives way, the pair stays joined by one line.
    assert nodes["department:2"]["parent"] == "employee:10"
    assert nodes["department:1"]["parent"] == "department:2" and nodes["department:1"]["relation_id"] == 3
    assert result["cross"] == []


def test_second_reporting_line_comes_back_as_cross():
    result = org_tree.build(
        [dept(1), dept(2)],
        [person(10), person(11, (1, 1, False)), person(12, (2, 2, False)), person(13, (3, 1, False))],
        [relation(4, "employee:11", "employee:13", "Наставник"), relation(5, "employee:12", "employee:13", "Проект")],
        director_id=10)
    nodes = check_tree(result)
    # The first line places the card, the second one is drawn across the branches.
    assert nodes["employee:13"]["parent"] == "employee:11" and nodes["employee:13"]["relation_id"] == 4
    assert result["cross"] == [{"source": "employee:12", "target": "employee:13", "kind": "reporting",
                                "label": "Проект", "relation_id": 5}]
    # Under a colleague from the same department the card is still inside its department's branch.
    assert result["secondary"] == {}
