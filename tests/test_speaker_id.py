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


def test_pyannote_is_gone(client):
    """pyannoteAI is removed: no key in the settings, no enrollment through it."""
    from app import settings_store
    assert not hasattr(config, "pyannoteai_key") and "pyannoteai_api_key" not in settings_store.DEFAULTS
    assert not hasattr(speaker_id, "create_voiceprint") and not hasattr(speaker_id, "identify_segments")
    response = client.post("/employees/1/voice-sample", data={"consent": "yes"}, files={"sample": ("a.wav", b"RIFF", "audio/wav")})
    assert response.status_code in (404, 405)
    assert "pyannote" not in client.get("/settings").text.lower()
