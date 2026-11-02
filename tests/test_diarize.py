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
    alike = [group(0, "Спикер 1", ANNA), group(0, "Спикер 2", vec(1, 0.9, 0))]  # similarity ≈ 0.74 > threshold
    assert diarize.cluster(alike, 0.5) == [0, 1]
    same = [group(0, "Спикер 1", ANNA), group(0, "Спикер 2", vec(1, 0.3, 0))]  # ≈ 0.96: the part split one voice
    assert diarize.cluster(same, 0.5) == [0, 0]


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

    def embed(path, segments):
        calls.append(segments)
        return {"speakers": {s["speaker"]: {"embedding": ANNA if s["speaker"] == "Спикер 1" else BORIS,
                                            "duration_seconds": s["end"] - s["start"]} for s in segments}}
    monkeypatch.setattr(diarize, "_cut", cut)
    monkeypatch.setattr(speaker_id, "embed_speakers", embed)
    segments = [seg("Спикер 1", 0, 25), seg("Спикер 2", 25, 50), seg("Ольга Кузнецова", 50, 58, speaker_source="voiceprint"),
                seg("Спикер 1", 70, 100), seg("Спикер 2", 100, 100.8, "да")]
    groups = diarize.voice_groups(record, segments, 101)  # «да» is shorter than a second: nothing to compare in minute 1
    assert len(calls) == 2 and sorted(cuts)[0][0] == 0.4
    assert all(s["speaker"] != "Ольга Кузнецова" for call in calls for s in call)
    assert all(s["start"] >= 0 and s["end"] - s["start"] >= 1.0 for call in calls for s in call)
    assert [(g["window"], g["label"]) for g in groups] == [(0, "Спикер 1"), (0, "Спикер 2"), (1, "Спикер 1")]


def test_voice_groups_give_up_when_the_service_loses_many_minutes(monkeypatch, tmp_path):
    record = tmp_path / "meeting.mp3"
    record.write_bytes(b"audio")
    monkeypatch.setattr(diarize, "_cut", fake_cut)

    def embed(path, segments):
        raise speaker_id.SpeakerIdentityError("Не удалось связаться с сервисом голосов")
    monkeypatch.setattr(speaker_id, "embed_speakers", embed)
    with pytest.raises(diarize.DiarizationError, match="не ответил"):
        diarize.voice_groups(record, [seg("Спикер 1", 0, 30), seg("Спикер 2", 70, 100)], 100)


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
