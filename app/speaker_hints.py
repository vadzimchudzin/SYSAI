"""«Кто это?» with candidates, and how sure a name from the conversation is — the meeting page side of speakers.py.

The processing keeps what it found (options["speaker_hints"]: the names it gave and the candidates of the voices it
left unnamed, the model's evidence included). The page adds what the current transcript proves by the rules, so the
candidates follow the corrections: a person already given to another voice is not offered again.
"""
import datetime as dt

from . import participants, speakers

LIMIT = 3


def save(meeting, analysis: dict) -> None:
    """Keep the names given (by the name: that is the label the transcript has now) and the candidates of the voices
    left as «Спикер N»."""
    named = {item["name"]: dict(item, label=label) for label, item in (analysis.get("named") or {}).items()
             if isinstance(item, dict) and item.get("name")}
    meeting.options = dict(meeting.options or {}, speaker_hints={
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "threshold": speakers.threshold_of(), "named": named, "ask": analysis.get("ask") or {}})


def _seq(transcript: list, label: str, start) -> int | None:
    """The replica a reason points to, found again by its label and time (edits move the numbers)."""
    try:
        start = float(start)
    except (TypeError, ValueError):
        return None
    near = [i for i, seg in enumerate(transcript) if isinstance(seg, dict) and abs(float(seg.get("start") or 0) - start) < 0.06]
    return next((i for i in near if transcript[i].get("speaker") == label), near[0] if near else None)


def _reasons(transcript: list, label: str, reasons) -> list:
    out = []
    for item in reasons or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        seq = _seq(transcript, label, item.get("start"))
        out.append({"text": item["text"], "at": item.get("at") or 0, "seq": seq,
                    "start": transcript[seq].get("start") if seq is not None else item.get("start"),
                    "end": transcript[seq].get("end") if seq is not None else item.get("end")})
    return out


def attach(diagram: dict | None, meeting, employees) -> None:
    """Candidates for every unknown voice and the confidence of every name from the conversation, into the diagram."""
    if not diagram:
        return
    transcript = [seg for seg in (meeting.transcript or []) if isinstance(seg, dict)]
    stored = (meeting.options or {}).get("speaker_hints") if isinstance(meeting.options, dict) else None
    stored = stored if isinstance(stored, dict) else {}
    by_id = {e.id: e for e in employees}
    allowed = set(participants.employee_ids(meeting)) if participants.is_set(meeting) else None
    people = [e for e in employees if allowed is None or e.id in allowed]
    unknown = [sp for sp in diagram.get("speakers") or [] if sp.get("status") == "unknown"]
    live = speakers.analyse(transcript, people, use_model=False)["ask"] if unknown and people else {}
    taken = {seg.get("speaker") for seg in transcript if not speakers.anonymous(seg.get("speaker"))}
    for sp in diagram.get("speakers") or []:
        label = sp.get("name")
        if sp.get("status") == "context":
            named = (stored.get("named") or {}).get(label) or {}
            confidence = named.get("confidence")
            if confidence is None:
                confidence = next((seg.get("speaker_confidence") for seg in transcript if seg.get("speaker") == label
                                   and isinstance(seg.get("speaker_confidence"), (int, float))), None)
            if isinstance(confidence, (int, float)):
                sp["confidence"] = round(100 * confidence)
                reasons = _reasons(transcript, label, named.get("reasons"))
                sp["reasons"] = reasons
                if reasons:
                    sp["status_hint"] = f"{sp['status_hint']} Уверенность {sp['confidence']}%: " + "; ".join(
                        r["text"] for r in reasons) + "."
        elif sp.get("status") == "unknown":
            offers = {}
            # what the processing found (the model's evidence included) first; the rules add what the edits revealed
            for item in list((stored.get("ask") or {}).get(label) or []) + list(live.get(label) or []):
                eid = item.get("employee_id") if isinstance(item, dict) else None
                employee = by_id.get(eid)
                if employee is None or eid in offers or employee.name in taken or (allowed is not None and eid not in allowed):
                    continue
                offers[eid] = {"employee_id": eid, "name": employee.name, "position": employee.position or "",
                               "percent": round(100 * float(item.get("confidence") or 0)),
                               "reasons": _reasons(transcript, label, item.get("reasons"))}
            sp["candidates"] = sorted(offers.values(), key=lambda c: -c["percent"])[:LIMIT]
            if sp["candidates"]:
                sp["status_hint"] = ("Голос не совпал ни с одним образцом; разговор подсказывает, кто это, но уверенности "
                                     "не хватило — см. «Кто это?».")
