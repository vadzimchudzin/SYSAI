"""Who is «Спикер 2»? Naming speakers from what the conversation itself proves.

Evidence, from strongest to weakest:
  * a voice profile already matched the voice (speaker_id) — those labels are names and are kept;
  * the speaker introduces themselves («это Игорь», «меня зовут Ольга», «Игорь на связи»);
  * somebody addresses a person by name («Ольга, что с закупкой?») and a different speaker answers right after;
  * the model reads the transcript and proposes names, each with a quote; code keeps a proposal only when the quote
    is in the transcript and contains the name, and when it does not contradict the evidence above.

One name per label and one label per name; a label without enough evidence stays «Спикер N» (the report then asks the
reviewer instead of guessing).
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


def evidence(segments: list, employees) -> dict:
    """label -> Counter(employee_id -> points) from introductions and answers to a call by name."""
    people = _people(employees)
    votes = collections.defaultdict(collections.Counter)
    for i, seg in enumerate(segments):
        label, text = seg.get("speaker"), str(seg.get("text") or "")
        own = anonymous(label)  # a label named by a voice profile still tells us whom it called
        introduced = set()
        for match in _INTRO.finditer(text):
            word = match.group(1) or match.group(2)
            hits = _who(word, people)
            if len(hits) == 1:
                if own:
                    votes[label][hits[0].id] += 3
                introduced.add(hits[0].id)
        for call in _ADDRESS.finditer(text):
            hits = _who(call.group(1), people)
            if len(hits) == 1 and hits[0].id not in introduced:
                # the caller is not the person called; the next different speaker most likely is
                if own:
                    votes[label][hits[0].id] -= 2
                for nxt in segments[i + 1:i + 3]:
                    if nxt.get("speaker") != label:
                        if anonymous(nxt.get("speaker")):
                            votes[nxt["speaker"]][hits[0].id] += 2
                        break
    return votes


def _model_proposals(segments: list, employees) -> list:
    if not config.llm_enabled() and not config.TESTING:
        return []
    from . import voice
    lines = [f"{s.get('speaker')}: {str(s.get('text') or '')[:300]}" for s in segments[:200]]
    try:
        raw = llm.chat(voice.model(), [
            {"role": "system", "content": "Определи, кто из сотрудников стоит за метками «Спикер N», только по тому, что "
                                          "сказано: представления, обращения по имени и ответы на них. Не угадывай по "
                                          "теме разговора. Для каждой уверенной пары дай дословную цитату с именем. "
                                          'JSON: {"speakers":[{"label":"Спикер 1","employee_id":ID,"quote":"..."}]}'},
            {"role": "user", "content": "Сотрудники: " + json.dumps([{"id": e.id, "name": e.name,
                                                                      "aliases": getattr(e, "aliases", "") or ""}
                                                                     for e in employees], ensure_ascii=False) +
                                        "\n<data>" + "\n".join(lines) + "</data>"}],
            json_mode=True, temperature=0.0, max_tokens=800, timeout=90)
        return (llm.parse_json(raw) or {}).get("speakers") or []
    except Exception as exc:  # noqa: BLE001  naming is a bonus
        log.info("speaker naming by the model unavailable: %s", exc)
        return []


def _proves(label: str, employee, quote: str, segments: list, names: list) -> bool:
    """A model's quote proves who a label is only the way the rules do: the label introduces itself with that name, or
    another speaker calls the name and this label answers right after. Calling somebody is never proof of being them."""
    quote = quote.strip().strip("«»\"'")
    if len(quote) < 4:
        return False
    norm = lambda t: " ".join(re.findall(r"[а-яё0-9]+", t.lower().replace("ё", "е")))
    q = norm(quote)
    for i, seg in enumerate(segments):
        if q not in norm(str(seg.get("text") or "")):
            continue
        text = str(seg.get("text") or "")
        if seg.get("speaker") == label:
            return any(employee in _who(m.group(1) or m.group(2), [(employee, names)]) for m in _INTRO.finditer(text))
        called = any(employee in _who(m.group(1), [(employee, names)]) for m in _ADDRESS.finditer(text))
        nxt = next((x for x in segments[i + 1:i + 3] if x.get("speaker") != seg.get("speaker")), None)
        return called and nxt is not None and nxt.get("speaker") == label
    return False


def name_speakers(segments: list, employees, use_model: bool = True) -> tuple:
    """(segments with names where proven, {label: employee name})."""
    if settings_store.get("speaker_naming") is False or not segments or not employees:
        return segments, {}
    labels = {s.get("speaker") for s in segments if anonymous(s.get("speaker"))}
    if not labels:
        return segments, {}
    by_id = {e.id: e for e in employees}
    taken = {s.get("speaker") for s in segments if not anonymous(s.get("speaker"))}  # voice profiles already named
    taken_ids = {e.id for e in employees if e.name in taken}
    votes = evidence(segments, employees)
    if use_model:
        people = dict((e.id, names) for e, names in _people(employees))
        for p in _model_proposals(segments, employees):
            if not isinstance(p, dict) or p.get("label") not in labels or p.get("employee_id") not in by_id:
                continue
            if _proves(p["label"], by_id[p["employee_id"]], str(p.get("quote") or ""), segments,
                       people.get(p["employee_id"], [])) and votes[p["label"]][p["employee_id"]] >= 0:
                votes[p["label"]][p["employee_id"]] += 2
    mapping, used = {}, set(taken_ids)
    ranked = sorted(((points, label, eid) for label, c in votes.items() for eid, points in c.items() if points >= 2),
                    key=lambda x: -x[0])
    for points, label, eid in ranked:
        if label in mapping or eid in used:
            continue
        rival = max((p for e, p in votes[label].items() if e != eid), default=0)
        if rival >= points:
            continue  # two people fit equally: keep «Спикер N»
        mapping[label] = by_id[eid].name
        used.add(eid)
    if not mapping:
        return segments, {}
    log.info("speakers named: %s", mapping)
    return [dict(s, speaker=mapping.get(s.get("speaker"), s.get("speaker"))) for s in segments], mapping
