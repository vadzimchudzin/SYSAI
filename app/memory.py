"""The AI employee's memory: the company, departments, people, notes, skills and documents, and search over them.

Every item has a source. Search is hybrid: word matching that understands Russian endings (works without any model)
plus embeddings when a key is set (cosine over vectors kept in memory_vectors). The best items go into the prompts of the
conversation and of the cases, filtered by what the person in the conversation may see.

After an approved meeting the assistant adds facts to memory itself. A fact is kept only with the words it rests on
(the quote must be in the transcript), only about people and departments from the directory, never as a judgement of a
person or about health, religion, politics and the like (personal data law: facts, source, can be viewed and corrected).
"""
import collections
import concurrent.futures
import hashlib
import json
import logging
import math
import re

from . import config, llm, settings_store
from .db import (Department, Document, DocumentChunk, Employee, Meeting, MemoryVector, Note, SessionLocal, Skill,
                 now)

log = logging.getLogger("sysai.memory")

NOTE_KINDS = ("fact", "status", "commitment", "done", "company")
CONFIDENCE = ("high", "medium", "low")
CONFIDENCE_RU = {"high": "высокая", "medium": "средняя", "low": "низкая"}
SOURCE_RU = {"meeting": "совещание", "conversation": "переписка", "manual": "вручную", "system": "система"}
TYPE_RU = {"company": "Компания", "department": "Отдел", "person": "Сотрудник", "note": "Заметка", "skill": "Навык",
           "document": "Документ"}
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory")
_WORD = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
_STOP = set("и в во на с со к ко о об от до за из у по для что кто как где когда ли же бы не ни а но или это этот эта "
            "эти тот та те его ее её их им ему ей мы вы он она они я ты мне меня нас вас при про над под без так "
            "там тут уже еще ещё все всё весь вся быть есть был была были будет нужно надо можно".split())
# Personal-data guard for automatic facts: no judgements of people and no special categories.
_JUDGEMENT = ("ленив", "безответствен", "некомпетент", "глуп", "туп", "токсичн", "неадекват", "бестолк", "халтур",
              "плохо работает", "плохой работник", "хороший работник", "лучший сотрудник", "худший", "раздража",
              "истеричн", "конфликтн")
_SPECIAL = ("болен", "болезн", "болеет", "диагноз", "беремен", "религи", "верующ", "политич", "партии", "националь",
            "ориентац", "судим", "инвалид", "лечени", "больниц")


def _testing() -> bool:
    return config.TESTING


# ----------------------------------------------------------------------------- words
def stem(word: str) -> str:
    """A crude Russian stem: the word without a common ending (enough for «сверки» ~ «сверку» ~ «сверка»)."""
    w = word.lower().replace("ё", "е")
    if len(w) <= 4:
        return w
    for ending in ("иями", "ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ией", "ией", "иях", "ах", "ях",
                   "ом", "ем", "ой", "ей", "ий", "ый", "ая", "яя", "ое", "ее", "ые", "ие", "ую", "юю", "ов", "ев",
                   "ия", "ие", "ии", "ть", "ла", "ли", "ет", "ит", "ут", "ют", "ат", "ят", "а", "я", "ы", "и", "у",
                   "ю", "е", "о", "ь"):
        if w.endswith(ending) and len(w) - len(ending) >= 4:
            return w[:-len(ending)][:7]
    return w[:7]


def terms(text: str) -> list:
    return [stem(w) for w in _WORD.findall(text or "") if w.lower() not in _STOP and len(w) > 1]


# ----------------------------------------------------------------------------- items
def _person_text(e) -> str:
    parts = [e.name]
    if e.aliases:
        parts.append(f"также: {e.aliases}")
    if e.position:
        parts.append(f"должность: {e.position}")
    for m in e.department_memberships:
        if m.department is not None:
            parts.append(f"отдел {m.department.name}" + (f", роль {m.role}" if m.role else "") +
                         (f", обязанности в отделе: {m.responsibilities}" if m.responsibilities else ""))
    if e.department and not e.department_memberships:
        parts.append(f"отдел: {e.department}")
    for label, value in (("отвечает за", e.responsibilities), ("компетенции", e.competencies), ("контекст", e.context),
                         ("как с ним общаться", getattr(e, "communication_style", ""))):
        if value:
            parts.append(f"{label}: {value}")
    return "; ".join(parts)


def items(s) -> list:
    """Everything the memory knows, as uniform items: key, type, title, text, owner (employee id) and meta."""
    out = []
    profile = str(settings_store.get("company_profile") or "").strip()
    company = str(settings_store.get("company_name") or "").strip()
    for i, part in enumerate(p.strip() for p in re.split(r"\n\s*\n", profile) if p.strip()):
        # The first paragraph is the general profile (the panel asks for «чем занимается компания» first).
        title = f"Компания {company}".strip() + (": чем занимается, профиль" if i == 0 else "")
        out.append({"key": f"company:{i}", "type": "company", "title": title, "text": part,
                    "owner": None, "url": "/memory"})
    for d in s.query(Department).filter(Department.active.is_(True)).all():
        members = ", ".join(f"{m.employee.name}" + (f" ({m.role})" if m.role else "") for m in d.memberships
                            if m.employee is not None and m.employee.active)
        text = "; ".join(x for x in (f"отдел {d.name}", d.description and f"функции: {d.description}",
                                     d.regulations and f"регламент: {d.regulations}",
                                     members and f"сотрудники: {members}") if x)
        out.append({"key": f"department:{d.id}", "type": "department", "title": d.name, "text": text, "owner": None,
                    "url": "/memory#departments"})
    people = {e.id: e for e in s.query(Employee).filter(Employee.active.is_(True)).all()}
    for e in people.values():
        out.append({"key": f"person:{e.id}", "type": "person", "title": e.name, "text": _person_text(e),
                    "owner": e.id, "url": f"/memory/people/{e.id}"})
    departments = {d.id: d.name for d in s.query(Department).all()}
    for n in (s.query(Note).filter(Note.archived.is_(False), Note.kind.in_(NOTE_KINDS))
              .order_by(Note.id.desc()).limit(3000)):
        subject = (people[n.employee_id].name if n.employee_id in people else
                   (f"отдел {departments[n.department_id]}" if n.department_id in departments else "компания"))
        out.append({"key": f"note:{n.id}", "type": "note", "title": subject, "text": f"{subject}: {n.text}",
                    "owner": n.employee_id, "url": f"/memory/people/{n.employee_id}" if n.employee_id else "/memory",
                    "meta": {"source": SOURCE_RU.get(n.source, n.source), "date": n.created_at,
                             "confidence": CONFIDENCE_RU.get(n.confidence or "medium", n.confidence)}})
    for k in s.query(Skill).filter(Skill.active.is_(True)).all():
        out.append({"key": f"skill:{k.id}", "type": "skill", "title": k.title,
                    "text": f"навык «{k.title}»; когда: {k.when_to_use}; {k.body}", "owner": None, "url": "/skills"})
    docs = {d.id: d for d in s.query(Document.id, Document.filename, Document.employee_id, Document.created_at)}
    for c in s.query(DocumentChunk).order_by(DocumentChunk.document_id, DocumentChunk.position).limit(5000):
        d = docs.get(c.document_id)
        if d is None:
            continue
        out.append({"key": f"chunk:{c.id}", "type": "document", "title": d.filename,
                    "text": f"документ «{d.filename}»: {c.text}", "owner": d.employee_id,
                    "url": f"/documents/{d.id}/view", "meta": {"document_id": d.id, "date": d.created_at}})
    return out


def visible(item: dict, viewer) -> bool:
    """`viewer` None = everything (director, reviewers, the panel). An employee sees the company, departments and
    skills, their own card, notes and documents, and only the name, position and department of colleagues."""
    if viewer is None or item["type"] in ("company", "department", "skill"):
        return True
    return item.get("owner") == viewer


# ----------------------------------------------------------------------------- vectors
def _hash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


def _model() -> str:
    return str(settings_store.get("embedding_model") or "").strip()


def ensure_vectors(s, pool: list, budget: int = 400) -> dict:
    """key -> vector for the items; missing or changed ones are made (at most `budget` per call)."""
    model = _model()
    if not model or not config.llm_enabled():
        return {}
    stored = {v.key: v for v in s.query(MemoryVector).filter(MemoryVector.key.in_([i["key"] for i in pool])).all()} \
        if pool else {}
    todo = [i for i in pool if i["key"] not in stored or stored[i["key"]].hash != _hash(i["text"])
            or stored[i["key"]].model != model][:budget]
    for start in range(0, len(todo), 64):
        batch = todo[start:start + 64]
        try:
            vectors = llm.embed([i["text"][:2000] for i in batch], model)
        except llm.LLMError as exc:
            log.info("memory vectors not made now: %s", exc)
            break
        for item, vector in zip(batch, vectors):
            row = stored.get(item["key"]) or MemoryVector(key=item["key"])
            row.hash, row.model, row.vector = _hash(item["text"]), model, vector
            s.merge(row)
            stored[item["key"]] = row
        s.commit()
    return {k: v.vector for k, v in stored.items() if v.model == model}


def _cos(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ----------------------------------------------------------------------------- search
def search(query: str, k: int = 8, viewer=None, types=None, s=None, pool=None) -> list:
    """Best items for the query: words (BM25 on stems) + meaning (embeddings, when available)."""
    own = s is None
    s = s or SessionLocal()
    try:
        pool = [i for i in (pool if pool is not None else items(s))
                if visible(i, viewer) and (not types or i["type"] in types)]
        q = terms(query)
        if not pool or not (q or query.strip()):
            return []
        docs = [terms(i["title"] + " " + i["text"]) for i in pool]
        df = collections.Counter(t for d in docs for t in set(d))
        avg = sum(len(d) for d in docs) / len(docs) or 1
        lexical = []
        for d in docs:
            tf = collections.Counter(d)
            score = 0.0
            for t in set(q):
                if t in tf:
                    idf = math.log(1 + (len(docs) - df[t] + 0.5) / (df[t] + 0.5))
                    score += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / avg))
            lexical.append(score)
        top = max(lexical) or 1.0
        scores = [x / top for x in lexical]
        vectors = ensure_vectors(s, pool)
        if vectors:
            try:
                qv = llm.embed([query[:2000]], _model())[0]
                sem = [_cos(qv, vectors[i["key"]]) if i["key"] in vectors else 0.0 for i in pool]
                low, high = min(sem), max(sem)
                span = (high - low) or 1.0
                scores = [0.45 * lx + 0.55 * ((sv - low) / span) for lx, sv in zip(scores, sem)]
            except llm.LLMError as exc:
                log.info("query vector not made: %s", exc)
        ranked = sorted(zip(scores, range(len(pool))), key=lambda x: -x[0])
        return [dict(pool[i], score=round(sc, 3)) for sc, i in ranked[:k] if sc > 0.05]
    finally:
        if own:
            s.close()


def for_prompt(query: str, viewer=None, k: int = 8) -> list:
    """Memory items for a model prompt: short, with the source, never failing the caller."""
    try:
        hits = search(query, k=k, viewer=viewer)
    except Exception:  # noqa: BLE001
        log.exception("memory search failed")
        return []
    out = []
    for h in hits:
        meta = h.get("meta") or {}
        entry = {"type": TYPE_RU[h["type"]], "about": h["title"], "text": h["text"][:700]}
        if meta.get("source"):
            entry["source"] = meta["source"]
        if meta.get("confidence"):
            entry["confidence"] = meta["confidence"]
        if meta.get("date"):
            entry["date"] = meta["date"].strftime("%d.%m.%Y")
        out.append(entry)
    return out


def skills_for(query: str, k: int = 2) -> list:
    """Scenarios and templates that fit a situation (a case message, a reply)."""
    try:
        hits = search(query, k=k, types=("skill",))
    except Exception:  # noqa: BLE001
        log.exception("skill search failed")
        return []
    with SessionLocal() as s:
        ids = [int(h["key"].split(":")[1]) for h in hits if h["score"] >= 0.25]
        rows = [s.get(Skill, i) for i in ids]
        for row in rows:
            row.uses += 1
        s.commit()
        return [{"title": r.title, "how": r.body[:1500]} for r in rows if r is not None]


# ----------------------------------------------------------------------------- questions about a document
def answer_about_document(document_id: int, question: str) -> dict:
    """Answer from the document's own text, with the pieces used. Raises llm.LLMError when the model is down."""
    with SessionLocal() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            raise ValueError("документ не найден")
        pool = [i for i in items(s) if i["type"] == "document" and (i.get("meta") or {}).get("document_id") == document_id]
        hits = search(question, k=5, s=s, pool=pool) or pool[:3]
        filename, status = doc.filename, doc.text_status
    if not hits:
        return {"answer": "В документе нет распознанного текста." if status != "pending" else
                "Документ ещё читается, спросите чуть позже.", "pieces": []}
    from . import voice
    pieces = [h["text"].split(": ", 1)[-1] for h in hits]
    raw = llm.chat(voice.model(), [
        {"role": "system", "content": "Ответь на вопрос только по фрагментам документа. Если ответа в них нет, так и "
                                      "скажи. Коротко, по-русски, цифры и даты — точно как в документе."},
        {"role": "user", "content": f"Документ «{filename}». Фрагменты:\n<data>" + "\n---\n".join(pieces) +
                                    f"</data>\nВопрос: {question}"}], temperature=0.0, max_tokens=600, timeout=90)
    return {"answer": raw.strip(), "pieces": pieces}


# ----------------------------------------------------------------------------- learning after a meeting
def _norm(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower().replace("ё", "е")))


def quote_found(quote: str, transcript: str) -> bool:
    """The quote must really be in the transcript (spelling and punctuation may differ a little)."""
    q, t = _norm(quote), _norm(transcript)
    if len(q.split()) < 3:
        return False
    if q in t:
        return True
    words = q.split()
    tset = set(t.split())
    return sum(w in tset for w in words) / len(words) >= 0.9 and " ".join(words[:3]) in t


def acceptable_fact(text: str) -> str:
    """'' when the fact may be kept, else the reason."""
    low = (text or "").lower()
    if len(low.strip()) < 8:
        return "слишком коротко"
    if any(w in low for w in _JUDGEMENT):
        return "оценка человека, а не факт"
    if any(w in low for w in _SPECIAL):
        return "особая категория персональных данных"
    return ""


def _similar(a: str, b: str) -> bool:
    x, y = set(terms(a)), set(terms(b))
    return bool(x and y) and len(x & y) / len(x | y) >= 0.6


def learn_from_meeting(meeting_id: int) -> int:
    """Add facts from an approved meeting. Returns how many were kept."""
    if not settings_store.get("memory_autolearn") or not config.llm_enabled() and not _testing():
        return 0
    from . import analyze, voice
    with SessionLocal() as s:
        meeting = s.get(Meeting, meeting_id)
        if meeting is None or not meeting.transcript:
            return 0
        if s.query(Note.id).filter_by(meeting_id=meeting_id, source="meeting", author="память").first():
            return 0  # already learned from this meeting
        transcript = analyze.transcript_text(meeting.transcript)
        people = {e.id: e for e in s.query(Employee).filter(Employee.active.is_(True)).all()}
        departments = {d.id: d for d in s.query(Department).filter(Department.active.is_(True)).all()}
        known = [n.text for n in s.query(Note).filter(Note.archived.is_(False)).order_by(Note.id.desc()).limit(300)]
        facts = {"meeting": meeting.title, "people": [{"id": e.id, "name": e.name, "position": e.position or None}
                                                       for e in people.values()],
                 "departments": [{"id": d.id, "name": d.name} for d in departments.values()],
                 "already_known": known[:80]}
    system = voice.system_prompt(
        "Выпиши из расшифровки совещания устойчивые факты для памяти ассистента: кто за что отвечает, кто с кем работает, "
        "какие правила и порядок работы в компании и отделах, контрагенты, объекты, предпочтения в общении. Не выписывай "
        "разовые поручения (они уже в задачах), оценки людей, здоровье, личную жизнь и то, что уже есть в already_known. "
        "Каждый факт — одно короткое утверждение в третьем лице с точной цитатой из расшифровки. "
        'JSON: {"facts":[{"subject":"person|department|company","employee_id":ID или null,"department_id":ID или null,'
        '"text":"...","quote":"дословно из расшифровки","confidence":"high|medium|low"}]} — не больше 12.')
    try:
        raw = llm.chat(voice.model(), [{"role": "system", "content": system},
                                       {"role": "user", "content": "<data>" + json.dumps(facts, ensure_ascii=False) +
                                        "</data>\nРасшифровка:\n<data>" + transcript[:60000] + "</data>"}],
                       json_mode=True, temperature=0.1, max_tokens=2500, timeout=180)
        proposed = (llm.parse_json(raw) or {}).get("facts") or []
    except Exception as exc:  # noqa: BLE001
        log.warning("memory not learned from meeting %s: %s", meeting_id, exc)
        return 0
    kept = 0
    with SessionLocal() as s:
        existing = [n.text for n in s.query(Note).filter(Note.archived.is_(False)).all()]
        for f in proposed[:12]:
            if not isinstance(f, dict):
                continue
            text, quote = str(f.get("text") or "").strip()[:600], str(f.get("quote") or "").strip()[:600]
            # Who the fact is about comes from the ids, checked against the directory (models write a name in
            # "subject" as often as the word "person").
            employee_id = f.get("employee_id") if isinstance(f.get("employee_id"), int) else None
            department_id = f.get("department_id") if isinstance(f.get("department_id"), int) else None
            if employee_id is not None and employee_id not in people or \
                    department_id is not None and department_id not in departments:
                log.info("memory fact skipped (unknown person or department): %s", text)
                continue
            subject = "person" if employee_id else ("department" if department_id else "company")
            if employee_id:
                department_id = None
            reason = acceptable_fact(text) or ("" if quote_found(quote, transcript) else "цитаты нет в расшифровке") or \
                ("уже известно" if any(_similar(text, x) for x in existing) else "")
            if reason:
                log.info("memory fact skipped (%s): %s", reason, text)
                continue
            confidence = f.get("confidence") if f.get("confidence") in CONFIDENCE else "medium"
            s.add(Note(employee_id=employee_id, department_id=department_id, meeting_id=meeting_id,
                       kind="company" if subject != "person" else "fact", text=text, quote=quote,
                       confidence=confidence, source="meeting", author="память"))
            existing.append(text)
            kept += 1
        s.commit()
    log.info("meeting %s: %s facts added to memory", meeting_id, kept)
    return kept


def schedule_learning(meeting_id: int) -> None:
    learn_from_meeting(meeting_id) if _testing() else POOL.submit(learn_from_meeting, meeting_id)


def refresh_vectors() -> None:
    """Background loop: new and changed memory items get their vectors ahead of the questions."""
    if not _model() or not config.llm_enabled():
        return
    with SessionLocal() as s:
        ensure_vectors(s, items(s), budget=128)


# ----------------------------------------------------------------------------- editing (the Memory page)
def add_note(*, text: str, employee_id=None, department_id=None, confidence="high", author="", quote="") -> int:
    reason = "" if author else acceptable_fact(text)
    if not text.strip() or reason:
        raise ValueError(reason or "пустая заметка")
    with SessionLocal() as s:
        note = Note(employee_id=employee_id, department_id=department_id, text=text.strip()[:1500], quote=quote[:600],
                    kind="fact" if employee_id else "company", source="manual", author=author[:80],
                    confidence=confidence if confidence in CONFIDENCE else "high")
        s.add(note)
        s.commit()
        return note.id


def edit_note(note_id: int, *, text=None, confidence=None, archived=None, by="") -> bool:
    with SessionLocal() as s:
        note = s.get(Note, note_id)
        if note is None:
            return False
        if text is not None:
            if not text.strip():
                return False
            note.text = text.strip()[:1500]
        if confidence in CONFIDENCE:
            note.confidence = confidence
        if archived is not None:
            note.archived = bool(archived)
        note.updated_at, note.edited_by = now(), by[:120]
        s.commit()
    return True
