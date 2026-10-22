"""Speech benchmark: WER, names and terms, cost — per recognition model and per improvement; plus speaker naming.

    OPENROUTER_API_KEY=... python3 scripts/speech_benchmark.py /tmp/speech_set [--quick]

The folder holds audio files with a `<name>.json` next to each: {"turns": [{"speaker": "...", "text": "..."}]}.
Speakers in the json are looked up in a temporary directory with the same names, so speaker naming can be scored.
"""
import base64
import json
import os
import pathlib
import sys
import tempfile
import time

tmp = tempfile.mkdtemp(prefix="sysai_bench_")
os.environ.update(SYSAI_TESTING="1", DATA_DIR=tmp, DATABASE_URL=f"sqlite:///{tmp}/b.db", SECRET_KEY="bench",
                  ADMIN_PASSWORD="pw")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import asr_eval, audio, db, llm, settings_store, speakers, speech_fix, transcribe  # noqa: E402
from app.db import Employee, SessionLocal  # noqa: E402

GLOSSARY = "ВентСнаб, СтройМонтаж, КС-2, Битрикс, Галерея Борисов"
TERMS = ["ВентСнаб", "СтройМонтаж", "Битрикс", "Галерея", "Борисов", "Вадим", "Анна", "Ольга", "Игорь", "Пётр"]


def setup(names):
    db.init_db()
    settings_store.set_many({"glossary": GLOSSARY, "agent_model": "google/gemini-2.5-flash",
                             "diarization_model": "google/gemini-2.5-flash", "daily_budget_usd": 0})
    aliases = {"Вадим Чудин": "Вадим", "Анна Смирнова": "Аня", "Ольга Кузнецова": "Оля", "Игорь Соколов": "",
               "Пётр Иванов": "Петя"}
    with SessionLocal() as s:
        for n in sorted(names):
            s.add(Employee(name=n, aliases=aliases.get(n, ""), telegram_chat_id=""))
        s.commit()


def spent() -> float:
    return llm.spent_today()


def recognise(path: pathlib.Path, model: str, cleanup: bool, prompt: bool, fix: str) -> str:
    work = pathlib.Path(tempfile.mkdtemp())
    target = work / "a.mp3"
    audio.extract(path, target, 0, audio.duration(str(path)) + 1, cleanup=cleanup)
    settings = settings_store.all_settings()
    if "gemini" in model:
        text_prompt = transcribe.VOICE_PROMPT.replace("Говорит один человек.", "Говорят несколько человек.").format(
            language="русский", glossary=(settings["glossary"] + ", " + ", ".join(speech_fix.vocabulary(settings)))
            if prompt else "нет")
        raw = llm.chat(model, [{"role": "user", "content": [
            {"type": "text", "text": text_prompt},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(target.read_bytes()).decode(), "format": "mp3"}}]}],
            json_mode=True, temperature=0, max_tokens=4000, timeout=180)
        text = (llm.parse_json(raw) or {}).get("text") or ""
    else:
        text = llm.transcribe_audio(target.read_bytes(), model, "mp3", "ru", timeout=180,
                                    prompt=speech_fix.prompt_text(settings) if prompt else "").get("text") or ""
    if fix != "none":
        text = speech_fix.fix_segments([{"text": text}], settings, use_model=(fix == "model"))[0][0]["text"]
    return text


def run(files, model, cleanup, prompt, fix, label):
    edits = words = heard = said = 0
    before, started = spent(), time.time()
    for path, ref in files:
        hyp = recognise(path, model, cleanup, prompt, fix)
        r, h = asr_eval.normalize(ref), asr_eval.normalize(hyp)
        edits += asr_eval.distance(r, h)
        words += len(r)
        a, b = asr_eval.terms_heard(ref, hyp, TERMS)
        heard, said = heard + a, said + b
    result = {"label": label, "model": model, "wer": edits / max(1, words), "terms": heard / max(1, said),
              "cost": spent() - before, "seconds": time.time() - started}
    print(f"{label:52} WER {result['wer']:6.1%}  имена/термины {result['terms']:6.1%}  "
          f"${result['cost']:.4f}  {result['seconds']:.0f} с", flush=True)
    return result


def naming(files, model="openai/gpt-4o-transcribe", verbose=True):
    """The full meeting pipeline (recognition, voices, correction, names) as in production: WER of the transcript and
    the share of speakers named correctly."""
    settings = settings_store.all_settings()
    settings["transcribe_model"] = model
    settings["glossary"] = GLOSSARY + "; имена сотрудников: " + ", ".join(e for e in speech_fix.vocabulary(settings))
    total = named = edits = words = 0
    for path, ref_turns in files:
        _, segments = transcribe.transcribe(str(path), settings)
        with SessionLocal() as s:
            people = s.query(Employee).all()
            segments, mapping = speakers.name_speakers(segments, people)
        ref = asr_eval.normalize(" ".join(t["text"] for t in ref_turns))
        edits += asr_eval.distance(ref, asr_eval.normalize(" ".join(seg["text"] for seg in segments)))
        words += len(ref)
        hyp_text = [(seg["speaker"], set(asr_eval.normalize(seg["text"]))) for seg in segments]
        for person in {t["speaker"] for t in ref_turns}:
            votes = {}
            for turn in (t for t in ref_turns if t["speaker"] == person):
                turn_words = set(asr_eval.normalize(turn["text"]))
                best = max(hyp_text, key=lambda x: len(turn_words & x[1]) / max(1, len(turn_words)), default=None)
                if best and len(turn_words & best[1]) / max(1, len(turn_words)) > 0.5:
                    votes[best[0]] = votes.get(best[0], 0) + 1
            label = max(votes, key=votes.get) if votes else None
            total += 1
            named += label == person
            if verbose:
                print(f"   {path.stem}: {person:16} → {label}")
    print(f"{model:32} WER стенограммы {edits / max(1, words):6.1%}   говорящих по имени: {named} из {total} "
          f"({named / max(1, total):.0%})", flush=True)
    return named, total, edits / max(1, words)


def main(folder: str, quick: bool):
    files, turns, names = [], [], set()
    for meta in sorted(pathlib.Path(folder).glob("*.json")):
        data = json.loads(meta.read_text())
        if not isinstance(data, dict) or "turns" not in data:
            continue  # results of an earlier run
        audio_path = next((meta.with_suffix(ext) for ext in (".mp3", ".wav", ".m4a", ".ogg") if meta.with_suffix(ext).exists()))
        files.append((audio_path, " ".join(t["text"] for t in data["turns"])))
        turns.append((audio_path, data["turns"]))
        names |= {t["speaker"] for t in data["turns"]}
    setup(names)
    print(f"Записей: {len(files)}, слов в эталоне: {sum(len(asr_eval.normalize(r)) for _, r in files)}\n")
    base = "openai/gpt-4o-transcribe"
    results = [run(files, base, False, False, "none", "Было: gpt-4o-transcribe как в проде")]
    results.append(run(files, base, True, False, "none", "+ очистка звука"))
    results.append(run(files, base, True, True, "none", "+ подсказка имён и терминов"))
    results.append(run(files, base, True, True, "rules", "+ исправление по справочнику (правила)"))
    results.append(run(files, base, True, True, "model", "Стало: + исправление моделью (полный конвейер)"))
    if not quick:
        print("\nДругие модели с полным конвейером:")
        for model in ("openai/gpt-4o-mini-transcribe", "openai/whisper-large-v3", "openai/whisper-1",
                      "google/gemini-2.5-flash", "google/gemini-2.5-pro"):
            try:
                results.append(run(files, model, True, True, "model", model))
            except Exception as exc:  # noqa: BLE001
                print(f"{model}: недоступна ({str(exc)[:120]})")
    first, last = results[0], results[4]
    print(f"\nWER: {first['wer']:.1%} → {last['wer']:.1%} (снижение на {1 - last['wer'] / max(1e-9, first['wer']):.0%})")
    print("\nПолный конвейер совещания (стенограмма с говорящими):")
    meeting = {}
    for model in (["openai/gpt-4o-transcribe", "google/gemini-2.5-flash"] +
                  ([] if quick else ["google/gemini-2.5-pro"])):
        meeting[model] = naming(turns, model, verbose=False)
    json.dump({"results": results, "meeting_pipeline": meeting}, open(pathlib.Path(tmp) / "benchmark.json", "w"),
              ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "/tmp/speech_set", "--quick" in sys.argv)
