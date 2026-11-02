"""Corrections of single replicas — another speaker, a split in two, a merge with a neighbour — and their undo.

Before the first correction the automatic transcript is kept as it was (Meeting.transcript_auto): «Вернуть
автоматическую разметку» brings it back, and the automatic speaker labelling is measured against the corrected copy
(diarization_eval.py). Every correction is logged with the replicas it replaced, so the latest ones are undone in order.
A log that no longer matches the transcript (it was replaced by a new transcription) is never applied.
"""
import copy
import datetime as dt
import hashlib
import json
import math
import re

from . import voice_diagram

HISTORY_LIMIT = 50          # corrections kept for undo
HISTORY_BYTES = 1_500_000   # and their size: a correction of every replica of a long meeting is large
SHOWN = 10                  # latest corrections listed on the meeting page
NEW = "new"                 # a voice that is not in the transcript yet («Спикер N»)
SAME = "same"               # the second part of a split keeps the speaker
OPS = {"speaker": "говорящий", "split": "разделение", "merge": "объединение", "assign": "все реплики говорящего"}


class EditError(ValueError):
    """A correction that cannot be made; the message is shown to the person."""


class Stale(EditError):
    """The page was made for an older transcript."""


def _seconds(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def mmss(value) -> str:
    sec = int(_seconds(value) or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def signature(segment) -> str:
    """Short fingerprint of one replica: a form made for an older transcript must not change another replica."""
    segment = segment if isinstance(segment, dict) else {}
    raw = json.dumps([segment.get("start"), segment.get("end"), segment.get("speaker"), segment.get("text")],
                     ensure_ascii=False, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def fingerprint(transcript) -> str:
    raw = json.dumps(transcript or [], ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def automatic(transcript) -> list:
    """The transcript with every manual speaker correction taken back (each replica gets its first labelling)."""
    restored, _ = voice_diagram.reset(copy.deepcopy(list(transcript or [])))
    return restored


def labels(transcript) -> list:
    """Distinct speakers in the order they first speak («Без метки» is not a speaker to choose)."""
    seen = []
    for segment in transcript or []:
        if isinstance(segment, dict):
            label = voice_diagram.label_of(segment)
            if label != voice_diagram.UNLABELLED and label not in seen:
                seen.append(label)
    return seen


def new_label(transcript) -> str:
    """«Спикер N» after every number in use, the automatic labels of renamed replicas included."""
    numbers = [0]
    for segment in transcript or []:
        if not isinstance(segment, dict):
            continue
        auto = segment.get("speaker_auto") if isinstance(segment.get("speaker_auto"), dict) else {}
        for label in (segment.get("speaker"), auto.get("speaker")):
            match = re.fullmatch(r"Спикер (\d{1,4})", str(label or "").strip())
            if match:
                numbers.append(int(match.group(1)))
    return f"Спикер {max(numbers) + 1}"


def words(text) -> list:
    """[(char offset, word)] of the replica: a second part may begin at any word but the first."""
    return [(match.start(), match.group(0)) for match in re.finditer(r"\S+", str(text or ""))]


def segment_at(transcript, seq, sig) -> dict:
    transcript = transcript or []
    if not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq < len(transcript) \
            or not isinstance(transcript[seq], dict):
        raise EditError("Такой реплики нет в расшифровке.")
    if str(sig or "") != signature(transcript[seq]):
        raise Stale("Расшифровка уже изменилась. Обновите страницу и повторите правку.")
    return transcript[seq]


def resolve(transcript, choice, guest_name="", employee=None):
    """(name, employee id or None, guest?) for the choice in the form, or None for «как было» (SAME)."""
    choice = str(choice or "").strip()
    if choice == SAME:
        return None
    if choice.startswith("label:"):
        label = choice[len("label:"):]
        if label not in labels(transcript):
            raise EditError("Такого говорящего нет в расшифровке.")
        sample = next(s for s in transcript if isinstance(s, dict) and voice_diagram.label_of(s) == label)
        employee_id = sample.get("speaker_employee_id")
        valid = isinstance(employee_id, int) and not isinstance(employee_id, bool)
        return label, employee_id if valid else None, bool(sample.get("speaker_guest"))
    if choice == "guest":
        return voice_diagram.guest_name(transcript, guest_name, ""), None, True
    if choice == NEW:
        return new_label(transcript), None, False
    if choice.startswith("employee:") and employee is not None:
        return employee.name, employee.id, False
    raise EditError("Выберите, кто говорит: говорящего этого совещания, сотрудника, гостя или нового говорящего.")


def _entry(op: str, summary: str, seq: int, patches: list, by: str) -> dict:
    return {"op": op, "summary": summary[:200], "seq": seq, "by": str(by or "")[:120],
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "patches": patches}


def set_speaker(transcript, seq: int, sig: str, who, by: str = "") -> tuple[list, dict]:
    """One replica gets another speaker; every other replica stays as it is."""
    segment = segment_at(transcript, seq, sig)
    if who is None:
        raise EditError("Выберите, кто говорит.")
    name, employee_id, guest = who
    if voice_diagram.label_of(segment) == name and segment.get("speaker_source") == "manual":
        raise EditError("У этой реплики уже этот говорящий.")
    result = list(transcript)
    result[seq] = voice_diagram.relabel(segment, name, employee_id, guest, by)
    summary = f"{mmss(segment.get('start'))}: {voice_diagram.label_of(segment)} → {name}"
    return result, _entry("speaker", summary, seq, [[seq, 1, [copy.deepcopy(segment)]]], by)


def split(transcript, seq: int, sig: str, at, who=None, by: str = "") -> tuple[list, dict]:
    """Two replicas instead of one, cut before the chosen word; the time of the cut is estimated by the text length."""
    segment = segment_at(transcript, seq, sig)
    text = str(segment.get("text") or "")
    try:
        at = int(at)
    except (TypeError, ValueError):
        at = -1
    if at not in [offset for offset, _ in words(text)][1:]:
        raise EditError("Выберите слово, с которого начинается вторая часть.")
    first, second = text[:at].rstrip(), text[at:].strip()
    start = _seconds(segment.get("start")) or 0.0
    end = _seconds(segment.get("end"))
    end = start if end is None or end < start else end
    cut = round(start + (end - start) * len(first) / max(1, len(first) + len(second)), 2)
    cut = min(max(cut, start), end)
    head, tail = copy.deepcopy(segment), copy.deepcopy(segment)
    head.update(text=first, end=cut)
    tail.update(text=second, start=cut)
    if who is not None:
        tail = voice_diagram.relabel(tail, *who, by=by)
    result = list(transcript[:seq]) + [head, tail] + list(transcript[seq + 1:])
    summary = f"{mmss(start)}: реплика разделена перед «{second.split()[0][:40]}»" + (f", вторая часть — {who[0]}" if who else "")
    return result, _entry("split", summary, seq, [[seq, 2, [copy.deepcopy(segment)]]], by)


def merge(transcript, seq: int, sig: str, direction: str, neighbour_sig: str, by: str = "") -> tuple[list, dict]:
    """The replica joins the previous one («prev», the previous speaker stays) or takes in the next one («next»)."""
    segment_at(transcript, seq, sig)
    if direction not in ("prev", "next"):
        raise EditError("Не указано, с какой репликой объединить.")
    other = seq - 1 if direction == "prev" else seq + 1
    if not 0 <= other < len(transcript) or not isinstance(transcript[other], dict):
        raise EditError("Соседней реплики нет.")
    if str(neighbour_sig or "") != signature(transcript[other]):
        raise Stale("Расшифровка уже изменилась. Обновите страницу и повторите правку.")
    first, second = sorted((seq, other))
    a, b = transcript[first], transcript[second]
    merged = copy.deepcopy(a)
    merged["text"] = " ".join(part for part in (str(a.get("text") or "").strip(), str(b.get("text") or "").strip()) if part)
    if _seconds(b.get("end")) is not None:
        merged["end"] = b["end"]
    result = list(transcript[:first]) + [merged] + list(transcript[second + 1:])
    summary = f"{mmss(a.get('start'))}: реплики {voice_diagram.label_of(a)} и {voice_diagram.label_of(b)} объединены"
    return result, _entry("merge", summary, first, [[first, 1, [copy.deepcopy(a), copy.deepcopy(b)]]], by)


def assigned(before, after, label: str, name: str, by: str = "") -> dict:
    """The log entry of «Кто это?»: every replica of one speaker renamed at once."""
    patches = [[index, 1, [copy.deepcopy(old)]] for index, (old, new) in enumerate(zip(before or [], after or []))
               if old != new]
    first = patches[0][0] if patches else 0
    return _entry("assign", f"все реплики «{label}» → {name} ({len(patches)})", first, patches, by)


def _entries(history) -> list:
    entries = history.get("entries") if isinstance(history, dict) else None
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _bounded(entries: list) -> list:
    entries = entries[-HISTORY_LIMIT:]
    while len(entries) > 1 and len(json.dumps(entries, ensure_ascii=False, default=str)) > HISTORY_BYTES:
        entries = entries[1:]
    return entries


def record(meeting, new_transcript: list, entry: dict) -> None:
    """Save a correction: the automatic transcript is kept before the first one, the log grows (bounded)."""
    if meeting.transcript_auto is None:
        meeting.transcript_auto = automatic(meeting.transcript)
    entry = dict(entry, after=fingerprint(new_transcript))
    meeting.transcript_edits = {"entries": _bounded(_entries(meeting.transcript_edits) + [entry])}
    meeting.transcript = new_transcript


def undo(meeting) -> dict:
    """Take back the latest correction; returns its log entry."""
    entries = _entries(meeting.transcript_edits)
    if not entries:
        raise EditError("Отменять нечего: правок нет.")
    last = entries[-1]
    if last.get("after") != fingerprint(meeting.transcript):
        raise Stale("Расшифровка изменилась после этой правки, отменить её уже нельзя.")
    result = list(meeting.transcript or [])
    for index, count, before in reversed(last.get("patches") or []):
        if not 0 <= index <= len(result) or index + count > len(result):
            raise Stale("Журнал правок не совпадает с расшифровкой, отменить правку нельзя.")
        result[index:index + count] = copy.deepcopy(before)
    previous = entries[:-1]
    meeting.transcript = result
    meeting.transcript_edits = {"entries": previous} if previous else None
    return last


def reset(meeting) -> int:
    """«Вернуть автоматическую разметку»: the transcript as it came from processing; the log is cleared.
    Returns the number of replicas that changed (splits and merges count as their replicas)."""
    current = list(meeting.transcript or [])
    if meeting.transcript_auto is not None:
        restored = copy.deepcopy(meeting.transcript_auto)
    else:
        restored = automatic(current)
    changed = sum(1 for old, new in zip(current, restored) if old != new) + abs(len(current) - len(restored))
    meeting.transcript = restored
    meeting.transcript_auto = None
    meeting.transcript_edits = None
    return changed


def forget(meeting) -> None:
    """A new transcription replaces the old one: its corrections and their log do not apply to it."""
    meeting.transcript_auto = None
    meeting.transcript_edits = None


def history(meeting) -> dict:
    """What the meeting page shows: how many corrections, the latest ones and whether the last one can be undone."""
    entries = _entries(meeting.transcript_edits)
    undoable = bool(entries) and entries[-1].get("after") == fingerprint(meeting.transcript)
    shown = []
    for entry in reversed(entries[-SHOWN:]):
        try:
            moment = dt.datetime.fromisoformat(str(entry.get("at")))
        except ValueError:
            moment = None
        shown.append({"summary": entry.get("summary", ""), "by": entry.get("by", ""), "op": OPS.get(entry.get("op"), ""),
                      "at": moment, "seq": entry.get("seq")})
    return {"count": len(entries), "undoable": undoable, "last": entries[-1].get("summary", "") if entries else "",
            "entries": shown, "edited": meeting.transcript_auto is not None}
