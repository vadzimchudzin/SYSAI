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
           "document": "Документ", "entity": "Объект или клиент"}
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
    from . import entities
    cards = entities.names(s)
    out += entities.memory_items(s)
    notes = (s.query(Note).filter(Note.archived.is_(False), Note.kind.in_(NOTE_KINDS))
             .order_by(Note.id.desc()).limit(3000).all())
    said_at = {m.id: m for m in s.query(Meeting.id, Meeting.title, Meeting.meeting_date, Meeting.created_at)
               .filter(Meeting.id.in_({n.meeting_id for n in notes if n.meeting_id}))} if notes else {}
    for n in notes:
        subject = (people[n.employee_id].name if n.employee_id in people else
                   (f"отдел {departments[n.department_id]}" if n.department_id in departments else
                    cards.get(n.entity_id) or "компания"))
        url = (f"/memory/people/{n.employee_id}" if n.employee_id else
               f"/entities/{n.entity_id}" if n.entity_id in cards else "/memory")
        meta = {"source": SOURCE_RU.get(n.source, n.source), "date": n.created_at,
                "confidence": CONFIDENCE_RU.get(n.confidence or "medium", n.confidence)}
        m = said_at.get(n.meeting_id)
        if m is not None:  # the meeting it was said at: its name and day are the source of the answer (№20)
            meta.update(meeting=m.title or f"Совещание №{m.id}",
                        meeting_date=m.meeting_date or (m.created_at.date() if m.created_at else None),
                        link=f"/meetings/{m.id}")
        out.append({"key": f"note:{n.id}", "type": "note", "title": subject, "text": f"{subject}: {n.text}",
                    "owner": n.employee_id, "url": url, "meta": meta})
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


def _day_ru(day) -> str:
    return day.strftime("%d.%m.%Y") if day else ""


def quoted(name: str) -> str:
    """«Планёрка», but ОАО «Криница» as it is (no quotes inside quotes)."""
    name = str(name or "").strip()
    return name if ("«" in name or '"' in name) else f"«{name}»"


def cite_of(item: dict) -> str:
    """Where a memory item comes from, in the words of an answer: «совещание «Планёрка» от 12.11.2025»,
    «документ «счёт_15.pdf»», «профиль компании», «карточка сотрудника Анна Петрова»."""
    meta = item.get("meta") or {}
    kind, title = item["type"], item.get("title") or ""
    if kind == "company":
        return "профиль компании"
    if kind == "department":
        return f"карточка отдела {quoted(title)}"
    if kind == "person":
        return f"карточка сотрудника {title}"
    if kind == "entity":
        return f"карточка {quoted(title)}"
    if kind == "skill":
        return f"навык {quoted(title)}"
    if kind == "document":
        return f"документ {quoted(title)}"
    day = meta.get("date")
    day = day.date() if hasattr(day, "date") else day
    if meta.get("meeting"):
        when = _day_ru(meta.get("meeting_date"))
        return f"совещание {quoted(meta['meeting'])}" + (f" от {when}" if when else "")
    word = "переписка" if meta.get("source") == SOURCE_RU["conversation"] else "заметка"
    return word + (f" от {_day_ru(day)}" if day else "")


def for_prompt(query: str, viewer=None, k: int = 8) -> list:
    """Memory items for a model prompt: short, each with a ref and its source (cite), never failing the caller.
    `url` (the panel page of the source) is taken out by number_sources before the model sees the prompt."""
    try:
        hits = search(query, k=k, viewer=viewer)
    except Exception:  # noqa: BLE001
        log.exception("memory search failed")
        return []
    out = []
    for i, h in enumerate(hits, 1):
        meta = h.get("meta") or {}
        entry = {"ref": f"m{i}", "type": TYPE_RU[h["type"]], "about": h["title"], "text": h["text"][:700],
                 "cite": cite_of(h)}
        if meta.get("confidence"):
            entry["confidence"] = meta["confidence"]
        if meta.get("date"):
            entry["date"] = meta["date"].strftime("%d.%m.%Y")
        if meta.get("link") or h.get("url"):
            entry["url"] = meta.get("link") or h["url"]
        out.append(entry)
    return out


# ----------------------------------------------------------------------------- sources in the answers (№20)
FALLBACK_CITE = {"Компания": "профиль компании", "Отдел": "карточка отдела {}", "Сотрудник": "карточка сотрудника {}",
                 "Документ": "документ {}", "Объект или клиент": "карточка {}", "Навык": "навык {}"}
_MARK = re.compile(r"[ \t]*[\[【]\s*([mkмк]\d+(?:\.\d+)?(?:\s*[,;]\s*[mkмк]?\d+(?:\.\d+)?)*)\s*[\]】]", re.I)
_OWN_LINE = re.compile(r"(^|\n)\s*источники?\s*:", re.I)
_NAMED = re.compile(r"совещани|планёрк|планерк|протокол|по документу|в документе|в заметк|в переписк|в карточк|"
                    r"по карточк|в профил|по профил|источник", re.I)
_DONT_KNOW = re.compile(r"не знаю|нет (данных|информац|сведений)|не наш(ё|е)л|не нашлось|не могу сказать|не помню|"
                        r"нет в памяти|не указан", re.I)
_QUESTION = re.compile(r"\?|^\W*(кто|что|чем|когда|где|куда|откуда|сколько|как|какой|какая|какое|какие|каков|почему|"
                       r"зачем|чей|чья|чьё|чье|есть ли|правда ли|подскажи|напомни|скажи|расскажи|уточни)\b", re.I)
_NUM = re.compile(r"\d+(?:[.,:/-]\d+)*")
MAX_CITES = 3


def _plain(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _entity_cite(source, card: dict) -> str:
    """The source of a fact of a card: a meeting with its day, otherwise the card itself."""
    source = str(source or "")
    found = re.match(r"(совещание «[^»]*»)\s*(?:от\s*)?(\d{4})-(\d{2})-(\d{2})", source)
    if found:
        return f"{found.group(1)} от {found.group(4)}.{found.group(3)}.{found.group(2)}"
    if source.startswith("совещание"):
        return source
    return f"карточка {quoted(card.get('name') or '')}"


def number_sources(ctx: dict) -> dict:
    """Every record the model may answer from gets a short ref and the words of its source (cite): known_facts m1,
    m2…, the cards of about_entities k1, k2… and their facts k1.1… The panel pages go to ctx["_links"] (never shown
    to the model). Safe to call again. Returns {ref: {"cite", "text"}}."""
    links = ctx.setdefault("_links", {})
    known = {}
    for i, f in enumerate([f for f in ctx.get("known_facts") or [] if isinstance(f, dict)], 1):
        ref = f.setdefault("ref", f"m{i}")
        if not f.get("cite"):
            kind = f.get("type")
            about = f.get("about") or ""
            f["cite"] = f.get("source") or FALLBACK_CITE.get(kind, "заметка").format(
                about if kind in ("Компания", "Сотрудник") else quoted(about))
        if f.get("url"):
            links[ref] = f.pop("url")
        known[ref] = {"cite": f["cite"], "text": f"{f.get('about') or ''}: {f.get('text') or ''}"}
    for i, card in enumerate([c for c in ctx.get("about_entities") or [] if isinstance(c, dict)], 1):
        ref = card.setdefault("ref", f"k{i}")
        card.setdefault("cite", f"карточка {quoted(card.get('name') or '')}")
        if card.get("url"):
            links[ref] = card.pop("url")
        fields = [card.get(k) for k in ("name", "unp", "address", "contacts", "description")]
        known[ref] = {"cite": card["cite"],
                      "text": " ".join(str(x) for x in fields + list(card.get("also_called") or []) if x)}
        for j, fact in enumerate([x for x in card.get("facts") or [] if isinstance(x, dict)], 1):
            fact_ref = fact.setdefault("ref", f"{ref}.{j}")
            if not fact.get("cite"):
                fact["cite"] = _entity_cite(fact.pop("source", ""), card)
            if fact.get("url"):
                links[fact_ref] = fact.pop("url")
            known[fact_ref] = {"cite": fact["cite"], "text": f"{card.get('name') or ''}: {fact.get('text') or ''}"}
    return known


def _named(cite: str, text: str) -> bool:
    """Does the answer already name this source in its own words?"""
    low, c = _plain(text), _plain(cite)
    title = re.search(r"«([^»]+)»", c)
    if c.startswith(("совещание", "документ")) and title and title.group(1) in low:
        return True
    if c.startswith("совещание") and re.search(r"совещ|планерк|встреч|протокол", low):
        day = re.search(r"(\d{2})\.(\d{2})\.\d{4}", c)
        if day and (f"{day.group(1)}.{day.group(2)}" in low or f"{int(day.group(1))}.{day.group(2)}" in low):
            return True
    if c == "профиль компании":
        return "профил" in low
    if c.startswith("карточка"):
        return "карточк" in low
    if c.startswith(("заметка", "переписка")):
        return bool(re.search(r"заметк|переписк", low))
    return False


def _numbers(text: str) -> set:
    return {n.strip(".,:/-") for n in _NUM.findall(text or "") if len(re.sub(r"\D", "", n)) >= 2}


def _guess(reply: str, message: str, known: dict) -> list:
    """Without the marks: the record whose numbers or several words the answer repeats (and the question did not
    have). At most two, the best first."""
    asked_nums, asked_terms = _numbers(message), set(terms(message))
    nums = _numbers(reply) - asked_nums
    words = set(terms(reply)) - asked_terms
    scored = []
    for ref, k in known.items():
        hit_nums = nums & _numbers(k["text"])
        hit_words = words & set(terms(k["text"]))
        if hit_nums or len(hit_words) >= 3:
            scored.append((3 * len(hit_nums) + len(hit_words), bool(hit_nums), ref))
    scored.sort(key=lambda x: (-x[0], x[2]))
    if not scored:
        return []
    best = [scored[0][2]]
    if len(scored) > 1 and scored[1][1] and scored[1][0] >= 0.8 * scored[0][0]:
        best.append(scored[1][2])
    return best


def cite(reply: str, ctx: dict, message: str = "", acting: bool = False, panel: bool = False) -> tuple:
    """The answer with its sources (№20). The model marks the facts it used with their refs («… 20 ноября [m2].»);
    the marks become one line «Источник: совещание «Планёрка» от 12.11.2025» with the exact names and days (and the
    panel page for those who use the panel). A source the answer already names is not repeated. When a question was
    answered without marks, the record whose numbers or words the answer repeats is named anyway. Returns
    (reply, [refs used])."""
    reply = reply or ""
    if not reply.strip():
        return reply, []
    known = number_sources(ctx)
    used = []

    def take(found):
        refs = []
        for ref in re.split(r"\s*[,;]\s*", found.group(1)):
            ref = ref.lower().replace("м", "m").replace("к", "k")
            if ref and ref[0].isdigit() and refs:  # «[m1, 3]»: the letter of the one before
                ref = refs[-1][0] + ref
            refs.append(ref)
        hits = [r for r in refs if r in known]
        if not hits and re.search(r"[мк]", found.group(1).lower()):
            return found.group(0)  # «[м2]» unknown: square metres, not a mark
        for r in hits:
            if r not in used:
                used.append(r)
        return ""

    text = _MARK.sub(take, reply)
    text = re.sub(r"[ \t]+([.,;:!?)])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    if not used and not acting and known and _QUESTION.search(message or "") and not _NAMED.search(text) \
            and not _DONT_KNOW.search(text):
        used = _guess(text, message, known)
    if not used or _OWN_LINE.search(text):
        return text, used
    links = ctx.get("_links") or {}
    named, seen = [], set()
    for ref in used:
        source = known[ref]["cite"]
        if not source or source in seen or _named(source, text):
            continue
        seen.add(source)
        url = links.get(ref) if panel and config.PUBLIC_URL else None
        named.append(source + (f" — {config.PUBLIC_URL}{url}" if url else ""))
    if named:
        text += ("\n\nИсточник: " if len(named) == 1 else "\n\nИсточники: ") + "; ".join(named[:MAX_CITES])
    return text, used


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
        from . import entities
        cards = {e.id: e for e in s.query(entities.Entity).filter(entities.Entity.active.is_(True),
                                                                  entities.Entity.merged_into_id.is_(None)).limit(300)}
        meeting_title = meeting.title or ""
        facts = {"meeting": meeting.title, "people": [{"id": e.id, "name": e.name, "position": e.position or None}
                                                       for e in people.values()],
                 "departments": [{"id": d.id, "name": d.name} for d in departments.values()],
                 "entities": [{"id": e.id, "name": e.name, "kind": entities.KINDS.get(e.kind, e.kind)}
                              for e in cards.values()],
                 "already_known": known[:80]}
    system = voice.system_prompt(
        "Выпиши из расшифровки совещания устойчивые факты для памяти ассистента: кто за что отвечает, кто с кем работает, "
        "какие правила и порядок работы в компании и отделах, контрагенты, объекты, предпочтения в общении. Не выписывай "
        "разовые поручения (они уже в задачах), оценки людей, здоровье, личную жизнь и то, что уже есть в already_known. "
        "Каждый факт — одно короткое утверждение в третьем лице с точной цитатой из расшифровки. Факт о клиенте, объекте, "
        'подрядчике или другой организации — subject "entity": entity_id из entities, если она там есть, иначе '
        "entity_name (название, как его называют) и entity_kind (client|object|contractor|org). "
        'JSON: {"facts":[{"subject":"person|department|company|entity","employee_id":ID или null,"department_id":ID или null,'
        '"entity_id":ID или null,"entity_name":"…","entity_kind":"…",'
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
            entity_id = f.get("entity_id") if isinstance(f.get("entity_id"), int) and f.get("entity_id") in cards else None
            if subject == "company" and (entity_id or f.get("entity_name") or f.get("subject") == "entity"):
                if entity_id is None and str(f.get("entity_name") or "").strip() and quote_found(quote, transcript):
                    card = entities.get_or_create(s, str(f["entity_name"]), str(f.get("entity_kind") or "org"), "meeting",
                                                  origin={"type": "meeting", "id": meeting_id, "title": meeting_title,
                                                          "quote": quote[:300]})
                    entity_id = card.id if card is not None else None
                    if card is not None:
                        cards[card.id] = card
                subject = "entity" if entity_id else subject
            reason = acceptable_fact(text) or ("" if quote_found(quote, transcript) else "цитаты нет в расшифровке") or \
                ("уже известно" if any(_similar(text, x) for x in existing) else "")
            if reason:
                log.info("memory fact skipped (%s): %s", reason, text)
                continue
            confidence = f.get("confidence") if f.get("confidence") in CONFIDENCE else "medium"
            s.add(Note(employee_id=employee_id, department_id=department_id, meeting_id=meeting_id,
                       entity_id=entity_id if subject == "entity" else None,
                       kind="company" if subject != "person" else "fact", text=text, quote=quote,
                       confidence=confidence, source="meeting", author="память"))
            if subject == "entity":  # the card shows this meeting even when it names the object another way
                entities.link(s, entity_id, "meeting", meeting_id, quote)
            existing.append(text)
            kept += 1
        s.commit()
    log.info("meeting %s: %s facts added to memory", meeting_id, kept)
    return kept


def learn_and_link(meeting_id: int) -> int:
    """After an approved meeting: facts for the memory, then the meeting and its tasks linked to the cards of the
    clients and objects it speaks about (entities.py)."""
    kept = learn_from_meeting(meeting_id)
    try:
        from . import entities
        entities.from_meeting(meeting_id)
    except Exception:  # noqa: BLE001
        log.exception("meeting %s not linked to the cards", meeting_id)
    return kept


def schedule_learning(meeting_id: int) -> None:
    learn_and_link(meeting_id) if _testing() else POOL.submit(learn_and_link, meeting_id)


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
