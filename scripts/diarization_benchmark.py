"""Measure the local speaker engine (app/voice_local.py) on recordings with a known answer.

    python scripts/diarization_benchmark.py FOLDER [--threshold 0.5] [--step 2.5] [--people] [--collar 0.25]

FOLDER holds pairs NAME.wav + NAME.json, the answer being [[start, end, "speaker"], …] in seconds (the open sets
VoxConverse and AMI in this form: docs/SPEAKERS.md). Prints the diarization error rate (DER) of every recording:
missed speech + false speech + speech given to the wrong person, as a share of all speech; cross-talk is counted.
"""
import argparse
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app import audio, voice_local  # noqa: E402
from app.diarization_eval import _assignment  # noqa: E402

GRID = 0.01


def grid(rows, labels, frames):
    out = np.zeros((len(labels), frames), dtype=bool)
    for start, end, label in rows:
        out[labels.index(label), max(0, int(round(start / GRID))):min(frames, int(round(end / GRID)))] = True
    return out


def der(reference, hypothesis, collar=0.0):
    """(DER, missed, false alarm, confusion) as shares of the reference speech."""
    end = max([row[1] for row in reference] + [row[1] for row in hypothesis] + [1.0])
    frames = int(end / GRID) + 1
    ref_labels = sorted({row[2] for row in reference})
    hyp_labels = sorted({row[2] for row in hypothesis})
    ref, hyp = grid(reference, ref_labels, frames), grid(hypothesis, hyp_labels, frames)
    scored = np.ones(frames, dtype=bool)
    if collar:
        for start, stop, _ in reference:
            for edge in (start, stop):
                scored[max(0, int((edge - collar) / GRID)):int((edge + collar) / GRID) + 1] = False
    ref, hyp = ref[:, scored], hyp[:, scored]
    together = ref.astype(np.float32) @ hyp.astype(np.float32).T if len(hyp_labels) else np.zeros((len(ref_labels), 0))
    size = max(together.shape)
    cost = np.zeros((size, size))
    cost[:together.shape[0], :together.shape[1]] = -together
    pairs = _assignment(cost.tolist())
    mapped = np.zeros_like(ref)
    for r, h in enumerate(pairs[:len(ref_labels)]):
        if h < len(hyp_labels):
            mapped[r] = hyp[h]
    n_ref, n_hyp = ref.sum(axis=0), hyp.sum(axis=0)
    correct = (ref & mapped).sum(axis=0)
    total = max(1, n_ref.sum())
    missed = np.maximum(0, n_ref - n_hyp).sum()
    false = np.maximum(0, n_hyp - n_ref).sum()
    confusion = (np.minimum(n_ref, n_hyp) - correct).sum()
    return tuple(float(x) / total for x in (missed + false + confusion, missed, false, confusion))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--threshold", type=float, default=voice_local.THRESHOLD)
    parser.add_argument("--step", type=float, default=voice_local.STEP_SECONDS)
    parser.add_argument("--people", action="store_true", help="tell the engine how many people speak")
    parser.add_argument("--collar", type=float, default=0.25)
    parser.add_argument("--only", default="")
    args = parser.parse_args()
    rows = []
    for wav in sorted(pathlib.Path(args.folder).glob(f"{args.only}*.wav")):
        reference = json.loads(wav.with_suffix(".json").read_text())
        people = len({row[2] for row in reference})
        started = time.monotonic()
        result = voice_local.diarize(wav, people=people if args.people else 0, threshold=args.threshold,
                                     step_seconds=args.step)
        took = time.monotonic() - started
        seconds = audio.duration(str(wav))
        score = der(reference, result["turns"], args.collar)
        rows.append((seconds, score))
        print(f"{wav.stem:10s} {seconds / 60:5.1f} мин  людей {people:2d} → {len(result['centres']):2d}   "
              f"DER {score[0]:6.1%}  (пропуск {score[1]:5.1%}, лишнее {score[2]:5.1%}, не тот {score[3]:5.1%})   "
              f"{took:5.0f} с = {took / seconds:.3f}× записи", flush=True)
    if rows:
        whole = sum(seconds for seconds, _ in rows)
        mean = [sum(seconds * score[k] for seconds, score in rows) / whole for k in range(4)]
        print(f"В среднем по времени: DER {mean[0]:.1%} (пропуск {mean[1]:.1%}, лишнее {mean[2]:.1%}, не тот {mean[3]:.1%})")


if __name__ == "__main__":
    main()
