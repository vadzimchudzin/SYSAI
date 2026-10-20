import datetime as dt

from app import analyze, config, speaker_id
from app.db import Employee


def test_voiceprint_encryption_round_trips(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "a-long-stable-test-secret")
    encrypted = speaker_id.encrypt_voiceprint("opaque-template")
    assert b"opaque-template" not in encrypted
    assert speaker_id.decrypt_voiceprint(encrypted) == "opaque-template"


def test_voiceprint_cannot_be_decrypted_after_secret_rotation(monkeypatch):
    monkeypatch.setattr(config, "SECRET_KEY", "secret-before-rotation")
    encrypted = speaker_id.encrypt_voiceprint("opaque-template")
    monkeypatch.setattr(config, "SECRET_KEY", "secret-after-rotation")
    try:
        speaker_id.decrypt_voiceprint(encrypted)
    except speaker_id.SpeakerIdentityError as exc:
        assert "SECRET_KEY" in str(exc)
    else:
        raise AssertionError("Expected decryption failure after secret rotation")


def test_identify_segments_renames_only_provider_confirmed_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PYANNOTEAI_API_KEY", "fake")
    monkeypatch.setattr(config, "_panel", lambda _key: "")
    profiles = [{
        "label": "employee-7", "employee_id": 7,
        "employee_name": "Мария Смирнова", "voiceprint": "opaque-template",
    }]
    output = {
        "identification": [
            {"start": 1.0, "end": 3.0, "speaker": "Мария Смирнова",
             "diarizationSpeaker": "SPEAKER_00", "match": "employee-7"},
            {"start": 4.0, "end": 6.0, "speaker": "SPEAKER_01",
             "diarizationSpeaker": "SPEAKER_01", "match": None},
        ],
        "voiceprints": [
            {"speaker": "SPEAKER_00", "match": "employee-7",
             "confidence": {"employee-7": 82, "employee-8": 20}},
            {"speaker": "SPEAKER_01", "match": None,
             "confidence": {"employee-7": 10}},
        ],
    }
    monkeypatch.setattr(speaker_id, "_identify_audio", lambda _path, _profiles: output)
    segments = [
        {"start": 12.0, "speaker": "Спикер 1", "text": "Доброе утро."},
        {"start": 14.5, "speaker": "Спикер 2", "text": "Проверка связи."},
    ]
    result = speaker_id.identify_segments(tmp_path / "chunk.wav", segments, profiles, offset=10.0)
    assert result[0]["speaker"] == "Мария Смирнова"
    assert result[0]["speaker_employee_id"] == 7
    assert result[0]["speaker_source"] == "voiceprint"
    assert result[0]["speaker_confidence"] == 82.0
    assert result[1]["speaker"] == "Спикер 2"
    assert "speaker_employee_id" not in result[1]


def test_unknown_or_low_confidence_voice_stays_anonymous(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PYANNOTEAI_API_KEY", "fake")
    monkeypatch.setattr(config, "_panel", lambda _key: "")
    profiles = [{"label": "employee-7", "employee_id": 7, "employee_name": "Мария", "voiceprint": "one"}]
    output = {
        "identification": [{"start": 0, "end": 5, "match": "employee-7"}],
        "voiceprints": [{"speaker": "SPEAKER_00", "match": "employee-7",
                         "confidence": {"employee-7": 48}}],
    }
    monkeypatch.setattr(speaker_id, "_identify_audio", lambda _path, _profiles: output)
    segments = [{"start": 1, "speaker": "Спикер 1", "text": "Фраза."}]
    result = speaker_id.identify_segments(tmp_path / "chunk.wav", segments, profiles)
    assert result[0]["speaker"] == "Спикер 1"
    assert "speaker_employee_id" not in result[0]


def test_ambiguous_voice_matches_do_not_assign_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PYANNOTEAI_API_KEY", "fake")
    monkeypatch.setattr(config, "_panel", lambda _key: "")
    profiles = [
        {"label": "employee-7", "employee_id": 7, "employee_name": "Мария", "voiceprint": "one"},
        {"label": "employee-8", "employee_id": 8, "employee_name": "Анна", "voiceprint": "two"},
    ]
    output = {
        "identification": [
            {"start": 0, "end": 4, "match": "employee-7"},
            {"start": 0, "end": 4, "match": "employee-8"},
        ],
        "voiceprints": [
            {"speaker": "SPEAKER_00", "match": "employee-7", "confidence": {"employee-7": 82}},
            {"speaker": "SPEAKER_01", "match": "employee-8", "confidence": {"employee-8": 80}},
        ],
    }
    monkeypatch.setattr(speaker_id, "_identify_audio", lambda _path, _profiles: output)
    segments = [{"start": 1, "speaker": "Спикер 1", "text": "Фраза."}]
    result = speaker_id.identify_segments(tmp_path / "chunk.wav", segments, profiles)
    assert result[0]["speaker"] == "Спикер 1"
    assert "speaker_employee_id" not in result[0]


def test_report_normalization_prefers_confirmed_voiceprint_identity():
    employee = Employee(id=7, name="Мария Смирнова", aliases="Мария")
    report = {
        "participants": [{"speaker": "Мария Смирнова", "name": "Мария", "employee_id": None, "role": "участник"}],
        "topics": [], "decisions": [], "tasks": [], "open_questions": [], "risks": [], "notes": [], "visualizations": [],
    }
    normalized = analyze.normalize(
        report, [employee], dt.date(2026, 10, 20),
        source_segments=[{"speaker": "Мария Смирнова", "speaker_employee_id": 7, "speaker_source": "voiceprint"}],
    )
    assert normalized["participants"][0]["employee_id"] == 7
    assert normalized["participants"][0]["name"] == "Мария Смирнова"


def test_profile_fingerprint_changes_when_sample_is_replaced():
    one = [{"label": "employee-7", "employee_id": 7, "voiceprint": "sample-one"}]
    two = [{"label": "employee-7", "employee_id": 7, "voiceprint": "sample-two"}]
    assert speaker_id.profiles_fingerprint(one) != speaker_id.profiles_fingerprint(two)


def test_voice_identity_stays_disabled_without_provider_key(monkeypatch):
    monkeypatch.setattr(config, "PYANNOTEAI_API_KEY", "")
    monkeypatch.setattr(config, "_panel", lambda _key: "")
    assert speaker_id.enrolled_voiceprints() == []
