"""Who speaks when, heard from the sound itself, on this server: no outside service and no labels of the recognition engine.

The other ways (diarize.py) start from the labels a speech-to-text engine gives each 45–90 second part and try to
repair them. This one listens to the recording directly, the way the open pyannote pipeline does:

1. A segmentation network (pyannote segmentation-3.0, MIT) hears every 10 seconds of the recording several times, in
   overlapping windows, and marks for each 17 ms which of up to three voices of that window speak — two at once too
   (cross-talk). It sees a change of voice inside a phrase and a one-word reply.
2. A voice print network (CAM++ of 3D-Speaker, Apache-2.0: 192 numbers per voice) gives a print to every voice of
   every window, from the sound where that voice speaks alone.
3. The prints of the whole meeting are clustered: one cluster is one person. Voices of one window are different
   people by construction, so they are never joined.
4. Every 17 ms of the recording gets its people; the replicas of the transcript take the person who speaks over
   them, and a replica two people share is cut where the voice changes (diarize.py).

Both networks are ONNX files in app/models and run on one CPU thread with onnxruntime, like the speech detector
(vad.py). The recording is decoded in blocks, so two hours do not need two hours of samples in memory.

The engine is measured on open recordings with a known answer (scripts/diarization_benchmark.py); the numbers and the
choice of the networks are in docs/SPEAKERS.md.
"""
from __future__ import annotations

import logging
import math
import pathlib
import threading
import time

from . import vad

log = logging.getLogger("sysai.voice_local")

MODELS = pathlib.Path(__file__).resolve().parent / "models"
SEGMENTATION = MODELS / "pyannote_segmentation_3.onnx"
EMBEDDING = MODELS / "campplus_voice.onnx"
MODEL_VERSION = "local-campplus-zh-en-advanced-v1"   # the voice prints of another model are never compared with these
RATE = 16000
WINDOW = 10 * RATE         # the segmentation network hears ten seconds at a time
HOP = 270                  # samples between two of its decisions (16.9 ms)
FRAME = HOP / RATE
FIELD = 991                # samples one decision is made from
STEP_SECONDS = 5.0         # a new window this often: every moment is heard twice (2.5 s is no more accurate, twice the work)
BLOCK_SECONDS = 60.0       # decoded at a time: a small server has 512 MB for everything
BATCH = 2
LOCAL = 3                  # voices the network tells apart inside one window
# the network answers with one of seven classes per frame: silence, one of three voices, or a pair of them
POWERSET = ((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, 0, 1), (0, 1, 1))
MIN_PRINT = 0.2            # seconds of a voice in a window needed to make any print of it
STRONG_PRINT = 2.0         # …and of the voice alone for a print that takes part in the clustering
PRINT_PAD = 1.0            # a shorter sound is repeated up to this length: the network is unsure on a fraction of a second
MAX_CLUSTERED = 1800       # the clearest prints that are clustered; the rest join the clusters found (memory, time)
PURE = 0.6                 # prints this alike on average surely are one person: the first, careful stage
THRESHOLD = 0.55           # two groups whose average prints are this alike are one person
COUNT_SLACK = 0.2          # with a known number of participants, merge down to it while this much below the threshold
SMALL_PRINTS = 3           # a cluster of fewer strong prints is most likely a known voice heard badly…
SMALL_KEEP = 0.25          # …unless it is this unlike everyone else: then it is a person who said little
NEAR_SECONDS = 90.0        # a weak print prefers the people who speak this close to it
NEAR_BONUS = 0.08
FILL_GAP = 0.25            # a shorter hole inside one person's speech is not a pause
MIN_TURN = 0.12            # a shorter flash of a voice is noise


class LocalVoiceError(RuntimeError):
    """The engine could not label the recording (the message is shown in Russian)."""


_lock = threading.Lock()
_sessions: dict = {}
_filters = None


def available() -> bool:
    """True when both networks and onnxruntime are present."""
    if not SEGMENTATION.is_file() or not EMBEDDING.is_file():
        return False
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def release() -> None:
    """Free the networks (about 100 MB) until they are needed again."""
    with _lock:
        _sessions.clear()


def _session(path: pathlib.Path):
    with _lock:
        if path not in _sessions:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1   # a small server: leave the other cores to the web requests
            options.inter_op_num_threads = 1
            options.log_severity_level = 3
            options.enable_cpu_mem_arena = False   # the arena keeps the largest input ever seen: 512 MB servers
            _sessions[path] = ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])
        return _sessions[path]


# ------------------------------------------------------------------------------------------------ voice prints
def _mel_filters():
    """Kaldi's 80 triangular mel filters over a 512-point spectrum (20 Hz – 8 kHz)."""
    global _filters
    if _filters is None:
        import numpy as np

        def mel(f):
            return 1127.0 * np.log(1.0 + f / 700.0)
        edges = np.linspace(mel(20.0), mel(RATE / 2), 82)
        bins = mel(np.arange(257) * RATE / 512.0)
        left, centre, right = edges[:-2, None], edges[1:-1, None], edges[2:, None]
        up = (bins[None, :] - left) / (centre - left)
        down = (right - bins[None, :]) / (right - centre)
        _filters = np.maximum(0.0, np.minimum(up, down)).astype(np.float32)
    return _filters


def features(samples):
    """Kaldi-compatible log-mel features (frames × 80) of mono 16 kHz samples in −1…1, mean removed per recording."""
    import numpy as np
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size < 400:
        raise LocalVoiceError("Звук слишком короткий для отпечатка голоса.")
    frames = np.lib.stride_tricks.sliding_window_view(samples, 400)[::160].astype(np.float32)
    frames = frames - frames.mean(axis=1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * (1 - 0.97), frames[:, 1:] - 0.97 * frames[:, :-1]], axis=1)
    window = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(400) / 399.0)) ** 0.85
    spectrum = np.fft.rfft(frames * window.astype(np.float32), n=512, axis=1)
    power = (spectrum.real ** 2 + spectrum.imag ** 2).astype(np.float32)
    out = np.log(np.maximum(power @ _mel_filters().T, 1.1920929e-07))
    out -= out.mean(axis=0, keepdims=True)
    return np.ascontiguousarray(out, dtype=np.float32)


def embed(samples):
    """The voice print of a piece of speech: a unit vector (numpy), or None when the sound gives none."""
    import numpy as np
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size < int(MIN_PRINT * RATE):
        return None
    if samples.size < int(PRINT_PAD * RATE):
        samples = np.tile(samples, int(math.ceil(PRINT_PAD * RATE / samples.size)))
    session = _session(EMBEDDING)
    vector = session.run(None, {session.get_inputs()[0].name: features(samples)[None, :, :]})[0].reshape(-1)
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm < 1e-8:
        return None
    return (vector / norm).astype(np.float32)


def embed_labels(path, intervals: dict, limit: float = 120.0) -> dict:
    """The voice print of each label from its speech: {label: [(start, end)] seconds} → {label: (vector as a list,
    seconds used)}; at most `limit` seconds per label, the recording is read once."""
    import numpy as np
    wanted = {label: sorted((max(0.0, float(a)), float(b)) for a, b in spans if b > a)
              for label, spans in intervals.items()}
    parts = {label: [] for label in wanted}
    taken = dict.fromkeys(wanted, 0)
    last = max((spans[-1][1] for spans in wanted.values() if spans), default=0.0)
    at, most = 0, int(limit * RATE)
    for block in vad._blocks(path, BLOCK_SECONDS):
        for label, spans in wanted.items():
            for a, b in spans:
                first, stop = max(int(a * RATE), at), min(int(b * RATE), at + len(block))
                if stop > first and taken[label] < most:
                    piece = block[first - at:stop - at][:most - taken[label]].copy()
                    parts[label].append(piece)
                    taken[label] += len(piece)
        at += len(block)
        if at > last * RATE:
            break
    out = {}
    for label, pieces in parts.items():
        vector = embed(np.concatenate(pieces)) if pieces else None
        if vector is not None:
            out[label] = ([float(v) for v in vector], round(taken[label] / RATE, 2))
    return out


# ------------------------------------------------------------------------------------------------ segmentation
def _windows(path, step: int):
    """(number, ten seconds of samples, seconds of real sound in them) for every window of a recording."""
    import numpy as np
    buffer, start, number, total = np.zeros(0, dtype=np.float32), 0, 0, 0
    for block in vad._blocks(path, BLOCK_SECONDS):
        buffer = np.concatenate([buffer, block]) if buffer.size else block
        total += len(block)
        while number * step + WINDOW <= start + len(buffer):
            first = number * step - start
            yield number, buffer[first:first + WINDOW], WINDOW / RATE
            number += 1
        drop = number * step - start
        if drop > 0:
            buffer, start = buffer[drop:], start + drop
    while number == 0 or number * step < total - RATE // 2:   # the tail, padded with silence
        first = number * step - start
        piece = buffer[first:first + WINDOW] if 0 <= first < len(buffer) else np.zeros(0, dtype=np.float32)
        if number and piece.size < RATE // 2:
            break
        padded = np.zeros(WINDOW, dtype=np.float32)
        padded[:piece.size] = piece
        yield number, padded, piece.size / RATE
        number += 1


def _activity(batch):
    """Who of the three voices of a window speaks in each frame: (windows, frames, 3) of 0/1."""
    import numpy as np
    session = _session(SEGMENTATION)
    scores = session.run(None, {session.get_inputs()[0].name: batch[:, None, :]})[0]
    return np.asarray(POWERSET, dtype=np.uint8)[scores.argmax(axis=2)]


def _runs(mask):
    """[(first, last)) of the stretches of True in a boolean array."""
    import numpy as np
    padded = np.concatenate([[False], mask, [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return list(zip(edges[::2], edges[1::2]))


def _voice_sound(samples, mask):
    """The samples of a window under the frames of a mask."""
    import numpy as np
    parts = [samples[max(0, a * HOP):min(len(samples), b * HOP + (FIELD - HOP) // 2)] for a, b in _runs(mask)]
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


def listen(path, step_seconds: float = STEP_SECONDS, progress=None, total: float = 0.0, embedder=None) -> dict:
    """Hear the whole recording: {"step", "windows": [activity (frames × 3)], "prints": [{"window", "local", "vector",
    "alone", "seconds"}]} — the voices of every window and their prints."""
    import numpy as np
    embedder = embedder or embed
    step = int(round(step_seconds * RATE))
    windows, prints, pending = [], [], []
    said = [time.monotonic()]

    def flush():
        if not pending:
            return
        activity = _activity(np.stack([samples for _, samples, _ in pending]))
        for (number, samples, real), active in zip(pending, activity):
            frames = min(active.shape[0], int(real * RATE / HOP) + 1)
            active = active.copy()
            active[frames:] = 0
            windows.append(active)
            alone = active.sum(axis=1) == 1
            for local in range(LOCAL):
                mine = active[:, local].astype(bool)
                seconds = float(mine.sum()) * FRAME
                if seconds < MIN_PRINT:
                    continue
                clean = mine & alone
                clean_seconds = float(clean.sum()) * FRAME
                vector = embedder(_voice_sound(samples, clean if clean_seconds >= min(seconds, 0.5) else mine))
                if vector is not None:
                    prints.append({"window": number, "local": local, "vector": vector,
                                   "alone": round(clean_seconds, 2), "seconds": round(seconds, 2)})
        if progress and time.monotonic() - said[0] > 5:
            said[0] = time.monotonic()
            at = pending[-1][0] * step_seconds
            progress(f"Голоса по звуку: {int(at // 60)} мин" + (f" из {int(total // 60)}" if total else ""))
        pending.clear()

    for item in _windows(path, step):
        pending.append(item)
        if len(pending) >= BATCH:
            flush()
    flush()
    return {"step": step, "windows": windows, "prints": prints}


# -------------------------------------------------------------------------------------------------- clustering
def _linkage(sim, threshold: float):
    """Average-linkage clustering of a similarity matrix (numpy, changed in place): the group of every row."""
    import numpy as np
    n = sim.shape[0]
    size = np.ones(n, dtype=np.float32)
    alive = np.ones(n, dtype=bool)
    group = np.arange(n)
    np.fill_diagonal(sim, -2.0)
    best = sim.argmax(axis=1)
    score = sim[np.arange(n), best]
    left = n
    while left > 1:
        a = int(np.where(alive, score, -3.0).argmax())
        b = int(best[a])
        if score[a] < threshold:
            break
        merged = (size[a] * sim[a] + size[b] * sim[b]) / (size[a] + size[b])
        merged[~alive] = -2.0
        merged[a] = merged[b] = -2.0
        sim[a, :] = merged
        sim[:, a] = merged
        sim[b, :] = -2.0
        sim[:, b] = -2.0
        size[a] += size[b]
        alive[b] = False
        group[group == b] = a
        left -= 1
        stale = np.flatnonzero(alive & ((best == a) | (best == b)))
        for k in stale:
            best[k] = sim[k].argmax()
            score[k] = sim[k, best[k]]
        better = alive & (merged > score)
        best[better], score[better] = a, merged[better]
        best[a] = merged.argmax()
        score[a] = merged[best[a]]
    return group


def _unit_rows(matrix):
    import numpy as np
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-9)


def cluster(prints: list, threshold: float = THRESHOLD, people: int = 0) -> tuple:
    """The person (0, 1, …) of every print and the average print of every person: (list, matrix).

    Two stages. First the clear prints (a voice alone for STRONG_PRINT seconds) are gathered into small sure groups:
    only prints very alike (PURE) join. Then the groups are joined by their average prints: an average of many
    prints carries far less noise of the room than one print, so the same limit works for a studio and for a far
    microphone. A group of a few prints joins a large one it resembles; at the end every print — weak ones too —
    goes to the closest person, the voices of one window to different people."""
    import numpy as np
    if not prints:
        return [], np.zeros((0, 0), dtype=np.float32)
    vectors = np.stack([p["vector"] for p in prints]).astype(np.float32)
    alone = np.array([p["alone"] for p in prints], dtype=np.float32)
    window = np.array([p["window"] for p in prints])
    strong = np.flatnonzero(alone >= STRONG_PRINT)
    if strong.size < 2:   # a short recording: the best that there is
        strong = np.argsort(-alone)[:max(2, min(len(prints), 20))]
    if strong.size > MAX_CLUSTERED:
        strong = np.sort(strong[np.argsort(-alone[strong], kind="stable")[:MAX_CLUSTERED]])
    chosen = vectors[strong]
    group = _linkage(chosen @ chosen.T, max(PURE, threshold))
    roots = list(dict.fromkeys(group.tolist()))
    weight = np.maximum(alone[strong], 0.1)
    sums = np.stack([(chosen[group == root] * weight[group == root][:, None]).sum(axis=0) for root in roots])
    counts = np.array([(group == root).sum() for root in roots], dtype=np.float32)
    seconds = np.array([weight[group == root].sum() for root in roots], dtype=np.float32)
    sums, counts, seconds = _join(sums, counts, seconds, threshold, people)
    centres = _unit_rows(sums)
    if len(centres) > 1:   # a group of a few prints is a known voice heard badly, unless it is unlike everyone
        large = counts >= SMALL_PRINTS
        if not large.any():
            large[counts.argmax()] = True
        between = centres @ centres.T
        keep = large.copy()
        for k in np.flatnonzero(~large):
            if between[k][large].max() < threshold - SMALL_KEEP and seconds[k] >= 2 * STRONG_PRINT:
                keep[k] = True
        centres = centres[keep]
    person = _assign(vectors, window, centres, alone)
    for _ in range(2):   # the average prints follow everything their person said
        centres = _centres(vectors, alone, person, len(centres), centres)
        person = _assign(vectors, window, centres, alone)
    used = sorted(set(person))
    renumber = {old: new for new, old in enumerate(used)}
    return [renumber[p] for p in person], centres[used]


def _join(sums, counts, seconds, threshold: float, people: int = 0):
    """Join the groups whose average prints are alike, the most alike first; with a known number of participants go
    on a little below the limit while there are more large groups than people."""
    import numpy as np
    sums, counts, seconds = sums.copy(), counts.copy(), seconds.copy()
    while len(sums) > 1:
        centres = _unit_rows(sums)
        between = centres @ centres.T
        np.fill_diagonal(between, -2.0)
        a, b = np.unravel_index(int(between.argmax()), between.shape)
        large = int((counts >= SMALL_PRINTS).sum())
        limit = threshold - COUNT_SLACK if people and large > people else threshold
        if between[a, b] < limit:
            break
        sums[a] += sums[b]
        counts[a] += counts[b]
        seconds[a] += seconds[b]
        keep = np.arange(len(sums)) != b
        sums, counts, seconds = sums[keep], counts[keep], seconds[keep]
    return sums, counts, seconds


def _centres(vectors, alone, person, count: int, previous):
    import numpy as np
    out = previous.copy()
    person = np.asarray(person)
    for k in range(count):
        mine = (person == k) & (alone >= STRONG_PRINT)
        if mine.any():
            out[k] = (vectors[mine] * alone[mine][:, None]).sum(axis=0)
    return _unit_rows(out)


def _assign(vectors, window, centres, alone) -> list:
    """Every print to its closest person; the prints of one window to different people (the best such pairing).

    A weak print (a word or two) leans to the people who speak around it."""
    import numpy as np
    scores = vectors @ centres.T
    person = scores.argmax(axis=1)
    window = np.asarray(window)
    strong = np.asarray(alone) >= STRONG_PRINT
    if centres.shape[0] > 1:
        step_windows = max(1, int(NEAR_SECONDS / STEP_SECONDS))   # close enough for any step in use
        order = np.argsort(window, kind="stable")
        sorted_windows = window[order]
        for i in np.flatnonzero(~strong):
            lo = np.searchsorted(sorted_windows, window[i] - step_windows, side="left")
            hi = np.searchsorted(sorted_windows, window[i] + step_windows, side="right")
            near = order[lo:hi]
            near = near[strong[near]]
            if near.size:
                bonus = np.zeros(centres.shape[0], dtype=np.float32)
                bonus[np.unique(person[near])] = NEAR_BONUS
                scores[i] = scores[i] + bonus
        person = scores.argmax(axis=1)
    by_window: dict = {}
    for index, number in enumerate(window.tolist()):
        by_window.setdefault(number, []).append(index)
    for members in by_window.values():
        if len(members) < 2 or len({int(person[i]) for i in members}) == len(members):
            continue
        taken, table = set(), sorted(((float(scores[i, k]), i, k) for i in members for k in range(centres.shape[0])),
                                     reverse=True)
        done = set()
        for _, i, k in table:
            if i in done or k in taken:
                continue
            person[i] = k
            done.add(i)
            taken.add(k)
    return [int(p) for p in person]


# ---------------------------------------------------------------------------------------------------- timeline
def timeline(heard: dict, person: list, people: int) -> tuple:
    """Who speaks in every frame of the recording: (activity: people × frames of 0/1, seconds per frame).

    Every moment was heard by several windows: a person speaks there when most of them say so, and as many people
    speak at once as the windows heard on average."""
    import numpy as np
    windows, step = heard["windows"], heard["step"]
    if not windows or not people:
        return np.zeros((0, 0), dtype=np.uint8), FRAME
    per_window = windows[0].shape[0]
    offsets = [int(round(number * step / HOP)) for number in range(len(windows))]
    frames = offsets[-1] + per_window
    votes = np.zeros((people, frames), dtype=np.float32)
    count = np.zeros(frames, dtype=np.float32)
    seen = np.zeros(frames, dtype=np.float32)
    weight = np.hamming(per_window).astype(np.float32) + 0.05   # a window is surest in its middle
    mapping = {(p["window"], p["local"]): who for p, who in zip(heard["prints"], person)}
    for number, active in enumerate(windows):
        at = offsets[number]
        seen[at:at + per_window] += weight
        known = np.zeros(per_window, dtype=np.float32)
        for local in range(LOCAL):
            who = mapping.get((number, local))
            if who is not None:
                votes[who, at:at + per_window] += weight * active[:, local]
                known += active[:, local]
        count[at:at + per_window] += weight * np.minimum(known, 2)
    seen = np.maximum(seen, 1e-6)
    speakers = np.rint(count / seen).astype(np.int32)
    out = np.zeros((people, frames), dtype=np.uint8)
    if people == 1:
        out[0] = speakers >= 1
        return out, FRAME
    order = np.argsort(-votes, axis=0)
    columns = np.arange(frames)
    first, second = order[0], order[1]
    out[first[speakers >= 1], columns[speakers >= 1]] = 1
    both = (speakers >= 2) & (votes[second, columns] > 0)
    out[second[both], columns[both]] = 1
    return out, FRAME


def turns(activity, frame: float = FRAME) -> list:
    """[[start, end, person]] of everything each person says, in time order; holes and flashes are smoothed out."""
    out = []
    for who in range(activity.shape[0]):
        merged = []
        for a, b in _runs(activity[who].astype(bool)):
            start, end = a * frame, b * frame
            if merged and start - merged[-1][1] <= FILL_GAP:
                merged[-1][1] = end
            else:
                merged.append([start, end])
        out.extend([round(a, 2), round(b, 2), who] for a, b in merged if b - a >= MIN_TURN)
    out.sort()
    return out


def diarize(path, people: int = 0, threshold: float = THRESHOLD, progress=None, total: float = 0.0,
            step_seconds: float = STEP_SECONDS) -> dict:
    """The whole way: {"turns": [[start, end, person]], "centres": [voice print per person], "seconds": [speech per
    person], "model", "took"}."""
    if not available():
        raise LocalVoiceError("Модели разметки голосов не найдены на сервере (app/models).")
    started = time.monotonic()
    try:
        try:
            heard = listen(path, step_seconds, progress, total)
        except vad.VADError as exc:
            raise LocalVoiceError(str(exc)) from exc
        if not heard["prints"]:
            raise LocalVoiceError("В записи не слышно речи — размечать голоса не по чему.")
        if progress:
            progress("Голоса по звуку: собираем голоса всей встречи")
        person, centres = cluster(heard["prints"], threshold, people)
        activity, frame = timeline(heard, person, len(centres))
    finally:
        release()
    found = turns(activity, frame)
    seconds = [round(float(activity[who].sum()) * frame, 1) for who in range(activity.shape[0])]
    return {"turns": found, "centres": [[float(v) for v in row] for row in centres], "seconds": seconds,
            "model": MODEL_VERSION, "took": round(time.monotonic() - started, 1)}
