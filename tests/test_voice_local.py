"""Speaker labelling from the sound itself (voice_local.py) and its labels on the transcript (diarize.turn_labels)."""
import numpy as np
import pytest

from app import diarize, settings_store, voice_local, voice_memory


def voices(count, dim=192, seed=7):
    rng = np.random.default_rng(seed)
    base = rng.normal(size=(count, dim)).astype(np.float32)
    return base / np.linalg.norm(base, axis=1, keepdims=True)


def heard_print(window, local, voice, noise, rng, alone=6.0):
    vector = voice + noise * rng.normal(size=voice.shape).astype(np.float32) / np.sqrt(voice.size)
    return {"window": window, "local": local, "vector": (vector / np.linalg.norm(vector)).astype(np.float32),
            "alone": alone, "seconds": alone}


def seg(speaker, start, end, text="обсуждаем план работ на эту неделю подробно", **extra):
    return dict(speaker=speaker, start=start, end=end, text=text, **extra)


def test_models_are_shipped_with_the_app():
    assert voice_local.available()
    assert diarize.unavailable("sound") == ""
    assert diarize.DEFAULT_METHOD == "sound" and next(iter(diarize.METHODS)) == "sound"


def test_features_are_kaldi_shaped():
    samples = np.random.default_rng(1).normal(size=16000).astype(np.float32) * 0.1
    out = voice_local.features(samples)
    assert out.shape == (98, 80) and np.isfinite(out).all()
    assert abs(float(out.mean())) < 1e-3   # the mean of the recording is removed
    with pytest.raises(voice_local.LocalVoiceError):
        voice_local.features(samples[:100])


def test_the_networks_run_and_silence_has_no_voices():
    silence = np.zeros((1, voice_local.WINDOW), dtype=np.float32)
    activity = voice_local._activity(silence)
    assert activity.shape[0] == 1 and activity.shape[2] == 3 and int(activity.sum()) == 0
    noise = np.random.default_rng(2).normal(size=3 * 16000).astype(np.float32) * 0.1
    vector = voice_local.embed(noise)
    assert vector.shape == (192,) and abs(float(np.linalg.norm(vector)) - 1.0) < 1e-4
    assert voice_local.embed(noise[:1000]) is None   # 60 ms is no voice
    voice_local.release()


def test_clustering_finds_the_people_of_a_meeting():
    rng = np.random.default_rng(3)
    anna, boris, vera = voices(3)
    prints = []
    for window in range(40):
        prints.append(heard_print(window, 0, (anna, boris, vera)[window % 3], 0.9, rng))
        if window % 4 == 0:
            prints.append(heard_print(window, 1, (anna, boris, vera)[(window + 1) % 3], 0.9, rng))
    person, centres = voice_local.cluster(prints)
    assert len(centres) == 3
    by_voice = {}
    for item, who in zip(prints, person):
        truth = int(np.argmax([float(item["vector"] @ v) for v in (anna, boris, vera)]))
        by_voice.setdefault(truth, set()).add(who)
    assert all(len(found) == 1 for found in by_voice.values())
    assert len({next(iter(found)) for found in by_voice.values()}) == 3


def test_two_voices_of_one_window_are_never_one_person():
    rng = np.random.default_rng(4)
    anna, boris = voices(2)
    prints = [heard_print(w, 0, anna if w % 2 else boris, 0.6, rng) for w in range(20)]
    # a one-word reply: its weak print looks like Anna's, but Anna already speaks in this window
    prints.append(heard_print(5, 1, anna, 1.2, rng, alone=0.3))
    person, centres = voice_local.cluster(prints)
    assert len(centres) == 2
    assert person[-1] != person[5]


def test_a_voice_heard_for_a_moment_joins_a_known_one_and_the_count_of_people_helps():
    rng = np.random.default_rng(5)
    anna, boris = voices(2)
    far = anna * 0.75 + voices(1, seed=11)[0] * 0.66   # Anna from the far end of the table
    prints = [heard_print(w, 0, anna, 0.5, rng) for w in range(0, 20)]
    prints += [heard_print(w, 0, boris, 0.5, rng) for w in range(20, 40)]
    prints += [heard_print(w, 0, far, 0.5, rng) for w in range(40, 50)]
    _, free = voice_local.cluster(prints, threshold=0.8)
    _, told = voice_local.cluster(prints, threshold=0.8, people=2)
    assert len(free) == 3 and len(told) == 2


def test_timeline_gives_every_frame_its_people_and_turns_smooth_the_holes():
    frames = 589
    first = np.zeros((frames, 3), dtype=np.uint8)
    first[:300, 0] = 1            # one voice for the first five seconds…
    first[290:500, 1] = 1         # …another comes in over its last words
    heard = {"step": 5 * voice_local.RATE, "windows": [first],
             "prints": [{"window": 0, "local": 0}, {"window": 0, "local": 1}]}
    activity, frame = voice_local.timeline(heard, [0, 1], 2)
    assert activity.shape == (2, frames)
    assert activity[0, 100] == 1 and activity[1, 100] == 0
    assert activity[0, 295] == 1 and activity[1, 295] == 1   # cross-talk: both
    assert activity[:, 550].sum() == 0
    found = voice_local.turns(activity, frame)
    assert [who for _, _, who in found] == [0, 1]
    assert found[0][0] == 0.0 and abs(found[0][1] - 300 * frame) < 0.02
    holes = np.zeros((1, 400), dtype=np.uint8)
    holes[0, :100] = holes[0, 105:200] = 1   # a hole of 80 ms is not a pause
    holes[0, 300:303] = 1                    # a flash of 50 ms is not speech
    assert len(voice_local.turns(holes, frame)) == 1


HEARD = {"turns": [[0.0, 10.0, 0], [10.2, 20.0, 1], [14.0, 14.6, 0], [20.5, 30.0, 0], [30.2, 36.0, 2]],
         "centres": [[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]], "seconds": [20.1, 9.8, 5.8], "model": "m"}


def test_replicas_take_the_person_who_speaks_over_them():
    segments = [seg("Спикер 1", 0.2, 9.8), seg("Спикер 1", 10.3, 13.9), seg("Спикер 1", 14.0, 14.6, "да"),
                seg("Спикер 1", 14.7, 19.9), seg("Спикер 2", 20.6, 29.0), seg("Спикер 2", 30.3, 35.0)]
    found = {}
    labels = diarize.turn_labels(segments, HEARD, 36.0, found=found)
    # the labels of the recognition engine do not matter; «да» said over Boris's phrase is Anna's
    assert labels == ["Спикер 1", "Спикер 2", "Спикер 1", "Спикер 2", "Спикер 1", "Спикер 3"]
    assert found["centres"]["Спикер 3"] == [0.6, 0.8] and found["seconds"]["Спикер 2"] == 9.8 and found["model"] == "m"


def test_a_replica_two_people_share_is_cut_where_the_voice_changes():
    text = "первый говорит долго и подробно про план а второй отвечает ему тоже долго и подробно"
    segments = [seg("Спикер 1", 5.0, 15.0, text)]
    details = {}
    labels = diarize.turn_labels(segments, HEARD, 36.0, details)
    assert labels == ["Спикер 1"] and list(details["cuts"]) == [0]
    out = diarize.apply(segments, labels, details)
    assert [item["speaker"] for item in out] == ["Спикер 1", "Спикер 2"]
    assert out[0]["text"].startswith("первый") and out[1]["text"].endswith("подробно")
    assert 9.0 < out[1]["start"] < 11.5
    assert diarize.turn_labels(segments, HEARD, 36.0, {}, cut=False) == ["Спикер 1"]


def test_a_word_of_another_voice_does_not_cut_a_phrase_and_named_voices_stay():
    segments = [seg("Спикер 1", 10.3, 19.9, "второй говорит долго и его коротко перебивают но он продолжает говорить"),
                seg("Анна", 21.0, 25.0, speaker_source="voiceprint"), seg("Спикер 9", 40.0, 41.0, "итог")]
    details = {}
    labels = diarize.turn_labels(segments, HEARD, 41.0, details)
    assert details["cuts"] == {}
    assert labels[0] == "Спикер 1" and labels[1] is None
    assert labels[2] is not None   # nobody was heard over it: the voice heard closest


def test_no_speech_keeps_the_part_labels(monkeypatch, tmp_path):
    with pytest.raises(diarize.DiarizationError):
        diarize.turn_labels([seg("Спикер 1", 0, 5)], {"turns": []}, 5.0)
    recording = tmp_path / "meeting.mp3"
    recording.write_bytes(b"audio")

    def broken(path, **kwargs):
        raise voice_local.LocalVoiceError("В записи не слышно речи — размечать голоса не по чему.")
    monkeypatch.setattr(voice_local, "diarize", broken)
    segments = [seg("Спикер 1", 0, 5)]
    out, info = diarize.relabel(recording, segments, 5.0, {"diarization_method": "sound"})
    assert out == segments and info["method"] == "chunks" and info["wanted"] == "sound" and "речи" in info["error"]


def test_the_sound_way_labels_the_meeting_and_tells_what_it_did(monkeypatch, tmp_path):
    recording = tmp_path / "meeting.mp3"
    recording.write_bytes(b"audio")
    monkeypatch.setattr(voice_local, "diarize", lambda path, **kwargs: HEARD)
    segments = [seg("Спикер 5", 0.2, 9.8), seg("Спикер 5", 10.3, 19.9), seg("Спикер 5", 30.3, 35.0)]
    out, info = diarize.relabel(recording, segments, 36.0, {})   # the default way
    assert [item["speaker"] for item in out] == ["Спикер 1", "Спикер 2", "Спикер 3"]
    assert info["method"] == "sound" and info["voices"] == 3 and not info["error"]


def test_a_server_on_part_labels_moves_to_the_sound_engine_once():
    assert settings_store.get("diarization_method") == "sound"
    settings_store.set_many({"diarization_method": "chunks"})
    assert settings_store.adopt_sound_engine() is True
    assert settings_store.get("diarization_method") == "sound"
    settings_store.set_many({"diarization_method": "chunks"})   # chosen on purpose after the move: kept
    assert settings_store.adopt_sound_engine() is False
    assert settings_store.get("diarization_method") == "chunks"


def test_a_way_chosen_on_purpose_is_not_replaced():
    settings_store.set_many({"diarization_method": "gemini"})
    assert settings_store.adopt_sound_engine() is False
    assert settings_store.get("diarization_method") == "gemini"


def test_voices_are_remembered_without_the_voice_service(monkeypatch):
    settings_store.set_many({"voice_memory": True})
    assert voice_memory.engine() and voice_memory.enabled()
    assert diarize.local_prints({"diarization_method": "sound"})
    seen = {}

    def fake(path, intervals, limit=120.0):
        seen.update(intervals)
        return {label: ([1.0, 0.0], 12.0) for label in intervals}
    monkeypatch.setattr(voice_local, "embed_labels", fake)
    groups = diarize.local_groups("meeting.mp3", [seg("employee-7", 0.0, 8.0), seg("employee-7", 9.0, 9.4, "да"),
                                                  seg("employee-9", 10.0, 16.0)], 16.0)
    assert sorted(seen) == ["employee-7", "employee-9"] and len(seen["employee-7"]) == 1
    assert [(g["label"], g["model"]) for g in groups] == [("employee-7", voice_local.MODEL_VERSION),
                                                         ("employee-9", voice_local.MODEL_VERSION)]
