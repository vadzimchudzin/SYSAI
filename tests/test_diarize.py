"""Who speaks across the whole recording (diarize.py): voice prints clustered over the meeting, the audio model by
half-hour parts, and the labels of the parts kept whenever a way fails."""
import json
import math
import pathlib

import pytest

from app import config, diarize, llm, speaker_id


def vec(*weights):
    """A unit 192-dim voice print mixing the base voices with these weights."""
    raw = list(weights) + [0.0] * (192 - len(weights))
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw]


def group(window, label, embedding, seconds=20.0):
    return {"window": window, "label": label, "embedding": embedding, "seconds": seconds}


def seg(speaker, start, end, text="обсуждаем план работ на эту неделю подробно", **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


ANNA, BORIS, VERA = vec(1, 0, 0), vec(0, 1, 0), vec(0, 0, 1)


def fake_cut(source, folder, start, seconds, name):
    target = pathlib.Path(folder) / name
    target.write_bytes(b"part")
    return target


def test_the_same_voice_in_other_minutes_is_one_person_even_with_another_label():
    groups = [group(0, "Спикер 1", ANNA), group(0, "Спикер 2", BORIS),
              group(1, "Спикер 1", vec(0.1, 1, 0)), group(1, "Спикер 2", vec(1, 0.1, 0)),  # the part swapped them
              group(2, "Спикер 1", vec(1, 0, 0.2))]
    person = diarize.cluster(groups, 0.5)
    assert person == [0, 1, 1, 0, 0]


def test_two_labels_of_one_minute_stay_apart_unless_nearly_the_same_voice():
    alike = [group(0, "Спикер 1", ANNA), group(0, "Спикер 2", vec(1, 1.2, 0))]  # similarity ≈ 0.64 > threshold
    assert diarize.cluster(alike, 0.5) == [0, 1]
    same = [group(0, "Спикер 1", ANNA), group(0, "Спикер 2", vec(1, 0.5, 0))]  # ≈ 0.89: the part split one voice
    assert diarize.cluster(same, 0.5) == [0, 0]


def test_a_label_is_one_person_only_inside_its_part():
    """Parts cut in pauses number their voices anew: «Спикер 1» of part 0 (Anna) is Boris in part 1. A print per minute
    would blend them; prints per part keep them apart and link the same voice across the cut."""
    segments = [seg("Спикер 1", 0, 20, part=0), seg("Спикер 2", 20, 38, part=0),
                seg("Спикер 1", 39, 55, part=1), seg("Спикер 2", 55, 80, part=1),
                seg("Спикер 2", 80, 84, "это мы ещё услышали", recovered=True)]  # heard again later, no part of its own
    rows = diarize._rows(segments, 84)
    assert diarize.windows_of(segments, rows) == ({0: 0, 1: 0, 2: 1, 3: 1, 4: 1}, True)
    groups = [group(0, "Спикер 1", ANNA), group(0, "Спикер 2", BORIS),
              group(1, "Спикер 1", vec(0.05, 1, 0)), group(1, "Спикер 2", vec(1, 0.05, 0))]
    labels = diarize.voice_labels(segments, groups, 0.5, total=84)
    assert labels == ["Спикер 1", "Спикер 2", "Спикер 2", "Спикер 1", "Спикер 1"]
    # without parts (an old transcript) the minute is the window, as before
    plain = [{k: v for k, v in item.items() if k != "part"} for item in segments]
    assert diarize.windows_of(plain, diarize._rows(plain, 84)) == ({0: 0, 1: 0, 2: 0, 3: 0, 4: 1}, False)


def test_old_transcripts_get_their_parts_from_the_saved_results():
    segments = [seg("Спикер 1", 0, 20), seg("Спикер 2", 41.0, 50), seg("Спикер 1", 95.3, 99)]
    diarize.mark_parts(segments, [0.4, 40.95, 95.3])
    assert [item["part"] for item in segments] == [0, 1, 2]
    tagged = [seg("Спикер 1", 0, 20, part=0), seg("Спикер 2", 41.0, 50)]
    assert diarize.mark_parts(tagged, [0, 40]) == tagged and "part" not in tagged[1]  # the transcript knows them


def test_part_starts_are_read_from_the_checkpoints():
    from app import transcribe
    from app.db import Meeting, ProcessingCheckpoint, SessionLocal
    with SessionLocal() as s:
        meeting = Meeting(title="t", status="done")
        s.add(meeting)
        s.commit()
        mid = meeting.id
        rows = [("0", [{"speaker": "Спикер 1", "start": 0.5, "text": "а"}]),
                ("1", [{"speaker": "Спикер 1", "start": 47.2, "text": "б"}, {"speaker": "Спикер 2", "start": 44.1, "text": "в"}]),
                ("1L", [{"speaker": "Спикер 1", "start": 44.1, "text": "в"}]),
                ("2", {"_sysai_checkpoint_state": "split", "half": 30, "overlap": 4}),
                ("2", [{"speaker": "Спикер 1", "start": 90.0, "text": "г"}])]
        for number, (part, data) in enumerate(rows):
            s.add(ProcessingCheckpoint(meeting_id=mid, stage="transcribe", part=part, fingerprint=f"f{number}", data=data))
        s.add(ProcessingCheckpoint(meeting_id=mid, stage="vad", part="0", fingerprint="v", data={"speech": []}))
        s.commit()
    assert transcribe.part_starts(mid) == [0.5, 44.1, 90.0]
    assert transcribe.part_starts(mid + 1) == []


def test_the_number_of_participants_lets_voices_merge_a_little_below_the_threshold():
    groups = [group(0, "Спикер 1", ANNA), group(1, "Спикер 1", vec(1, 1.9, 0)), group(1, "Спикер 2", VERA)]
    # the first two are ≈ 0.47 alike: below 0.5, but within the slack when only two people were there
    assert diarize.cluster(groups, 0.5) == [0, 1, 2]
    assert diarize.cluster(groups, 0.5, people=2) == [0, 0, 1]


def test_a_voice_heard_for_a_moment_joins_the_closest_known_voice():
    groups = [group(0, "Спикер 1", ANNA), group(0, "Спикер 2", BORIS), group(3, "Спикер 3", vec(0.6, 1, 0), seconds=1.5)]
    assert diarize.cluster(groups, 0.9) == [0, 1, 1]


def test_voice_labels_follow_the_clusters_and_are_numbered_by_first_appearance():
    segments = [seg("Спикер 2", 0, 20), seg("Спикер 1", 20, 50), seg("Спикер 1", 61, 90), seg("Спикер 2", 90, 110),
                seg("Спикер 2", 115, 117, "да"), seg("Спикер 3", 130, 131, "угу")]
    groups = [group(0, "Спикер 2", ANNA), group(0, "Спикер 1", BORIS),
              group(1, "Спикер 1", vec(1, 0.05, 0)), group(1, "Спикер 2", vec(0.05, 1, 0))]
    labels = diarize.voice_labels(segments, groups, 0.5, total=131)
    # minute 1 swapped the labels of the part; «угу» of a label never long enough for a print stays a voice of its own
    assert labels == ["Спикер 1", "Спикер 2", "Спикер 1", "Спикер 2", "Спикер 2", "Спикер 3"]


def test_voice_groups_send_every_minute_with_its_speech_and_keep_named_voices(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    cuts, calls = [], []

    def cut(source, folder, start, seconds, name):
        cuts.append((round(start, 2), round(seconds, 2)))
        target = pathlib.Path(folder) / name
        target.write_bytes(b"part")
        return target

    def embed(path, segments, min_seconds=None):
        calls.append((segments, min_seconds))
        return {"speakers": {s["speaker"]: {"embedding": ANNA if s["speaker"] == "Спикер 1" else BORIS,
                                            "duration_seconds": s["end"] - s["start"]} for s in segments}}
    monkeypatch.setattr(diarize, "_cut", cut)
    monkeypatch.setattr(speaker_id, "embed_speakers", embed)
    segments = [seg("Спикер 1", 0, 25), seg("Спикер 2", 25, 50), seg("Ольга Кузнецова", 50, 58, speaker_source="voiceprint"),
                seg("Спикер 1", 70, 100), seg("Спикер 2", 100, 100.8, "да"), seg("Спикер 1", 110, 110.3, "а")]
    groups = diarize.voice_groups(record, segments, 111)
    assert len(calls) == 2 and sorted(cuts)[0][0] == 0.4
    assert all(s["speaker"] != "Ольга Кузнецова" for call, _ in calls for s in call)
    # a label is compared by a second of speech or more; the short «да» goes on its own, «а» is too short to hear
    first = next(call for call in calls if call[1] is None)
    second = next(call for call in calls if call[1] is not None)
    assert all(s["start"] >= 0 and s["end"] - s["start"] >= 1.0 for s in first[0])
    assert [s["speaker"] for s in second[0]] == ["Спикер 1", "#4"] and second[1] == diarize.SHORT_MIN_SECONDS
    assert [(g["window"], g["label"], g.get("short")) for g in groups] == [
        (0, "Спикер 1", None), (0, "Спикер 2", None), (1, "Спикер 1", None), (1, "#4", 4)]


def test_voice_groups_print_every_part_on_its_own(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    calls, said = [], []
    monkeypatch.setattr(diarize, "_cut", fake_cut)

    def embed(path, segments, min_seconds=None):
        calls.append(sorted(s["speaker"] for s in segments))
        return {"speakers": {s["speaker"]: {"embedding": ANNA, "duration_seconds": 10.0} for s in segments}}
    monkeypatch.setattr(speaker_id, "embed_speakers", embed)
    segments = [seg("Спикер 1", 0, 20, part=0), seg("Спикер 2", 20, 38, part=0),
                seg("Спикер 1", 39, 55, part=1), seg("Спикер 2", 55, 75, part=1)]  # one minute, two parts
    groups = diarize.voice_groups(record, segments, 75, progress=said.append)
    assert calls == [["Спикер 1", "Спикер 2"], ["Спикер 1", "Спикер 2"]]
    assert [(g["window"], g["label"]) for g in groups] == [(0, "Спикер 1"), (0, "Спикер 2"), (1, "Спикер 1"), (1, "Спикер 2")]
    assert said == ["Голоса по всей записи: 2 из 2 кусков"]


def test_voice_groups_give_up_when_the_service_loses_many_minutes(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    monkeypatch.setattr(diarize, "_cut", fake_cut)

    def embed(path, segments, min_seconds=None):
        raise speaker_id.SpeakerIdentityError("Не удалось связаться с сервисом голосов")
    monkeypatch.setattr(speaker_id, "embed_speakers", embed)
    with pytest.raises(diarize.DiarizationError, match="не ответил"):
        diarize.voice_groups(record, [seg("Спикер 1", 0, 30), seg("Спикер 2", 70, 100)], 100)


def test_voice_groups_stop_at_an_outdated_voice_service(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    monkeypatch.setattr(diarize, "_cut", fake_cut)

    def embed(path, segments, min_seconds=None):
        raise speaker_id.OutdatedServiceError("Сервис голосов на Vercel — старая версия (x). Передеплойте speaker-service.")
    monkeypatch.setattr(speaker_id, "embed_speakers", embed)
    with pytest.raises(diarize.DiarizationError, match="старая версия"):
        diarize.voice_groups(record, [seg("Спикер 1", 60 * k, 60 * k + 30) for k in range(12)], 720)


def test_gemini_parts_are_joined_by_the_replicas_both_heard(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    monkeypatch.setattr(diarize, "GEMINI_WINDOW", 60.0)
    monkeypatch.setattr(diarize, "GEMINI_OVERLAP", 30.0)
    monkeypatch.setattr(diarize, "_cut", fake_cut)
    segments = [seg("Спикер 1", 0, 20), seg("Спикер 2", 20, 40), seg("Спикер 1", 62, 75), seg("Спикер 2", 75, 88),
                seg("Спикер 1", 95, 110), seg("Спикер 3", 125, 140)]
    answers = iter([
        {"speakers": [{"i": 0, "speaker": "Спикер 1"}, {"i": 1, "speaker": "Спикер 2"}, {"i": 2, "speaker": "Спикер 1"},
                      {"i": 3, "speaker": "Спикер 2"}]},
        # the second part (60–150 s) numbers by its own first voice: its «Спикер 1» is the first part's «Спикер 2»
        {"speakers": [{"i": 2, "speaker": "Спикер 2"}, {"i": 3, "speaker": "Спикер 1"}, {"i": 4, "speaker": "Спикер 1"},
                      {"i": 5, "speaker": "Спикер 3"}]},
        {"speakers": [{"i": 5, "speaker": "Спикер 1"}]},
    ])
    prompts = []

    def chat(model, messages, **kwargs):
        prompts.append(messages[0]["content"][0]["text"])
        return json.dumps(next(answers))
    monkeypatch.setattr(llm, "chat", chat)
    labels = diarize.gemini_labels(record, segments, 140, {"diarization_model": "google/gemini-2.5-flash"}, people=3)
    assert labels == ["Спикер 1", "Спикер 2", "Спикер 1", "Спикер 2", "Спикер 2", "Спикер 3"]
    assert len(prompts) == 3 and "3 чел." in prompts[0]


def test_gemini_must_answer_for_nearly_every_replica(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    monkeypatch.setattr(diarize, "_cut", fake_cut)
    monkeypatch.setattr(llm, "chat", lambda *a, **k: json.dumps({"speakers": [{"i": 0, "speaker": "Спикер 1"}]}))
    with pytest.raises(diarize.DiarizationError, match="только у 1 из 3"):
        diarize.gemini_labels(record, [seg("Спикер 1", 0, 5), seg("Спикер 2", 5, 9), seg("Спикер 1", 9, 14)], 14, {})


def test_relabel_keeps_the_part_labels_when_a_way_cannot_run(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    segments = [seg("Спикер 1", 0, 30), seg("Спикер 2", 30, 60)]
    monkeypatch.setattr(config, "voice_service_enabled", lambda: False)
    same, info = diarize.relabel(record, segments, 60, {"diarization_method": "voice"})
    assert same is segments and info["method"] == "chunks" and info["wanted"] == "voice"
    assert "SYSAI_VOICE_SERVICE_URL" in info["error"]
    monkeypatch.setattr(config, "voice_service_enabled", lambda: True)

    def broken(*args, **kwargs):
        raise diarize.DiarizationError("Сервис голосов не вернул ни одного отпечатка голоса.")
    monkeypatch.setattr(diarize, "voice_groups", broken)
    same, info = diarize.relabel(record, segments, 60, {"diarization_method": "voice"})
    assert same is segments and info["error"].startswith("Сервис голосов не вернул")
    monkeypatch.setattr(diarize, "voice_groups", lambda *a, **k: 1 / 0)
    same, info = diarize.relabel(record, segments, 60, {"diarization_method": "voice"})
    assert same is segments and info["error"] == "Сбой разметки голосов: ZeroDivisionError"
    same, info = diarize.relabel(record, segments, 60, {"diarization_method": "chunks"})
    assert same is segments and info == {"method": "chunks", "wanted": "chunks", "error": ""}


def test_relabel_gives_new_labels_and_leaves_named_voices(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    monkeypatch.setattr(config, "voice_service_enabled", lambda: True)
    segments = [seg("Спикер 1", 0, 30, speaker_auto={"speaker": "x"}), seg("Ольга Кузнецова", 30, 40, speaker_source="voiceprint",
                speaker_employee_id=4), seg("Спикер 3", 61, 90)]
    monkeypatch.setattr(diarize, "voice_groups", lambda *a, **k: [group(0, "Спикер 1", ANNA), group(1, "Спикер 3", ANNA)])
    result, info = diarize.relabel(record, segments, 90, {"diarization_method": "voice", "diarization_voice_threshold": 0.5})
    assert [s["speaker"] for s in result] == ["Спикер 1", "Ольга Кузнецова", "Спикер 1"]
    assert result[1]["speaker_employee_id"] == 4 and info["method"] == "voice" and info["voices"] == 2


def test_settings_of_the_way_are_checked():
    assert diarize.method_of({"diarization_method": "pyannote"}) == "chunks"
    assert diarize.method_of({"diarization_method": "gemini"}) == "gemini"
    assert diarize.threshold_of({"diarization_voice_threshold": "abc"}) == 0.5
    assert diarize.threshold_of({"diarization_voice_threshold": 3}) == 0.95
    assert diarize.THRESHOLDS[0] == 0.3 and diarize.THRESHOLDS[-1] == 0.8
