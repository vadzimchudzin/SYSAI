"""How close the automatic speaker labelling is to a corrected reference («эталон»).

A reference is a meeting transcript a person corrected until it was right (transcript_edit.py), frozen together with
the automatic labelling it started from. Both are compared on a 0.1-second grid of the reference speech:

- «Говорящий верный» — the share of speech where the automatic speaker, after the best one-to-one matching of the labels,
  is the reference one (one minus the speaker confusion of the diarization error rate; missed speech counts as wrong);
- «Лишние голоса» — automatic labels that mostly are a person another label already is (one person split in several);
- «Склейки» — automatic labels holding a second person for a noticeable time (two people as one voice);
- «Имена» — of the time the automatic labelling put a person's name, the share where the name is right, and of the
  reference time with an employee's name, the share named right;
- «Короткие реплики» — reference replicas up to 2 seconds or 3 words whose automatic speaker is right.
"""
import copy
import datetime as dt
import math
from collections import Counter

from . import speakers as speaker_names
from . import transcript_edit, voice_diagram

STEP = 0.1              # seconds per grid cell
SHORT_SECONDS = 2.0
SHORT_WORDS = 3
MERGE_SHARE = 0.15      # a second person in one label: at least this share of the label…
MERGE_SECONDS = 5.0     # …and at least this long
MIN_LABEL_SECONDS = 1.0  # shorter labels are noise, not a voice


def _seconds(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _norm(label) -> str:
    return " ".join(str(label or "").casefold().replace("ё", "е").split())


def _named(label) -> bool:
    return bool(label) and label != voice_diagram.UNLABELLED and not speaker_names.anonymous(label)


def _rows(transcript) -> list:
    rows = []
    for segment in transcript or []:
        if not isinstance(segment, dict):
            continue
        start, end = _seconds(segment.get("start")), _seconds(segment.get("end"))
        if start is None or end is None or end <= start:
            continue
        rows.append((start, end, voice_diagram.label_of(segment), segment))
    return rows


def _cells(start, end, frames):
    return range(min(frames, int(round(start / STEP))), min(frames, int(round(end / STEP))))


def _grid(rows, frames) -> list:
    labels = [None] * frames
    for start, end, label, _ in rows:
        for cell in _cells(start, end, frames):
            labels[cell] = label
    return labels


def _order(labels) -> list:
    seen = {}
    for label in labels:
        if label is not None and label not in seen:
            seen[label] = len(seen)
    return list(seen)


def _assignment(cost) -> list:
    """Hungarian method for a square matrix: the column of each row that makes the total cost the least."""
    n = len(cost)
    inf = float("inf")
    u, v, owner, way = [0.0] * (n + 1), [0.0] * (n + 1), [0] * (n + 1), [0] * (n + 1)
    for row in range(1, n + 1):
        owner[0], column = row, 0
        least, used = [inf] * (n + 1), [False] * (n + 1)
        while True:
            used[column] = True
            current, delta, chosen = owner[column], inf, 0
            for j in range(1, n + 1):
                if not used[j]:
                    value = cost[current - 1][j - 1] - u[current] - v[j]
                    if value < least[j]:
                        least[j], way[j] = value, column
                    if least[j] < delta:
                        delta, chosen = least[j], j
            for j in range(n + 1):
                if used[j]:
                    u[owner[j]] += delta
                    v[j] -= delta
                else:
                    least[j] -= delta
            column = chosen
            if owner[column] == 0:
                break
        while column:
            previous = way[column]
            owner[column] = owner[previous]
            column = previous
    result = [0] * n
    for j in range(1, n + 1):
        if owner[j]:
            result[owner[j] - 1] = j - 1
    return result


def _matching(together: Counter, hyp_labels: list, ref_labels: list) -> dict:
    """{automatic label: reference speaker} that covers the most speech, one to one."""
    if not hyp_labels or not ref_labels:
        return {}
    size = max(len(hyp_labels), len(ref_labels))
    weight = [[together.get((h, r), 0) if i < len(hyp_labels) and j < len(ref_labels) else 0
               for j, r in enumerate(ref_labels + [None] * (size - len(ref_labels)))]
              for i, h in enumerate(hyp_labels + [None] * (size - len(hyp_labels)))]
    top = max(max(row) for row in weight)
    picked = _assignment([[top - value for value in row] for row in weight])
    return {hyp_labels[i]: ref_labels[j] for i, j in enumerate(picked)
            if i < len(hyp_labels) and j < len(ref_labels) and weight[i][j] > 0}


def _share(part, whole):
    return round(part / whole, 4) if whole else None


def evaluate(reference, hypothesis) -> dict:
    """Metrics of `hypothesis` (an automatic transcript) against `reference` (the corrected one)."""
    ref_rows, hyp_rows = _rows(reference), _rows(hypothesis)
    total = max([end for _, end, _, _ in ref_rows + hyp_rows], default=0.0)
    frames = int(math.ceil(total / STEP))
    ref, hyp = _grid(ref_rows, frames), _grid(hyp_rows, frames)
    speech = sum(1 for label in ref if label is not None)
    together = Counter((h, r) for h, r in zip(hyp, ref) if h is not None and r is not None)
    ref_labels, hyp_labels = _order(ref), _order(hyp)
    mapping = _matching(together, hyp_labels, ref_labels)
    correct = sum(together.get((h, r), 0) for h, r in mapping.items())

    inside = {h: Counter() for h in hyp_labels}
    for (h, r), count in together.items():
        inside[h][r] += count
    voices = [h for h in hyp_labels if sum(inside[h].values()) * STEP >= MIN_LABEL_SECONDS]
    dominant = {h: inside[h].most_common(1)[0][0] for h in voices}
    extra = len(voices) - len(set(dominant.values()))
    split = []
    for person in ref_labels:
        parts = sorted(((h, inside[h][person]) for h in voices if dominant[h] == person), key=lambda item: -item[1])
        if len(parts) > 1:
            split.append({"person": person, "labels": [[h, round(count * STEP, 1)] for h, count in parts]})
    merges = []
    for h in voices:
        top = inside[h].most_common(2)
        if len(top) == 2:
            whole = sum(inside[h].values())
            if top[1][1] * STEP >= MERGE_SECONDS and top[1][1] >= MERGE_SHARE * whole:
                merges.append({"label": h, "people": [[r, round(count * STEP, 1)] for r, count in top]})

    guests = {voice_diagram.label_of(seg) for _, _, _, seg in ref_rows if seg.get("speaker_guest")}
    named = sum(1 for h, r in zip(hyp, ref) if r is not None and _named(h))
    named_right = sum(1 for h, r in zip(hyp, ref) if r is not None and _named(h) and _norm(h) == _norm(r))
    known = sum(1 for r in ref if _named(r) and r not in guests)
    known_right = sum(1 for h, r in zip(hyp, ref) if _named(r) and r not in guests and _norm(h) == _norm(r))

    short = short_right = 0
    for start, end, label, segment in ref_rows:
        if end - start > SHORT_SECONDS and len(str(segment.get("text") or "").split()) > SHORT_WORDS:
            continue
        cells = [hyp[cell] for cell in _cells(start, end, frames) if hyp[cell] is not None]
        if not cells:
            cells = [None]
        short += 1
        heard = Counter(cells).most_common(1)[0][0]
        short_right += int(heard is not None and mapping.get(heard) == label)

    people = []
    for person in ref_labels:
        own = sum(1 for r in ref if r == person)
        right = sum(count for (h, r), count in together.items() if r == person and mapping.get(h) == person)
        people.append({"name": person, "seconds": round(own * STEP, 1), "accuracy": _share(right, own)})
    return {
        "speech_seconds": round(speech * STEP, 1),
        "accuracy": _share(correct, speech),
        "ref_speakers": len(ref_labels),
        "hyp_speakers": len(voices),
        "extra": max(0, extra),
        "split": split,
        "merges": merges,
        "names": {"precision": _share(named_right, named), "named_seconds": round(named * STEP, 1),
                  "recall": _share(known_right, known)},
        "short": {"total": short, "right": short_right, "accuracy": _share(short_right, short)},
        "speakers": people,
        "mapping": mapping,
    }


def combine(results: list) -> dict | None:
    """The same metrics over several references: shares weighted by time and counts, voices summed."""
    results = [r for r in results if isinstance(r, dict) and r.get("speech_seconds")]
    if not results:
        return None

    def weighted(get, weight):
        pairs = [(get(r), weight(r)) for r in results if get(r) is not None and weight(r)]
        whole = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / whole, 4) if whole else None
    short_total = sum(r["short"]["total"] for r in results)
    return {
        "references": len(results),
        "speech_seconds": round(sum(r["speech_seconds"] for r in results), 1),
        "accuracy": weighted(lambda r: r["accuracy"], lambda r: r["speech_seconds"]),
        "extra": sum(r["extra"] for r in results),
        "merges": sum(len(r["merges"]) for r in results),
        "extra_per_meeting": round(sum(r["extra"] for r in results) / len(results), 1),
        "names": {"precision": weighted(lambda r: r["names"]["precision"], lambda r: r["names"]["named_seconds"]),
                  "recall": weighted(lambda r: r["names"]["recall"], lambda r: r["speech_seconds"])},
        "short": {"total": short_total, "right": sum(r["short"]["right"] for r in results),
                  "accuracy": _share(sum(r["short"]["right"] for r in results), short_total)},
    }


# ------------------------------------------------------------------------------------------------- references
def save(meeting, by: str = "") -> dict:
    """Freeze the corrected transcript and the automatic labelling it started from as the meeting's reference."""
    transcript = copy.deepcopy(meeting.transcript or [])
    if not _rows(transcript):
        raise transcript_edit.EditError("В расшифровке нет реплик со временем — эталон не из чего сделать.")
    auto = meeting.transcript_auto if meeting.transcript_auto is not None else transcript_edit.automatic(transcript)
    previous = meeting.diarization_reference if isinstance(meeting.diarization_reference, dict) else {}
    fingerprint = transcript_edit.fingerprint(transcript)
    # the ways compared on the old answer key were measured against it: they stay only when it did not change
    runs = (previous.get("runs") or {}) if previous.get("fingerprint") == fingerprint else {}
    reference = {"transcript": transcript, "auto": copy.deepcopy(auto), "by": str(by or "")[:120],
                 "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                 "fingerprint": fingerprint, "runs": runs}
    meeting.diarization_reference = reference
    return reference


def remove(meeting) -> bool:
    had = meeting.diarization_reference is not None
    meeting.diarization_reference = None
    return had


def summary(meeting) -> dict | None:
    """What the meeting page shows about its reference, or None without one."""
    reference = meeting.diarization_reference
    if not isinstance(reference, dict) or not isinstance(reference.get("transcript"), list):
        return None
    try:
        moment = dt.datetime.fromisoformat(str(reference.get("at")))
    except ValueError:
        moment = None
    return {"metrics": evaluate(reference["transcript"], reference.get("auto") or []),
            "at": moment, "by": reference.get("by", ""),
            "stale": reference.get("fingerprint") != transcript_edit.fingerprint(meeting.transcript),
            "replicas": len(reference["transcript"])}


def pct(value) -> str:
    return "—" if value is None else f"{round(value * 100)}%"


def label_list(pairs) -> str:
    return ", ".join(f"{label} ({transcript_edit.mmss(seconds)})" for label, seconds in pairs)
