import datetime as dt
from urllib.parse import parse_qs, urlsplit

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


def test_voice_sample_enrollment_requires_explicit_consent(client, monkeypatch):
    from app.db import Employee, EmployeeVoiceprint, SessionLocal

    monkeypatch.setattr(config, "pyannoteai_key", lambda: "fake-api-key")
    with SessionLocal() as session:
        employee = Employee(name="Голос Без Согласия")
        session.add(employee)
        session.commit()
        employee_id = employee.id

    response = client.post(
        f"/employees/{employee_id}/voice-sample",
        files={"sample": ("sample.wav", b"fake-audio", "audio/wav")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    message = parse_qs(urlsplit(response.headers["location"]).query).get("voice_msg", [""])[0].lower()
    assert "соглас" in message
    with SessionLocal() as session:
        assert session.query(EmployeeVoiceprint).filter_by(employee_id=employee_id).count() == 0


def test_voice_sample_enrollment_saves_encrypted_template_and_consent(client, monkeypatch):
    from app.db import Employee, EmployeeVoiceprint, SessionLocal

    monkeypatch.setattr(config, "pyannoteai_key", lambda: "fake-api-key")
    monkeypatch.setattr(speaker_id, "normalize_sample", lambda _source, _target: 12.5)
    monkeypatch.setattr(speaker_id, "create_voiceprint", lambda _path, _employee_id: "provider-template-secret")

    with SessionLocal() as session:
        employee = Employee(name="Голос С Согласием")
        session.add(employee)
        session.commit()
        employee_id = employee.id

    response = client.post(
        f"/employees/{employee_id}/voice-sample",
        data={"consent": "yes"},
        files={"sample": ("sample.wav", b"fake-audio", "audio/wav")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    message = parse_qs(urlsplit(response.headers["location"]).query).get("voice_msg", [""])[0].lower()
    assert "создан" in message
    with SessionLocal() as session:
        profile = session.query(EmployeeVoiceprint).filter_by(employee_id=employee_id).one()
        assert b"provider-template-secret" not in profile.encrypted_voiceprint
        assert speaker_id.decrypt_voiceprint(profile.encrypted_voiceprint) == "provider-template-secret"
        assert profile.consent_at is not None
        assert profile.consent_version == speaker_id.CONSENT_VERSION
        assert profile.sample_duration == 12.5


def test_voice_sample_can_be_deleted_without_deleting_employee(client):
    from app.db import Employee, EmployeeVoiceprint, SessionLocal

    with SessionLocal() as session:
        employee = Employee(name="Голос Для Удаления")
        session.add(employee)
        session.flush()
        employee_id = employee.id
        session.add(EmployeeVoiceprint(
            employee_id=employee_id,
            encrypted_voiceprint=speaker_id.encrypt_voiceprint("delete-me"),
            sample_duration=9.0,
            consent_at=dt.datetime.now(dt.timezone.utc),
        ))
        session.commit()

    response = client.post(f"/employees/{employee_id}/voice-sample/delete", follow_redirects=False)
    assert response.status_code == 303
    with SessionLocal() as session:
        assert session.get(Employee, employee_id) is not None
        assert session.query(EmployeeVoiceprint).filter_by(employee_id=employee_id).count() == 0



def test_analysis_transcript_marks_confirmed_voiceprint_identity():
    text = analyze.transcript_text([
        {"start": 12.0, "speaker": "Мария Смирнова", "text": "Доброе утро.",
         "speaker_employee_id": 7, "speaker_source": "voiceprint", "speaker_confidence": 82.0},
        {"start": 15.0, "speaker": "Спикер 1", "text": "Продолжаем."},
    ])
    assert "[VOICEPRINT_CONFIRMED employee_id=7] Мария Смирнова: Доброе утро." in text
    assert "[VOICEPRINT_CONFIRMED" not in text.splitlines()[1]



def test_single_long_transcript_segment_is_not_named_from_a_short_voice_match(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PYANNOTEAI_API_KEY", "fake")
    monkeypatch.setattr(config, "_panel", lambda _key: "")
    profiles = [{"label": "employee-7", "employee_id": 7, "employee_name": "Мария", "voiceprint": "one"}]
    output = {
        "identification": [{"start": 0, "end": 14, "match": "employee-7"}],
        "voiceprints": [{"speaker": "SPEAKER_00", "match": "employee-7",
                         "confidence": {"employee-7": 91}}],
    }
    monkeypatch.setattr(speaker_id, "_identify_audio", lambda _path, _profiles: output)
    segments = [{"start": 0, "speaker": "Спикер 1", "text": "Длинная неразмеченная расшифровка"}]
    result = speaker_id.identify_segments(tmp_path / "chunk.wav", segments, profiles, duration=60.0)
    assert result[0]["speaker"] == "Спикер 1"
    assert "speaker_employee_id" not in result[0]
