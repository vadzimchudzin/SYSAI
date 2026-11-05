"""Top-down organisation tree for the departments map.

The map is a real tree: every active employee and every department appears exactly once,
under one parent. The parent is picked by priority, so the picture follows what was set up
in the constructor:

* a department hangs from an explicit block link (company / department / person →
  department); otherwise from its head when the head sits elsewhere in the tree (has a
  reporting line or is the director); otherwise from the director; otherwise from the company;
* a person hangs from an explicit reporting line; otherwise from the department they head;
  otherwise from the department they joined first; a person without a department who leads
  somebody hangs from the director; people with no department and no line at all gather in
  «Без подразделения» next to the departments;
* the director hangs from the company unless a line says otherwise.

Links that are not used for the tree (a second department, a second line) come back as
secondary lines. A combination of links that would close a loop never hides anybody: the
weakest link in the loop gives way and the card moves to its next option.
"""
from __future__ import annotations

COMPANY = "company:1"
FREE = "group:free"

# When links form a loop the weakest one gives way.
_STRENGTH = {"reporting": 5, "block": 5, "director": 4, "manager": 3, "membership": 3,
             "heads": 2, "lead": 1, "root": 0, "company": 0, "free": 0}
_BLOCK_LABEL = {"company": "Подразделение", "department": "Связь отделов", "employee": "Руководит"}


def build(departments, employees, relations, director_id=None):
    """Arrange the organisation as a tree.

    ``departments`` — ``[{"id", "name"}]`` in display order; ``employees`` — ``[{"id", "name",
    "memberships": [{"id", "department_id", "is_primary", "role"}]}]`` in display order;
    ``relations`` — ``[{"id", "source", "target", "label"}]`` with keys like ``employee:5``.

    Returns ``{"nodes": [...], "cross": [...], "secondary": {employee_id: [department_id]}}``:
    nodes go parents first with children in display order, each with the tree link that
    leads to it (``parent``, ``kind``, ``label`` and ``relation_id`` / ``employee_id`` +
    ``department_id`` when the link can be removed from the map).
    """
    dept_keys = [f"department:{d['id']}" for d in departments]
    emp_keys = [f"employee:{e['id']}" for e in employees]
    known = set(dept_keys) | set(emp_keys) | {COMPANY}
    order = {key: index for index, key in enumerate(dept_keys + emp_keys)}
    director = f"employee:{director_id}" if director_id and f"employee:{director_id}" in known else None

    incoming, leads = {}, set()
    for relation in sorted(relations, key=lambda r: r["id"]):
        source, target = relation["source"], relation["target"]
        if source in known and target in known and source != target and target != COMPANY:
            incoming.setdefault(target, []).append(relation)
            leads.add(source)

    def relation_option(relation, kind, default_label):
        return {"parent": relation["source"], "kind": kind,
                "label": relation.get("label") or default_label, "relation_id": relation["id"]}

    options = {}
    heads = {}
    for employee in employees:
        key = f"employee:{employee['id']}"
        own = [relation_option(r, "reporting", "Подчинение") for r in incoming.get(key, [])]
        memberships = [m for m in employee.get("memberships", [])
                       if f"department:{m['department_id']}" in known]
        # The department a person heads comes first, then the one they joined first.
        memberships.sort(key=lambda m: (not m.get("is_primary"), m.get("id") or 0,
                                        order[f"department:{m['department_id']}"]))
        for membership in memberships:
            if membership.get("is_primary"):
                heads.setdefault(membership["department_id"], []).append(employee)
        if key == director:
            own.append({"parent": COMPANY, "kind": "director", "label": "Директор"})
        else:
            for membership in memberships:
                primary = bool(membership.get("is_primary"))
                own.append({
                    "parent": f"department:{membership['department_id']}",
                    "kind": "manager" if primary else "membership",
                    "label": membership.get("role") or ("Руководитель" if primary else "Сотрудник"),
                    "employee_id": employee["id"], "department_id": membership["department_id"],
                })
            if not memberships and not incoming.get(key):
                if key in leads:
                    # Nobody above and no department, but somebody reports to them: they belong to the
                    # hierarchy, not to the pile — under the director (or above him if he reports to them).
                    if director:
                        own.append({"parent": director, "kind": "lead", "label": "Подчинение"})
                    own.append({"parent": COMPANY, "kind": "company", "label": "Компания"})
                else:
                    own.append({"parent": FREE, "kind": "free", "label": "Без подразделения"})
            else:
                own.append({"parent": COMPANY, "kind": "company", "label": "Компания"})
        options[key] = own

    for department in departments:
        key = f"department:{department['id']}"
        own = [relation_option(r, "block", _BLOCK_LABEL.get(r["source"].split(":")[0], "Связь отделов"))
               for r in incoming.get(key, [])]
        for head in heads.get(department["id"], []):
            head_key = f"employee:{head['id']}"
            if head_key == director or incoming.get(head_key):
                own.append({"parent": head_key, "kind": "heads", "label": "Руководит",
                            "employee_id": head["id"], "department_id": department["id"]})
        if director:
            own.append({"parent": director, "kind": "lead", "label": "Подразделение"})
        own.append({"parent": COMPANY, "kind": "root", "label": "Подразделение"})
        options[key] = own

    pick = {key: 0 for key in options}
    chosen = lambda key: options[key][pick[key]]

    def loop_from(start):
        path, seen, key = [], {}, start
        while key in options:
            if key in seen:
                return path[seen[key]:]
            seen[key] = len(path)
            path.append(key)
            key = chosen(key)["parent"]
        return None

    keys = dept_keys + emp_keys
    for _ in range(sum(len(v) for v in options.values()) + 1):
        loop = next((found for found in map(loop_from, keys) if found), None)
        if not loop:
            break
        weakest = min(loop, key=lambda k: (_STRENGTH.get(chosen(k)["kind"], 0), -order[k]))
        pick[weakest] += 1

    parent = {key: chosen(key)["parent"] for key in options}
    link = {key: chosen(key) for key in options}
    if FREE in parent.values():
        # The pile of people without a department sits next to the departments, under the director.
        holder = director or COMPANY
        up = director
        while up in parent:
            up = parent[up]
            if up == FREE:  # the director reports to someone from the pile: keep the pile at the top
                holder = COMPANY
                break
        parent[FREE] = holder
        link[FREE] = {"parent": holder, "kind": "group", "label": "Без подразделения"}

    children = {}
    for key, up in parent.items():
        children.setdefault(up, []).append(key)

    def rank(key):
        if key == FREE:
            return (9, 0)
        if key == director:
            return (0, 0)
        if key.startswith("employee:"):
            return (1 if link[key]["kind"] in {"manager", "director"} else 2, order[key])
        return (3 if link[key]["kind"] == "heads" else 4, order[key])

    nodes = [{"key": COMPANY, "parent": None, "kind": "company", "label": ""}]
    stack = [COMPANY]
    while stack:
        current = stack.pop()
        kids = sorted(children.get(current, []), key=rank)
        for kid in kids:
            nodes.append({"key": kid, **link[kid]})
        stack.extend(reversed(kids))
    # Depth-first walk above lists parents before children; keep that order stable.
    adjacent = {frozenset((key, up)) for key, up in parent.items()}
    used_relations = {l["relation_id"] for l in link.values() if l.get("relation_id")}
    used_memberships = {(l["employee_id"], l["department_id"]) for l in link.values() if l.get("employee_id")}

    cross = []
    for relation in sorted(relations, key=lambda r: r["id"]):
        source, target = relation["source"], relation["target"]
        if relation["id"] in used_relations or source not in known or target not in known or source == target:
            continue
        if frozenset((source, target)) in adjacent:
            continue
        cross.append({"source": source, "target": target,
                      "kind": "block" if target.startswith("department:") else "reporting",
                      "label": relation.get("label") or "", "relation_id": relation["id"]})
    def ancestors(key):
        found, up = set(), parent.get(key)
        while up is not None and up not in found:
            found.add(up)
            up = parent.get(up)
        return found

    secondary = {}
    for employee in employees:
        key = f"employee:{employee['id']}"
        above = ancestors(key)
        for membership in employee.get("memberships", []):
            dept_key = f"department:{membership['department_id']}"
            if dept_key not in known or (employee["id"], membership["department_id"]) in used_memberships:
                continue
            # Already drawn inside that department's branch (e.g. under its head): nothing to add.
            if dept_key in above or frozenset((key, dept_key)) in adjacent:
                continue
            secondary.setdefault(employee["id"], []).append(membership["department_id"])
            primary = bool(membership.get("is_primary"))
            cross.append({"source": dept_key, "target": key, "kind": "manager" if primary else "membership",
                          "label": membership.get("role") or ("Руководитель" if primary else "Сотрудник"),
                          "employee_id": employee["id"], "department_id": membership["department_id"]})
    return {"nodes": nodes, "cross": cross, "secondary": secondary}
