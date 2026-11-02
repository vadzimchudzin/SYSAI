"""Who is «Спикер 2»? Naming speakers from what the whole conversation proves, with a confidence.

Evidence is gathered over the whole meeting, each piece with its weight:
  * the speaker introduces themselves («это Игорь», «меня зовут Ольга», «Игорь на связи») — 0.85;
  * somebody calls a person by name («Ольга, что с закупкой?») and this speaker answers right after — 0.6 every time;
  * the model reads the meeting and points at an introduction or an answer the rules missed; its quote must be in the
    transcript and prove it the same way — 0.4;
  * the model sees that the role and the topic fit («говорит о сметах — это зона Анны») — 0.25, never a name alone.
Calling a person is evidence against being them. The support of a candidate is 1 − Π(1 − weight) (halved for every
time the speaker called that name, twice at most); the confidence is the support times one minus the support of the
strongest rival. A voice profile already matched (speaker_id) is a name and is kept.

A name is given only from the threshold up (settings «speaker_name_threshold», 75%), one name per label and one
label per name; the other labels stay «Спикер N», and «Кто это?» offers their 2–3 candidates with the reasons and the
moments to listen to (an unsure name in the protocol is worse than a question).
"""
import collections
import json
import logging
import re

from . import config, llm, settings_store
from .speech_fix import _base, _forms, _same

log = logging.getLogger("sysai.speakers")

ANON = re.compile(r"^\s*(спикер|speaker|говорящий)\s*\d+\s*$", re.IGNORECASE)
_NAME = r"([А-ЯЁ][а-яё]{2,})"
# «Это Вадим», «меня зовут Пётр», «с вами Анна», «Игорь на связи»
_INTRO = re.compile(r"(?:^|[.!?…,]\s*)(?i:это|меня зовут|с вами|на связи)\s+" + _NAME + r"|" + _NAME + r"\s+(?i:на связи)")
# «Анна, что с актом?», «Отлично. Игорь, а что…», «Да, Пётр, слушаю», «Спасибо, Анна.»
_ADDRESS = re.compile(r"(?:^|[.!?…]\s*)(?:(?i:да|ну|так|хорошо|отлично|слушай|скажи|а|и|спасибо|привет|"
                      r"добрый день|доброе утро)[,!]?\s+)?" + _NAME + r"\s*[,!?]")


def anonymous(label: str) -> bool:
    return bool(ANON.match(str(label or "")))


def _people(employees) -> list:
    """[(employee, [names that may be heard])] — first name, nicknames, surname."""
    out = []
    for e in employees:
        names = set()
        parts = (e.name or "").split()
        if parts:
            names.add(parts[0])
            if len(parts) > 1:
                names.add(parts[-1])
        for alias in (getattr(e, "aliases", "") or "").split(","):
            alias = alias.strip()
            if alias and " " not in alias:
                names.add(alias)
        out.append((e, sorted(n for n in names if len(n) >= 3)))
    return out


def _who(word: str, people) -> list:
    """Employees a heard word (in any case form) may refer to."""
    found = []
    for e, names in people:
        for n in names:
            base = _base(n)
            if any(_same(stem + ending, n) or (ending and _same(stem, base)) or (not ending and _same(stem, base))
                   for stem, ending in _forms(word)):
                found.append(e)
                break
    return found


WEIGHTS = {"intro": 0.85, "answer": 0.6, "model": 0.4, "role": 0.25}
CALLED = 0.5             # support kept after the speaker called that name (per call, twice at most)
DEFAULT_THRESHOLD = 0.75
CANDIDATES = 3
MIN_CANDIDATE = 0.1      # weaker candidates are not offered
QUOTE_CHARS = 140
MODEL_LINES = 320        # replicas the model reads: every one with a name, an introduction or near them


def threshold_of(settings=None) -> float:
    """The confidence a name needs (0.5–0.95)."""
    value = (settings or {}).get("speaker_name_threshold") if settings else settings_store.get("speaker_name_threshold")
    try:
        value = float(value if value is not None else DEFAULT_THRESHOLD)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    if value != value:
        return DEFAULT_THRESHOLD
    value = value / 100 if value > 1 else value  # a percentage
    return min(0.95, max(0.5, value))


def _time(segment, key="start") -> float:
    try:
        value = float(segment.get(key))
    except (TypeError, ValueError):
        return 0.0
    return value if value == value and value >= 0 else 0.0


def _quote(text: str, at: int = 0) -> str:
    """Up to QUOTE_CHARS of a replica around character `at`."""
    text = " ".join(str(text or "").split())
    if len(text) <= QUOTE_CHARS:
        return text
    begin = max(0, min(at - 40, len(text) - QUOTE_CHARS))
    piece = text[begin:begin + QUOTE_CHARS].strip()
    return ("…" if begin else "") + piece + ("…" if begin + QUOTE_CHARS < len(text) else "")


def _item(kind: str, segments: list, said: int, heard: int, at: int = 0, quote: str = "", why: str = "") -> dict:
    """One piece of evidence: `said` is the replica with the name, `heard` the replica of the voice to listen to."""
    voice = segments[heard]
    return {"kind": kind, "weight": WEIGHTS[kind], "said": said, "seq": heard,
            "at": round(_time(segments[said]), 2), "start": round(_time(voice), 2),
            "end": round(max(_time(voice, "end"), _time(voice)), 2),
            "quote": quote or _quote(segments[said].get("text"), at), "why": why[:160]}


def _evidence(segments: list, employees) -> tuple:
    """({(label, employee id): [evidence]}, Counter((label, employee id) -> times the label called that name))."""
    people = _people(employees)
    found = collections.defaultdict(list)
    called = collections.Counter()
    for i, seg in enumerate(segments):
        label, text = seg.get("speaker"), str(seg.get("text") or "")
        own = anonymous(label)  # a label named by a voice profile still tells us whom it called
        introduced = set()
        for match in _INTRO.finditer(text):
            word = match.group(1) or match.group(2)
            hits = _who(word, people)
            if len(hits) == 1:
                if own:
                    found[(label, hits[0].id)].append(_item("intro", segments, i, i, match.start()))
                introduced.add(hits[0].id)
        for call in _ADDRESS.finditer(text):
            hits = _who(call.group(1), people)
            if len(hits) == 1 and hits[0].id not in introduced:
                # the caller is not the person called; the next different speaker most likely is
                if own:
                    called[(label, hits[0].id)] += 1
                for j in range(i + 1, min(i + 3, len(segments))):
                    nxt = segments[j]
                    if nxt.get("speaker") != label:
                        if anonymous(nxt.get("speaker")):
                            found[(nxt["speaker"], hits[0].id)].append(_item("answer", segments, i, j, call.start(1)))
                        break
    return found, called


def _model_lines(segments: list, employees) -> list:
    """The replicas the model reads: all of a short meeting; of a long one every replica with a name of a participant
    or an introduction, with the one before and two after (the whole meeting, not just its start)."""
    if len(segments) <= MODEL_LINES:
        return list(range(len(segments)))
    people = _people(employees)
    keep = set()
    for i, seg in enumerate(segments):
        text = str(seg.get("text") or "")
        words = re.findall(r"[А-ЯЁ][а-яё]{2,}", text)
        if _INTRO.search(text) or any(_who(word, people) for word in words):
            keep.update(range(max(0, i - 1), min(len(segments), i + 3)))
    return sorted(keep)[:MODEL_LINES]


def _model_proposals(segments: list, employees) -> list:
    if not config.llm_enabled() and not config.TESTING:
        return []
    from . import voice
    lines = [f"[{i}] {segments[i].get('speaker')}: {str(segments[i].get('text') or '')[:300]}"
             for i in _model_lines(segments, employees)]
    try:
        raw = llm.chat(voice.model(), [
            {"role": "system", "content": "Определи, кто из сотрудников стоит за метками «Спикер N», по всей встрече. "
                                          "Имя (kind \"name\") — только по тому, что сказано: человек представился или "
                                          "ответил, когда к нему обратились по имени; дай дословную цитату с именем. "
                                          "Если имени нет, но роль и тема явно указывают на сотрудника (должность, "
                                          "зона ответственности), дай kind \"role\", цитату этой метки и коротко почему. "
                                          "Не угадывай. "
                                          'JSON: {"speakers":[{"label":"Спикер 1","employee_id":ID,"kind":"name","quote":"...","why":"..."}]}'},
            {"role": "user", "content": "Сотрудники: " + json.dumps([{"id": e.id, "name": e.name,
                                                                      "aliases": getattr(e, "aliases", "") or "",
                                                                      "position": getattr(e, "position", "") or ""}
                                                                     for e in employees], ensure_ascii=False) +
                                        "\n<data>" + "\n".join(lines) + "</data>"}],
            json_mode=True, temperature=0.0, max_tokens=1200, timeout=90)
        return (llm.parse_json(raw) or {}).get("speakers") or []
    except Exception as exc:  # noqa: BLE001  naming is a bonus
        log.info("speaker naming by the model unavailable: %s", exc)
        return []


def _norm(text) -> str:
    return " ".join(re.findall(r"[а-яёa-z0-9]+", str(text or "").lower().replace("ё", "е")))


def _proof(label: str, employee, quote: str, segments: list, names: list) -> tuple | None:
    """(replica with the name, replica of the voice) when a model's quote proves who a label is the way the rules do:
    the label introduces itself with that name, or another speaker calls the name and this label answers right after.
    Calling somebody is never proof of being them."""
    quote = quote.strip().strip("«»\"'")
    if len(quote) < 4:
        return None
    q = _norm(quote)
    for i, seg in enumerate(segments):
        if q not in _norm(seg.get("text")):
            continue
        text = str(seg.get("text") or "")
        if seg.get("speaker") == label:
            intro = any(employee in _who(m.group(1) or m.group(2), [(employee, names)]) for m in _INTRO.finditer(text))
            return (i, i) if intro else None
        called = any(employee in _who(m.group(1), [(employee, names)]) for m in _ADDRESS.finditer(text))
        j = next((k for k in range(i + 1, min(i + 3, len(segments))) if segments[k].get("speaker") != seg.get("speaker")), None)
        return (i, j) if called and j is not None and segments[j].get("speaker") == label else None
    return None


def _proves(label: str, employee, quote: str, segments: list, names: list) -> bool:
    return _proof(label, employee, quote, segments, names) is not None


def _role(label: str, quote: str, segments: list) -> int | None:
    """The replica of this label the model quoted for a role hint."""
    q = _norm(quote)
    if len(q) < 4:
        return None
    return next((i for i, seg in enumerate(segments) if seg.get("speaker") == label and q in _norm(seg.get("text"))), None)


def _support(items: list, calls: int) -> float:
    rest = 1.0
    for item in items:
        rest *= 1.0 - item["weight"]
    return (1.0 - rest) * CALLED ** min(2, calls)


REASONS = {"intro": "представился", "answer": "назвали по имени, ответил этот голос",
           "model": "по разговору", "role": "роль и тема"}


def reason_text(item: dict) -> str:
    if item.get("kind") == "role":
        return f"{REASONS['role']}: {item.get('why') or item.get('quote')}"
    return f"{REASONS.get(item.get('kind'), 'по разговору')}: «{item.get('quote')}»"


def analyse(segments: list, employees, use_model: bool = True, threshold: float | None = None) -> dict:
    """Who every «Спикер N» is: {"mapping": {label: name}, "named": {label: candidate},
    "ask": {label: [candidates]}}; a candidate is {"employee_id", "name", "confidence", "reasons": [evidence]}."""
    empty = {"mapping": {}, "named": {}, "ask": {}}
    if settings_store.get("speaker_naming") is False or not segments or not employees:
        return empty
    labels = [label for label in dict.fromkeys(s.get("speaker") for s in segments) if anonymous(label)]
    if not labels:
        return empty
    threshold = threshold_of() if threshold is None else threshold
    by_id = {e.id: e for e in employees}
    taken = {s.get("speaker") for s in segments if not anonymous(s.get("speaker"))}  # voice profiles already named
    taken_ids = {e.id for e in employees if e.name in taken}
    found, called = _evidence(segments, employees)
    if use_model:
        names = dict((e.id, n) for e, n in _people(employees))
        for p in _model_proposals(segments, employees):
            if not isinstance(p, dict) or p.get("label") not in labels or p.get("employee_id") not in by_id:
                continue
            key, quote = (p["label"], p["employee_id"]), str(p.get("quote") or "")
            if p.get("kind") == "role":
                seq = _role(p["label"], quote, segments)
                if seq is not None and not any(item["kind"] == "role" for item in found[key]):
                    found[key].append(_item("role", segments, seq, seq, quote=_quote(quote),
                                            why=str(p.get("why") or "")))
                continue
            proof = _proof(p["label"], by_id[p["employee_id"]], quote, segments, names.get(p["employee_id"], []))
            if proof and not any(item["said"] == proof[0] for item in found[key]):  # the rules already counted it
                found[key].append(_item("model", segments, proof[0], proof[1], quote=_quote(quote)))
    support = {key: _support(items, called[key]) for key, items in found.items()
               if key[0] in labels and key[1] in by_id and key[1] not in taken_ids}

    def confidence(label, eid):
        rival = max((value for (other_label, other), value in support.items()
                     if other_label == label and other != eid), default=0.0)
        return support[(label, eid)] * (1.0 - rival)

    def candidate(label, eid):
        items = sorted(found[(label, eid)], key=lambda item: (-item["weight"], item["at"]))
        reasons = [dict({k: v for k, v in item.items() if k not in ("weight", "said")}, text=reason_text(item))
                   for item in items[:3]]
        return {"employee_id": eid, "name": by_id[eid].name, "confidence": round(confidence(label, eid), 3),
                "reasons": reasons}

    ranked = sorted(((confidence(label, eid), label, eid) for label, eid in support), key=lambda x: (-x[0], x[1], x[2]))
    mapping, named, used = {}, {}, set(taken_ids)
    for score, label, eid in ranked:
        if score < threshold:
            break
        if label in mapping or eid in used:
            continue
        mapping[label] = by_id[eid].name
        named[label] = candidate(label, eid)
        used.add(eid)
    ask = {}
    for label in labels:
        if label in mapping:
            continue
        offers = [candidate(label, eid) for _, other, eid in ranked if other == label and support[(label, eid)] >= MIN_CANDIDATE]
        if offers:
            ask[label] = offers[:CANDIDATES]
    return {"mapping": mapping, "named": named, "ask": ask}


def name_speakers(segments: list, employees, use_model: bool = True, threshold: float | None = None,
                  found: dict | None = None) -> tuple:
    """(segments with names where proven, {label: employee name}); `found` receives the whole analysis."""
    result = analyse(segments, employees, use_model, threshold)
    if found is not None:
        found.update(result)
    mapping = result["mapping"]
    if not mapping:
        return segments, {}
    log.info("speakers named: %s", mapping)
    confidence = {label: item["confidence"] for label, item in result["named"].items()}
    return [dict(s, speaker=mapping[s.get("speaker")], speaker_confidence=confidence[s.get("speaker")])
            if s.get("speaker") in mapping else s for s in segments], mapping
