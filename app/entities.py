"""Objects and clients (№19): one card per client, object or other organization, with where it comes up.

Cards appear by hand on the «Объекты и клиенты» page, from incoming documents (the counterparty the intake found,
doc_intake.py; the object of a document is linked to an existing card) and from approved meetings (facts about a client
or an object, memory.learn_from_meeting).
Names are compared without the legal form and the quote marks, so «Криница», «ОАО «Криница»» and «"Криница"» are one
card; the same УНП is one card too. A misspelt name («Крынiца», «Криница-М») is not merged by itself: the page offers it
as a possible duplicate, one click merges. Mentions are found in meetings, tasks, documents and messages (Russian endings
are understood: «Криницы», «Кринице»), each with the words, and every fact on a card keeps its source.
"""
import difflib
import logging
import re
import threading
import time

from . import config, settings_store
from .db import (ConversationMessage, Document, Entity, EntityLink, Meeting, Note, SessionLocal, Task, now)

log = logging.getLogger("sysai.entities")

KINDS = {"client": "Клиент", "object": "Объект", "contractor": "Подрядчик / поставщик", "org": "Организация"}
SOURCE_TYPES = {"meeting": "Совещания", "task": "Задачи", "document": "Документы", "message": "Переписка"}
_LEGAL_LONG = sorted((
    "общество с ограниченной ответственностью", "общество с дополнительной ответственностью",
    "совместное общество с ограниченной ответственностью", "иностранное общество с ограниченной ответственностью",
    "открытое акционерное общество", "закрытое акционерное общество", "публичное акционерное общество",
    "акционерное общество", "частное унитарное предприятие", "частное торговое унитарное предприятие",
    "частное производственное унитарное предприятие", "частное строительное унитарное предприятие",
    "производственное унитарное предприятие", "торговое унитарное предприятие", "строительное унитарное предприятие",
    "республиканское унитарное предприятие", "коммунальное унитарное предприятие", "коммунальное производственное "
    "унитарное предприятие", "государственное учреждение", "государственное предприятие", "унитарное предприятие",
    "индивидуальный предприниматель"), key=len, reverse=True)
_LEGAL_SHORT = set("ооо оао зао пао ао одо сооо иооо чуп чтуп чпуп чсуп пуп туп суп уп руп куп кпуп гп гуп гу ип чп тчуп "
                   "ип".split())
_WORD = re.compile(r"[а-яa-z0-9]+")
MIN_NAME = 3
SCAN_BATCH = 300
_CACHE = {"at": 0.0, "patterns": []}
_CACHE_LOCK = threading.Lock()


class EntityError(ValueError):
    """A card cannot be saved or merged (Russian message for the panel)."""


def _testing() -> bool:
    return config.TESTING


# ----------------------------------------------------------------------------- names
def _plain(text: str) -> str:
    t = str(text or "").lower().replace("ё", "е")
    t = re.sub(r"[«»“”„\"'‘’`]", " ", t)
    return " ".join(_WORD.findall(t))


def key(name: str) -> str:
    """«ОАО «Криница»» → «криница»: lower case, no quote marks, no legal form, no punctuation."""
    t = f" {_plain(name)} "
    for phrase in _LEGAL_LONG:
        t = t.replace(f" {phrase} ", " ")
    words = [w for w in t.split() if w not in _LEGAL_SHORT]
    return " ".join(words)


def _names(e) -> list:
    out = []
    for n in [e.name] + list(e.aliases or []):
        k = key(n)
        if len(k) >= MIN_NAME and k not in out:
            out.append(k)
    return out


def _word_re(word: str) -> str:
    if len(word) >= 5 and word[-1] in "аяоеыиуюйь":
        return re.escape(word[:-1]) + r"[а-яa-z]{0,3}"
    if len(word) >= 4 and not word.isdigit():
        return re.escape(word) + r"[а-яa-z]{0,3}"
    return re.escape(word)


def pattern(names: list):
    """One regex for all the spellings of a card over _plain() text; Russian endings allowed («Криницы», «Кринице»)."""
    alts = [r"[\s]+".join(_word_re(w) for w in n.split()) for n in names if n]
    if not alts:
        return None
    return re.compile(r"(?<![а-яa-z0-9])(?:" + "|".join(sorted(alts, key=len, reverse=True)) + r")(?![а-яa-z0-9])")


def _invalidate() -> None:
    with _CACHE_LOCK:
        _CACHE["at"] = 0.0


def _patterns(s) -> list:
    """[(entity_id, regex)] of the active cards; rebuilt when a card changed here or after a minute (tests: always)."""
    with _CACHE_LOCK:
        if time.time() - _CACHE["at"] < 60 and not _testing():
            return _CACHE["patterns"]
    out = []
    for e in s.query(Entity).filter(Entity.active.is_(True), Entity.merged_into_id.is_(None)):
        rx = pattern(_names(e))
        if rx is not None:
            out.append((e.id, rx))
    with _CACHE_LOCK:
        _CACHE.update(at=time.time(), patterns=out)
    return out


def _sentences(text: str) -> list:
    return [x.strip() for x in re.split(r"(?<=[.!?…])\s+|\n+", str(text or "")) if x.strip()]


def mentions(s, text: str, only=None) -> dict:
    """{entity_id: quote} for the cards the text speaks about (the quote is the sentence with the name)."""
    flat = _plain(text)
    if not flat:
        return {}
    found = {}
    for entity_id, rx in _patterns(s):
        if only is not None and entity_id not in only:
            continue
        if not rx.search(flat):
            continue
        quote = next((x for x in _sentences(text) if rx.search(_plain(x))), "")
        found[entity_id] = quote[:300]
    return found


def resolve(s, entity):
    """The card a merged duplicate went into."""
    seen = set()
    while entity is not None and entity.merged_into_id and entity.id not in seen:
        seen.add(entity.id)
        entity = s.get(Entity, entity.merged_into_id)
    return entity


def find(s, name: str = "", unp: str = ""):
    """The card with this name (any spelling, without the legal form) or this УНП."""
    unp = re.sub(r"\D", "", unp or "")
    k = key(name)
    for e in s.query(Entity).filter(Entity.merged_into_id.is_(None)):
        if unp and len(unp) == 9 and re.sub(r"\D", "", e.unp or "") == unp:
            return e
        if k and k in _names(e):
            return e
    return None


def _source(e, field: str, source: dict) -> None:
    sources = dict(e.sources or {})
    sources[field] = source
    e.sources = sources


def own(name: str) -> bool:
    """Our own company («СтройДом», «ООО «СтройДом»» when the profile says «СтройДом») is not a client card."""
    mine = key(str(settings_store.get("company_name") or ""))
    return bool(mine) and key(name) == mine


def get_or_create(s, name: str, kind: str = "org", source: str = "manual", unp: str = "", origin=None):
    """The card for this organization or object: an existing one (the same name without the legal form or the same
    УНП — then a new spelling is kept as an alias), or a new one. `origin` is the source of the name and the УНП.
    None for a name too short to tell apart and for our own company."""
    name = " ".join(str(name or "").split())[:300]
    if len(key(name)) < MIN_NAME or own(name):
        return None
    e = find(s, name, unp)
    if e is None:
        e = Entity(kind=kind if kind in KINDS else "org", name=name, aliases=[], source=source)
        s.add(e)
        s.flush()
        if origin:
            _source(e, "name", origin)
        _invalidate()
    elif name.lower() != (e.name or "").lower() and name.lower() not in [a.lower() for a in e.aliases or []]:
        e.aliases = list(e.aliases or []) + [name]
        _invalidate()
    if kind in ("client", "object", "contractor") and e.kind == "org":
        e.kind = kind
    digits = re.sub(r"\D", "", unp or "")
    if digits and not e.unp:
        e.unp = digits[:20]
        if origin:
            _source(e, "unp", origin)
    return e


def link(s, entity_id: int, source_type: str, source_id: int, quote: str = "") -> bool:
    if source_type not in SOURCE_TYPES or not source_id:
        return False
    if s.query(EntityLink.id).filter_by(entity_id=entity_id, source_type=source_type, source_id=source_id).first():
        return False
    s.add(EntityLink(entity_id=entity_id, source_type=source_type, source_id=int(source_id), quote=(quote or "")[:600]))
    return True


def link_text(s, source_type: str, source_id: int, text: str, only=None) -> int:
    added = 0
    for entity_id, quote in mentions(s, text, only).items():
        added += link(s, entity_id, source_type, source_id, quote)
    return added


# ----------------------------------------------------------------------------- where cards come from
def from_document(document_id: int) -> None:
    """After the intake: the counterparty of the document gets a card (with the УНП and its quote), the object is linked
    when its card exists, and the document is linked to every card it mentions."""
    from . import doc_intake
    with SessionLocal() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            return
        facts = doc.facts or {}
        fields = facts.get("fields") or {}
        origin = {"type": "document", "id": doc.id, "title": doc.filename or ""}
        party = fields.get("counterparty") or {}
        if party.get("value"):
            unp = (fields.get("unp") or {}).get("value") or ""
            e = get_or_create(s, party["value"], "org", "document", unp,
                              origin={**origin, "quote": party.get("quote") or ""})
            if e is not None:
                if unp and (e.sources or {}).get("unp", {}).get("id") == doc.id:
                    _source(e, "unp", {**origin, "quote": (fields.get("unp") or {}).get("quote") or ""})
                link(s, e.id, "document", doc.id, f"{doc_intake.line(facts)} — «{party.get('quote') or ''}»")
        place = fields.get("object") or {}
        if place.get("value"):
            # Objects are written in documents in many ways («ж/д по ул. Ленина, 5», «объект: Ленина 5»), so a card is
            # not made from a document by itself: an existing one is linked, a new one is made on the page or comes
            # from a meeting.
            e = resolve(s, find(s, place["value"]))
            if e is not None:
                link(s, e.id, "document", doc.id, place.get("quote") or "")
        s.flush()
        link_text(s, "document", doc.id, f"{doc.filename}\n{doc.caption or ''}\n{(doc.text or '')[:20000]}")
        s.commit()


def meeting_text(m) -> str:
    from . import analyze
    report = m.report if isinstance(m.report, dict) else {}
    parts = [m.title or "", str(report.get("summary") or "")]
    parts += [x.get("text") if isinstance(x, dict) else str(x) for x in report.get("decisions") or []]
    if isinstance(m.transcript, list):
        parts.append(analyze.transcript_text(m.transcript))
    return "\n".join(p for p in parts if p)


def from_meeting(meeting_id: int) -> None:
    """An approved meeting: it and its tasks are linked to the cards they mention."""
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if m is None:
            return
        link_text(s, "meeting", m.id, meeting_text(m))
        for t in m.tasks:
            link_text(s, "task", t.id, f"{t.title}\n{t.description or ''}\n{t.source_quote or ''}")
        s.commit()


def _marks() -> dict:
    return dict(settings_store.get("entity_scan") or {})


def run_pending() -> None:
    """Background loop: new messages and tasks are linked to the cards they mention."""
    marks, changed = _marks(), False
    with SessionLocal() as s:
        if not _patterns(s):
            last_m = s.query(ConversationMessage.id).order_by(ConversationMessage.id.desc()).first()
            last_t = s.query(Task.id).order_by(Task.id.desc()).first()
            fresh = {"message": last_m[0] if last_m else 0, "task": last_t[0] if last_t else 0}
            if {k: marks.get(k) for k in fresh} != fresh:
                settings_store.set_many({"entity_scan": {**marks, **fresh}})
            return  # nothing to look for (a new card scans the history itself)
        rows = (s.query(ConversationMessage).filter(ConversationMessage.id > int(marks.get("message") or 0),
                                                   ConversationMessage.role.in_(("user", "assistant")))
                .order_by(ConversationMessage.id).limit(SCAN_BATCH).all())
        for row in rows:
            link_text(s, "message", row.id, row.text or "")
        if rows:
            marks["message"], changed = rows[-1].id, True
        tasks = (s.query(Task).filter(Task.id > int(marks.get("task") or 0)).order_by(Task.id).limit(SCAN_BATCH).all())
        for t in tasks:
            link_text(s, "task", t.id, f"{t.title}\n{t.description or ''}\n{t.source_quote or ''}")
        if tasks:
            marks["task"], changed = tasks[-1].id, True
        s.commit()
    if changed:
        settings_store.set_many({"entity_scan": marks})


def rescan(entity_ids=None) -> int:
    """Look for these cards (all of them when None) in the whole history: meetings, tasks, documents, messages."""
    with SessionLocal() as s:
        only = set(entity_ids) if entity_ids else None
        added = 0
        for m in s.query(Meeting).order_by(Meeting.id.desc()).limit(500):
            added += link_text(s, "meeting", m.id, meeting_text(m), only)
        for t in s.query(Task).order_by(Task.id.desc()).limit(3000):
            added += link_text(s, "task", t.id, f"{t.title}\n{t.description or ''}\n{t.source_quote or ''}", only)
        for d in s.query(Document).order_by(Document.id.desc()).limit(1000):
            added += link_text(s, "document", d.id, f"{d.filename}\n{d.caption or ''}\n{(d.text or '')[:20000]}", only)
        for row in (s.query(ConversationMessage).filter(ConversationMessage.role.in_(("user", "assistant")))
                    .order_by(ConversationMessage.id.desc()).limit(5000)):
            added += link_text(s, "message", row.id, row.text or "", only)
        s.commit()
    return added


def _rescan_soon(entity_id: int) -> None:
    if _testing():
        rescan([entity_id])
    else:
        threading.Thread(target=rescan, args=([entity_id],), daemon=True, name="entity-scan").start()


# ----------------------------------------------------------------------------- the panel: editing and merging
def _clean_aliases(raw, name: str) -> list:
    items = raw if isinstance(raw, list) else re.split(r"[,;\n]", str(raw or ""))
    out = []
    for a in items:
        a = " ".join(str(a).split())[:200]
        if a and a.lower() != name.lower() and a.lower() not in [x.lower() for x in out]:
            out.append(a)
    return out[:30]


def save(entity_id=None, *, name: str, kind: str = "org", aliases="", unp: str = "", address: str = "",
         contacts: str = "", description: str = "", by: str = "") -> int:
    name = " ".join(str(name or "").split())[:300]
    if len(key(name)) < MIN_NAME:
        raise EntityError("название слишком короткое (без ООО, ОАО и кавычек нужно хотя бы 3 буквы)")
    with SessionLocal() as s:
        e = s.get(Entity, int(entity_id)) if entity_id else None
        if entity_id and e is None:
            raise EntityError("карточка не найдена")
        alias_list = _clean_aliases(aliases, name)
        mine = {key(name)} | {key(a) for a in alias_list}
        for other in s.query(Entity).filter(Entity.merged_into_id.is_(None)):
            if e is not None and other.id == e.id:
                continue
            same = mine & set(_names(other))
            if same:
                raise EntityError(f"такое название уже у карточки «{other.name}» — объедините их на её странице")
        digits = re.sub(r"\D", "", unp or "")
        if e is None:
            e = Entity(source="manual", aliases=[])
            s.add(e)
        for field, new in (("name", name), ("unp", digits[:20]), ("address", " ".join(str(address or "").split())[:300])):
            if new and new != (getattr(e, field) or ""):
                _source(e, field, {"type": "manual", "by": (by or "")[:120]})
        e.name, e.kind, e.aliases, e.unp = name, kind if kind in KINDS else "org", alias_list, digits[:20]
        e.address = " ".join(str(address or "").split())[:300]
        e.contacts, e.description = str(contacts or "").strip()[:2000], str(description or "").strip()[:4000]
        e.updated_by = (by or "")[:120]
        s.commit()
        entity_id = e.id
    _invalidate()
    _rescan_soon(entity_id)
    return entity_id


_KIND_WORDS = (("клиент", "client"), ("заказчик", "client"), ("объект", "object"), ("подряд", "contractor"),
               ("поставщик", "contractor"), ("организац", "org"))


def bulk_add(text: str, kind: str = "client", by: str = "") -> tuple:
    """«Список основных объектов и клиентов» pasted on the page: one per line, «Название», or with «;» (a tab, « — »)
    the УНП and the type («ОАО «Криница»; 100345678», «Склад на Заводской; объект»). Returns (new cards, already
    there); a known name only gets the УНП or the type it lacked."""
    added, found, new_ids = 0, 0, []
    kind = kind if kind in KINDS else "client"
    with SessionLocal() as s:
        for raw in str(text or "").splitlines()[:500]:
            parts = [p.strip(" \t-•*·") for p in re.split(r"\t|;|\s+[—–]\s+", raw)]
            parts = [p for p in parts if p]
            if not parts:
                continue
            unp = next((re.sub(r"\D", "", p) for p in parts[1:] if re.fullmatch(r"[\d\s]{9,11}", p)), "")
            this_kind = next((k for p in parts[1:] for word, k in _KIND_WORDS if p.lower().startswith(word)), kind)
            before = find(s, parts[0], unp)
            e = get_or_create(s, parts[0], this_kind, "manual", unp, origin={"type": "manual", "by": (by or "")[:120]})
            if e is None:
                continue
            if before is None:
                added += 1
                new_ids.append(e.id)
            else:
                found += 1
        s.commit()
    _invalidate()
    if new_ids:
        if _testing():
            rescan(new_ids)
        else:
            threading.Thread(target=rescan, args=(new_ids,), daemon=True, name="entity-scan").start()
    return added, found


def merge(keep_id: int, gone_id: int, by: str = "") -> None:
    """`gone` is a duplicate of `keep`: its spellings, links and facts move over, its card stays as a pointer."""
    if keep_id == gone_id:
        raise EntityError("нельзя объединить карточку с самой собой")
    with SessionLocal() as s:
        keep, gone = s.get(Entity, keep_id), s.get(Entity, gone_id)
        if keep is None or gone is None or keep.merged_into_id or gone.merged_into_id:
            raise EntityError("карточка не найдена или уже объединена")
        aliases = _clean_aliases(list(keep.aliases or []) + [gone.name] + list(gone.aliases or []), keep.name)
        keep.aliases = aliases
        for field in ("unp", "address", "contacts", "description"):
            if not (getattr(keep, field) or "") and (getattr(gone, field) or ""):
                setattr(keep, field, getattr(gone, field))
                if (gone.sources or {}).get(field):
                    _source(keep, field, gone.sources[field])
        if keep.kind == "org" and gone.kind != "org":
            keep.kind = gone.kind
        for l in s.query(EntityLink).filter_by(entity_id=gone.id).all():
            if not s.query(EntityLink.id).filter_by(entity_id=keep.id, source_type=l.source_type,
                                                    source_id=l.source_id).first():
                l.entity_id = keep.id
            else:
                s.delete(l)
        s.query(Note).filter(Note.entity_id == gone.id).update({"entity_id": keep.id}, synchronize_session=False)
        gone.merged_into_id, gone.active, gone.updated_by = keep.id, False, (by or "")[:120]
        keep.updated_by = (by or "")[:120]
        s.commit()
    _invalidate()
    _rescan_soon(keep_id)


def delete(entity_id: int) -> None:
    with SessionLocal() as s:
        e = s.get(Entity, entity_id)
        if e is None:
            raise EntityError("карточка не найдена")
        s.query(EntityLink).filter_by(entity_id=e.id).delete()
        s.query(Note).filter(Note.entity_id == e.id).update({"entity_id": None}, synchronize_session=False)
        s.query(Entity).filter(Entity.merged_into_id == e.id).update({"merged_into_id": None}, synchronize_session=False)
        s.delete(e)
        s.commit()
    _invalidate()


def add_fact(entity_id: int, text: str, by: str = "") -> int:
    text = " ".join(str(text or "").split())[:1500]
    if not text:
        raise EntityError("пустой факт")
    with SessionLocal() as s:
        if s.get(Entity, entity_id) is None:
            raise EntityError("карточка не найдена")
        note = Note(entity_id=entity_id, kind="company", text=text, source="manual", author=(by or "")[:80],
                    confidence="high")
        s.add(note)
        s.commit()
        return note.id


def duplicates(s, limit: int = 20) -> list:
    """Possible duplicates the code did not merge by itself: a typo, one name inside the other («Криница» and
    «Криница-Минск»), the same УНП written differently."""
    cards = [e for e in s.query(Entity).filter(Entity.active.is_(True), Entity.merged_into_id.is_(None)).order_by(Entity.id)]
    out = []
    for i, a in enumerate(cards):
        for b in cards[i + 1:]:
            why = ""
            for ka in _names(a):
                for kb in _names(b):
                    if difflib.SequenceMatcher(None, ka, kb).ratio() >= 0.84:
                        why = "похожие названия"
                    elif min(len(ka), len(kb)) >= 5 and (f" {ka} " in f" {kb} " or f" {kb} " in f" {ka} "):
                        why = "одно название входит в другое"
                    if why:
                        break
                if why:
                    break
            if why:
                out.append({"a": {"id": a.id, "name": a.name, "kind": KINDS.get(a.kind, a.kind)},
                            "b": {"id": b.id, "name": b.name, "kind": KINDS.get(b.kind, b.kind)}, "why": why})
            if len(out) >= limit:
                return out
    return out


def _source_ru(src: dict) -> str:
    if not src:
        return ""
    if src.get("type") == "document":
        return f"документ «{src.get('title') or src.get('id')}»"
    if src.get("type") == "meeting":
        return f"совещание «{src.get('title') or src.get('id')}»"
    return "вручную" + (f" ({src['by']})" if src.get("by") else "")


def listing(s, q: str = "", kind: str = "") -> list:
    counts = {}
    for entity_id, source_type in s.query(EntityLink.entity_id, EntityLink.source_type):
        counts.setdefault(entity_id, {}).setdefault(source_type, 0)
        counts[entity_id][source_type] += 1
    facts = {}
    for (entity_id,) in s.query(Note.entity_id).filter(Note.entity_id.isnot(None), Note.archived.is_(False)):
        facts[entity_id] = facts.get(entity_id, 0) + 1
    needle = key(q) if q.strip() else ""
    out = []
    for e in s.query(Entity).filter(Entity.merged_into_id.is_(None)).order_by(Entity.name):
        if kind and e.kind != kind:
            continue
        if needle and not any(needle in n for n in _names(e)) and needle not in _plain(e.description or ""):
            continue
        out.append({"id": e.id, "name": e.name, "kind": e.kind, "kind_ru": KINDS.get(e.kind, e.kind),
                    "aliases": list(e.aliases or []), "unp": e.unp or "", "active": e.active,
                    "links": counts.get(e.id, {}), "facts": facts.get(e.id, 0), "source": e.source or ""})
    return out


def _source_link(s, source_type: str, source_id: int) -> dict:
    from . import doc_intake
    if source_type == "meeting":
        m = s.get(Meeting, source_id)
        return {"title": (m.title or f"Совещание {m.id}") if m else "удалено", "url": f"/meetings/{source_id}" if m else "",
                "date": m.meeting_date or m.created_at if m else None}
    if source_type == "task":
        t = s.get(Task, source_id)
        return {"title": t.title if t else "удалена", "url": f"/meetings/{t.meeting_id}" if t and t.meeting_id else "",
                "date": t.deadline if t else None, "who": (t.assignee_name or "") if t else "",
                "status": t.status if t else ""}
    if source_type == "document":
        d = s.get(Document, source_id)
        return {"title": d.filename if d else "удалён", "url": f"/documents/{source_id}/view" if d else "",
                "date": d.created_at if d else None, "line": doc_intake.line(d.facts or {}) if d and d.facts else ""}
    row = s.get(ConversationMessage, source_id)
    return {"title": "сообщение" if row else "удалено", "url": "", "date": row.created_at if row else None,
            "who": ("ассистент" if row and row.role == "assistant" else "человек") if row else ""}


def card(s, entity_id: int) -> dict:
    e = s.get(Entity, entity_id)
    if e is None:
        raise EntityError("карточка не найдена")
    target = resolve(s, e)
    links = {k: [] for k in SOURCE_TYPES}
    for l in s.query(EntityLink).filter_by(entity_id=e.id).order_by(EntityLink.id.desc()).limit(400):
        links[l.source_type].append({"id": l.source_id, "quote": l.quote or "", **_source_link(s, l.source_type, l.source_id)})
    facts = []
    for n in s.query(Note).filter(Note.entity_id == e.id).order_by(Note.id.desc()).limit(100):
        m = s.get(Meeting, n.meeting_id) if n.meeting_id else None
        facts.append({"id": n.id, "text": n.text, "quote": n.quote or "", "archived": n.archived,
                      "source": (f"совещание «{m.title}»" if m else {"manual": "вручную", "conversation": "переписка",
                                                                         "system": "система"}.get(n.source, n.source))
                                + (f" ({n.author})" if n.source == "manual" and n.author else ""),
                      "url": f"/meetings/{m.id}" if m else "", "at": n.created_at})
    sources = e.sources or {}
    others = [{"id": o.id, "name": o.name} for o in s.query(Entity).filter(Entity.merged_into_id.is_(None), Entity.id != e.id)
              .order_by(Entity.name).limit(500)]
    return {"id": e.id, "name": e.name, "kind": e.kind, "kind_ru": KINDS.get(e.kind, e.kind), "aliases": list(e.aliases or []),
            "unp": e.unp or "", "address": e.address or "", "contacts": e.contacts or "", "description": e.description or "",
            "active": e.active, "merged_into": {"id": target.id, "name": target.name} if target is not e and target else None,
            "sources": {k: _source_ru(v) for k, v in sources.items()},
            "source_urls": {k: (f"/documents/{v['id']}/view" if v.get("type") == "document" else "")
                            for k, v in sources.items()},
            "links": links, "facts": facts, "others": others, "by": e.updated_by or "", "updated_at": e.updated_at}


# ----------------------------------------------------------------------------- the conversation and the memory
def for_prompt(s, text: str, full: bool, limit: int = 3) -> list:
    """The cards of the clients and objects the message speaks about, for the conversation model: facts with their
    source; meetings, open tasks and documents only for people who see the whole team."""
    from . import doc_intake, memory
    found = mentions(s, text)
    out = []
    for entity_id in list(found)[:limit]:
        e = s.get(Entity, entity_id)
        if e is None:
            continue
        view = {"name": e.name, "kind": KINDS.get(e.kind, e.kind), "also_called": list(e.aliases or [])[:6] or None,
                "unp": e.unp or None, "address": e.address or None, "contacts": e.contacts or None,
                "description": e.description or None, "url": f"/entities/{e.id}"}
        facts = []
        for n in (s.query(Note).filter(Note.entity_id == e.id, Note.archived.is_(False)).order_by(Note.id.desc()).limit(8)):
            m = s.get(Meeting, n.meeting_id) if n.meeting_id else None
            day = (m.meeting_date or (m.created_at.date() if m.created_at else None)) if m else None
            if m is not None:  # the source of the answer (№20): the meeting with its day, or the card itself
                facts.append({"text": n.text, "cite": f"совещание {memory.quoted(m.title)}" + (f" от {day.strftime('%d.%m.%Y')}" if day else ""),
                              "url": f"/meetings/{m.id}"})
            elif n.source == "conversation" and n.created_at:
                facts.append({"text": n.text, "cite": f"переписка от {n.created_at.strftime('%d.%m.%Y')}",
                              "url": f"/entities/{e.id}"})
            else:
                facts.append({"text": n.text, "cite": f"карточка {memory.quoted(e.name)}", "url": f"/entities/{e.id}"})
        view["facts"] = facts
        if full:
            links = s.query(EntityLink).filter_by(entity_id=e.id).order_by(EntityLink.id.desc()).limit(200).all()
            meetings = [s.get(Meeting, l.source_id) for l in links if l.source_type == "meeting"][:3]
            view["meetings"] = [{"title": m.title, "date": m.meeting_date.isoformat() if m.meeting_date else None}
                                for m in meetings if m is not None]
            tasks = [s.get(Task, l.source_id) for l in links if l.source_type == "task"]
            view["open_tasks"] = [{"title": t.title, "who": t.assignee_name or None,
                                   "deadline": t.deadline.isoformat() if t.deadline else None, "status": t.status}
                                  for t in tasks if t is not None and t.status not in ("done", "assistant_done")][:5]
            docs = [s.get(Document, l.source_id) for l in links if l.source_type == "document"][:5]
            view["documents"] = [{"id": d.id, "filename": d.filename, "what": doc_intake.line(d.facts or {}) or None,
                                  "date": d.created_at.date().isoformat() if d.created_at else None}
                                 for d in docs if d is not None and not d.sensitive]
        out.append({k: v for k, v in view.items() if v not in (None, [], "")})
    return out


def memory_items(s) -> list:
    """The cards as memory items (memory.items): search finds «какой УНП у Криницы», «адрес объекта на Ленина»."""
    out = []
    for e in s.query(Entity).filter(Entity.active.is_(True), Entity.merged_into_id.is_(None)):
        parts = [f"{KINDS.get(e.kind, 'организация').lower()} {e.name}"]
        if e.aliases:
            parts.append("также: " + ", ".join(e.aliases))
        for label, v in (("УНП", e.unp), ("адрес", e.address), ("контакты", e.contacts), ("описание", e.description)):
            if v:
                parts.append(f"{label}: {v}")
        out.append({"key": f"entity:{e.id}", "type": "entity", "title": e.name, "text": "; ".join(parts), "owner": None,
                    "url": f"/entities/{e.id}"})
    return out


def names(s) -> dict:
    """{entity_id: name} of the cards, for the notes in memory."""
    return {e.id: e.name for e in s.query(Entity.id, Entity.name)}
