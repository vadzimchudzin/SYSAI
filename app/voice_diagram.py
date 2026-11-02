"""Who spoke when: the data behind the voice diagram on the meeting page, and manual speaker corrections.

Everything is derived from the saved transcript ({speaker, start, end, text, speaker_source?}) and, for meetings
processed after the waveform was introduced, from the stored loudness outline (see waveform.py). The audio itself is
gone after processing, so corrections here are made by reading the text, not by listening.
"""
import math

from . import speakers as speaker_names
from . import waveform

# Muted pastels on the dark theme: clay, blue, green and amber first (the application's own accents).
PALETTE = ("#d98e8a", "#8fa8d8", "#7fb77e", "#e0a85a", "#b79bd9", "#6fbfb4", "#d98fb8", "#c2b56b", "#9fb2c4", "#e39274")
MERGE_GAP = 1.5          # seconds of silence inside one speaker's turn
MIN_OVERLAP = 0.3        # shorter cross-talk is usually a timestamp rounding, not two voices
MAX_OVERLAPS = 2000
SAMPLE_QUOTES = 3
QUOTE_CHARS = 180
GUEST = "Гость"
UNLABELLED = "Без метки"

STATUS = {
    "voice": ("голос совпал", "green", "Сопоставлено с подтверждённым образцом голоса. Оценка сходства не является вероятностью."),
    "manual": ("указан вручную", "blue", "Говорящего указал человек по тексту реплик."),
    "guest": ("гость", "gray", "Отмечен как гость: не сотрудник, голос не сохраняется."),
    "context": ("имя из разговора", "clay", "Имя определено по представлению или обращению в разговоре, без сверки голоса."),
    "unknown": ("не распознан", "amber", "Голос не совпал ни с одним образцом, и в разговоре нет доказательства имени."),
}


def _seconds(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def label_of(segment) -> str:
    return str(segment.get("speaker") or "").strip() or UNLABELLED


def _union(intervals) -> float:
    total, current_start, current_end = 0.0, None, None
    for start, end in sorted(intervals):
        if current_end is None or start > current_end:
            if current_end is not None:
                total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    if current_end is not None:
        total += current_end - current_start
    return total


def _shares(seconds: list) -> list:
    """Whole percentages that add up to exactly 100 (largest remainder)."""
    whole = sum(seconds)
    if whole <= 0:
        return [0 for _ in seconds]
    raw = [100 * value / whole for value in seconds]
    shares = [int(math.floor(value)) for value in raw]
    order = sorted(range(len(raw)), key=lambda i: raw[i] - shares[i], reverse=True)
    for index in order[:100 - sum(shares)]:
        shares[index] += 1
    return shares


def _status(label: str, rows: list) -> str:
    sources = {row.get("speaker_source") for row in rows}
    if "voiceprint" in sources:
        return "voice"
    if "manual" in sources:
        return "guest" if any(row.get("speaker_guest") for row in rows) else "manual"
    if speaker_names.anonymous(label) or label == UNLABELLED:
        return "unknown"
    return "context"


def _overlaps(segments: list) -> list:
    """Intervals where two or more different speakers talk at the same time, by transcript timestamps."""
    events = []
    for _, speaker, start, end in segments:
        if end > start:
            events.append((start, 1, speaker))
            events.append((end, -1, speaker))
    events.sort(key=lambda event: (event[0], event[1]))
    active: dict = {}
    found, opened = [], None
    for moment, delta, speaker in events:
        before = sum(1 for count in active.values() if count > 0)
        active[speaker] = active.get(speaker, 0) + delta
        after = sum(1 for count in active.values() if count > 0)
        if before < 2 <= after:
            opened = moment
        elif before >= 2 > after and opened is not None:
            if moment - opened >= MIN_OVERLAP:
                if found and opened - found[-1][1] < 0.2:
                    found[-1][1] = round(moment, 2)
                else:
                    found.append([round(opened, 2), round(moment, 2)])
            opened = None
        if len(found) >= MAX_OVERLAPS:
            break
    return found


def _quote(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= QUOTE_CHARS else text[:QUOTE_CHARS - 1].rstrip() + "…"


def build(transcript, duration=0, stored_waveform=None, employees=()) -> dict | None:
    """Speakers with colours and shares, merged turns, cross-talk and the waveform; None without timed speech."""
    rows = []
    for seq, segment in enumerate(transcript or []):
        if not isinstance(segment, dict):
            continue
        start, end = _seconds(segment.get("start")), _seconds(segment.get("end"))
        if start is None:
            continue
        end = start if end is None or end < start else end
        rows.append((seq, start, end, label_of(segment), segment))
    if not rows:
        return None
    rows.sort(key=lambda row: (row[1], row[0]))
    total = max([_seconds(duration) or 0.0] + [row[2] for row in rows])
    if total <= 0:
        return None

    order, by_label = [], {}
    for row in rows:
        if row[3] not in by_label:
            by_label[row[3]] = []
            order.append(row[3])
        by_label[row[3]].append(row)
    index = {label: number for number, label in enumerate(order)}
    names = {str(e.name).casefold(): e.id for e in employees or () if getattr(e, "name", "")}

    segments = [[seq, index[label], round(start, 2), round(end, 2)] for seq, start, end, label, _ in rows]
    turns = []
    for position, (seq, speaker, start, end) in enumerate(segments):
        last = turns[-1] if turns else None
        if last and last[0] == speaker and start - last[2] <= MERGE_GAP:
            last[2] = max(last[2], end)
            last[4] = position
        else:
            turns.append([speaker, start, end, position, position])

    spoken = [_union([(start, end) for _, start, end, _, _ in by_label[label]]) for label in order]
    shares = _shares(spoken)
    people = []
    for number, label in enumerate(order):
        own = [row[4] for row in by_label[label]]
        status = _status(label, own)
        employee_id = next((row.get("speaker_employee_id") for row in own
                            if isinstance(row.get("speaker_employee_id"), int) and not isinstance(row.get("speaker_employee_id"), bool)), None)
        if employee_id is None and status != "guest":
            employee_id = names.get(label.casefold())
        longest = sorted(by_label[label], key=lambda row: (-(row[2] - row[1]), row[1]))
        samples = [{"seq": seq, "start": start, "end": end, "text": _quote(segment.get("text"))}
                   for seq, start, end, _, segment in longest if str(segment.get("text") or "").strip()][:SAMPLE_QUOTES]
        label_text, tone, hint = STATUS[status]
        people.append({
            "key": label, "name": label, "n": number + 1, "color": PALETTE[number % len(PALETTE)],
            "seconds": round(spoken[number], 1), "share": shares[number], "status": status,
            "status_label": label_text, "status_tone": tone, "status_hint": hint,
            "turns": sum(1 for turn in turns if turn[0] == number), "employee_id": employee_id,
            "samples": sorted(samples, key=lambda sample: sample["start"]),
            "manual": any(row.get("speaker_source") == "manual" for row in own),
        })

    outline = waveform.clean(stored_waveform)
    return {
        "duration": round(total, 2),
        "speakers": people,
        "segments": segments,
        "turns": turns,
        "overlaps": _overlaps(segments),
        "peaks": outline["peaks"] if outline else None,
        "peaks_duration": outline["duration"] if outline else None,
        "synthetic": bool(outline and outline["synthetic"]),
        "speech_seconds": round(_union([(row[1], row[2]) for row in rows]), 1),
        "voice_matched": sum(1 for person in people if person["status"] == "voice"),
        "unknown": [person for person in people if person["status"] == "unknown"],
        "has_manual": any(person["manual"] for person in people),
    }


def client_payload(diagram: dict) -> dict:
    """What the browser needs to draw: no quotes, the transcript text is already on the page."""
    return {
        "duration": diagram["duration"],
        "speakers": [{"name": p["name"], "n": p["n"], "color": p["color"], "share": p["share"], "seconds": p["seconds"],
                      "status": p["status"], "statusLabel": p["status_label"], "turns": p["turns"]} for p in diagram["speakers"]],
        "segments": diagram["segments"],
        "turns": diagram["turns"],
        "overlaps": diagram["overlaps"],
        "peaks": diagram["peaks"],
        "peaksDuration": diagram["peaks_duration"],
        "synthetic": diagram["synthetic"],
    }


def guest_name(transcript, wanted: str, replacing: str) -> str:
    """The typed guest name, or the first free «Гость», «Гость 2», …"""
    wanted = " ".join(str(wanted or "").split())[:80]
    if wanted:
        return wanted
    taken = {label_of(s) for s in transcript or [] if isinstance(s, dict) and label_of(s) != replacing}
    number = 1
    while (GUEST if number == 1 else f"{GUEST} {number}") in taken:
        number += 1
    return GUEST if number == 1 else f"{GUEST} {number}"


_AUTO_KEYS = ("speaker", "speaker_source", "speaker_employee_id", "speaker_similarity", "speaker_confidence")
_MANUAL_KEYS = ("speaker_guest", "speaker_assigned_by", "speaker_auto")


def relabel(segment: dict, name: str, employee_id=None, guest=False, by: str = "") -> dict:
    """A copy of one replica that now belongs to `name`; its first automatic labelling stays in `speaker_auto`."""
    updated = dict(segment)
    if "speaker_auto" not in updated:
        updated["speaker_auto"] = {key: segment[key] for key in _AUTO_KEYS if key in segment}
    for key in ("speaker_similarity", "speaker_confidence", "speaker_employee_id", "speaker_guest"):
        updated.pop(key, None)
    updated.update(speaker=name, speaker_source="manual", speaker_assigned_by=str(by or "")[:120])
    if employee_id is not None:
        updated["speaker_employee_id"] = int(employee_id)
    if guest:
        updated["speaker_guest"] = True
    return updated


def assign(transcript, label: str, name: str, employee_id=None, guest=False, by: str = "") -> tuple[list, int]:
    """(new transcript, changed replicas): every replica of `label` now belongs to `name`.

    The first automatic labelling of each replica is kept in `speaker_auto`, so the correction can be undone."""
    changed, result = 0, []
    for segment in transcript or []:
        if not isinstance(segment, dict) or label_of(segment) != label:
            result.append(segment)
            continue
        result.append(relabel(segment, name, employee_id, guest, by))
        changed += 1
    return result, changed


def reset(transcript) -> tuple[list, int]:
    """(transcript with the automatic labelling restored, restored replicas)."""
    changed, result = 0, []
    for segment in transcript or []:
        if not isinstance(segment, dict) or not isinstance(segment.get("speaker_auto"), dict):
            result.append(segment)
            continue
        restored = {key: value for key, value in segment.items() if key not in _AUTO_KEYS and key not in _MANUAL_KEYS}
        restored.update(segment["speaker_auto"])
        restored.setdefault("speaker", "")
        result.append(restored)
        changed += 1
    return result, changed
