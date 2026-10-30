"""Real WAV/ffmpeg routes and encrypted DB persistence. Only external inference is stubbed."""
import array
import datetime as dt
import hashlib
import io
import json
import math
import wave
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from app import accounts, config, db, main, settings_store, speaker_id
from app.db import Employee, EmployeeVoiceSample, EmployeeVoiceprint, Meeting, Recording, SessionLocal, User, now


def make_wav(path, seconds=10, frequency=230, amplitude=6000):
    values = array.array('h', (int(amplitude * math.sin(2 * math.pi * frequency * i / 16000)) for i in range(seconds * 16000)))
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(values.tobytes())
    return path


def turn(start=0, end=8, speaker='Спикер 1'):
    return dict(start=start, end=end, speaker=speaker, text='Проверяем разговор на рабочей встрече')


def seed(tmp_path, silent=False, transcript=None):
    path = make_wav(tmp_path / 'meeting.wav', seconds=120, amplitude=0 if silent else 6000)
    with SessionLocal() as s:
        e = Employee(name='Анна'); m = Meeting(title='TicNote', source='ticnote', status='done', audio_path=str(path), transcript=transcript or [turn()])
        s.add_all([e, m]); s.commit()
        return e.id, m.id, path


def post(client, eid, mid, **overrides):
    data = dict(speaker='Спикер 1', start=0, end=8, employee_id=eid, consent='yes', listened='yes')
    data.update(overrides)
    return client.post(f'/meetings/{mid}/speaker-clips/confirm', data=data, follow_redirects=False)


def samples():
    with SessionLocal() as s:
        return s.query(EmployeeVoiceSample).all()


def embedding_result(*args):
    return dict(embedding=[1.0]+[0.0]*191, model_version='real-model-v1', waveform=[], sample_blob_path=None, duration_seconds=8)


def valid_sample(eid=1, source='source', content='content', vector=None, model='real-model-v1'):
    return SimpleNamespace(employee_id=eid, id=1, source_kind='meeting_confirmed',
        source_meeting_id=1, source_start=0, source_end=8, source_audio_sha256=source,
        content_sha256=content, embedding_status='ready', encrypted_audio=b'sealed',
        encrypted_embedding=speaker_id.encrypt_voiceprint(json.dumps(vector or ([1.0]+[0.0]*191))),
        embedding_dimensions=192, model_version=model, confirmed_by='1', consent_at=now(),
        consent_version=speaker_id.CONFIRMED_CONSENT_VERSION, sample_duration=8,
        quality_metrics={'rms': 3000, 'active_ratio': 1, 'clipped_ratio': 0},
        expires_at=now()+dt.timedelta(days=180))


def test_candidates_reject_malformed_negative_nonfinite_and_overlap():
    for bad in ['bad', -1, float('nan'), float('inf'), True]:
        assert speaker_id.propose_clear_speaker_clips([turn(bad, 8)]) == []
    assert speaker_id.propose_clear_speaker_clips([dict(start=0, speaker='A', text='missing end')]) == []
    clips = speaker_id.propose_clear_speaker_clips([turn(0,20,'A'),turn(10,18,'B'),turn(25,33,'C')])
    assert [c['speaker'] for c in clips] == ['C']
    clips = speaker_id.propose_clear_speaker_clips([turn(0,8,'A'),turn(1,7,''),turn(10,18,'B')])
    assert [c['speaker'] for c in clips] == ['B']


def test_debug_off_no_prompts_manual_entry_available(client, tmp_path):
    eid, mid, _ = seed(tmp_path)
    html = client.get(f'/meetings/{mid}').text
    assert 'id="voice-sample-clips"' not in html
    assert 'Выбрать образец голоса вручную' in html
    html = client.get(f'/meetings/{mid}?voice_enroll=1').text
    assert 'id="voice-sample-clips"' in html and 'name="listened"' in html
    settings_store.set_many({'voice_debug_mode': True})
    assert 'id="voice-sample-clips"' in client.get(f'/meetings/{mid}').text


def test_debug_stops_known_ready_but_unknown_remains(client, tmp_path, monkeypatch):
    eid, mid, path = seed(tmp_path)
    settings_store.set_many({'voice_debug_mode': True})
    monkeypatch.setattr(speaker_id, 'identification_readiness', lambda _: dict(ready=True, people=[dict(employee_id=eid, ready=True)]))
    with SessionLocal() as s:
        m = s.get(Meeting, mid)
        assert main._meeting_voice_enrollment_context(s,m)[0]  # unknown is never silently hidden
        m.transcript = [dict(turn(), speaker_source='voiceprint', speaker_employee_id=eid)]
        assert main._meeting_voice_enrollment_context(s,m)[0] == []
        assert main._meeting_voice_enrollment_context(s,m,manual=True)[0]


def test_filter_before_eight_item_cap(client, tmp_path):
    eid, mid, _ = seed(tmp_path, transcript=[turn(n*10,n*10+8,str(n)) for n in range(10)])
    settings_store.set_many({'voice_debug_mode': True})
    with SessionLocal() as s:
        for n in range(8):
            s.add(EmployeeVoiceSample(employee_id=eid, encrypted_embedding=b'', source_kind='meeting_confirmed', source_meeting_id=mid, source_start=n*10, source_end=n*10+8))
        s.commit()
        assert len(main._meeting_voice_enrollment_context(s,s.get(Meeting,mid))[0]) == 2


def test_real_preview_exact_stored_db_audio_and_pending(client, tmp_path, monkeypatch):
    eid, mid, source = seed(tmp_path)
    monkeypatch.setattr(config, 'voice_service_enabled', lambda:False)
    preview = client.get(f'/meetings/{mid}/speaker-clip?start=0&end=8')
    assert preview.status_code == 200 and preview.headers['cache-control'] == 'private, no-store'
    assert post(client,eid,mid).status_code == 303
    row = samples()[0]
    assert row.embedding_status == 'pending' and not row.encrypted_embedding
    assert row.sample_blob_path == '' and row.audio_bytes == len(preview.content)
    assert row.encrypted_audio != preview.content
    assert speaker_id.decrypt_sample_audio(row) == preview.content
    assert row.source_end-row.source_start == row.sample_duration == 8
    assert row.confirmed_by and row.consent_at and row.expires_at and row.source_audio_sha256
    with wave.open(io.BytesIO(preview.content)) as w:
        pcm=w.readframes(w.getnframes())
    assert row.content_sha256 == hashlib.sha256(pcm).hexdigest()
    source.unlink()
    audio=client.get(f'/employees/{eid}/voice-samples/{row.id}/audio')
    assert audio.status_code == 200 and audio.content == preview.content
    assert client.get(f'/meetings/{mid}?voice_enroll=1').text.count('<audio') == 0


@pytest.mark.parametrize('consent,listened',[('', 'yes'),('yes',''),('','')])
def test_both_listening_and_consent_required(client,tmp_path,consent,listened):
    eid,mid,_=seed(tmp_path)
    assert post(client,eid,mid,consent=consent,listened=listened).status_code == 303
    assert not samples()


def test_silence_clipping_and_truncation_rejected(client,tmp_path):
    eid,mid,path=seed(tmp_path,silent=True)
    assert client.get(f'/meetings/{mid}/speaker-clip?start=0&end=8').status_code==400
    post(client,eid,mid)
    assert not samples()
    make_wav(path,seconds=3)
    post(client,eid,mid)
    assert not samples()
    bad=tmp_path/'clipped.wav'
    with wave.open(str(bad),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(array.array('h',[32767]*128000).tobytes())
    assert not speaker_id.clip_has_speech(bad)


def test_quality_gate_not_a_claim_of_speech(tmp_path):
    # Synthetic tone is a signal fixture, not a biometric or speech-quality benchmark.
    p=make_wav(tmp_path/'tone.wav',seconds=8)
    assert speaker_id.clip_has_speech(p)
    assert 'NOT a speech detector' in speaker_id.clip_has_speech.__doc__


def test_service_error_pending_retry_and_real_model(client,tmp_path,monkeypatch):
    eid,mid,_=seed(tmp_path)
    monkeypatch.setattr(config,'voice_service_enabled',lambda:True)
    def fail(*a): raise speaker_id.SpeakerIdentityError('offline')
    monkeypatch.setattr(speaker_id,'embed_voice_sample',fail)
    post(client,eid,mid)
    row=samples()[0]
    assert row.embedding_status=='failed' and not row.encrypted_embedding
    assert speaker_id.enrolled_embeddings()==[]
    monkeypatch.setattr(speaker_id,'embed_voice_sample',embedding_result)
    response=client.post(f'/employees/{eid}/voice-samples/{row.id}/retry',follow_redirects=False)
    assert response.status_code==303
    row=samples()[0]
    assert row.embedding_status=='ready' and row.model_version=='real-model-v1'
    assert len(speaker_id.enrolled_embeddings())==1


def test_transient_service_does_not_store_audio_remote(client,tmp_path,monkeypatch):
    eid,mid,_=seed(tmp_path)
    monkeypatch.setattr(config,'voice_service_enabled',lambda:True)
    monkeypatch.setattr(config,'voice_service_url',lambda:'https://voice.invalid')
    monkeypatch.setattr(config,'voice_service_token',lambda:'fixture')
    calls=[]
    def fake_post(url, **kw):
        calls.append(kw)
        return SimpleNamespace(raise_for_status=lambda:None,json=lambda:embedding_result())
    monkeypatch.setattr(speaker_id.httpx,'post',fake_post)
    post(client,eid,mid)
    assert calls[0]['data']['purpose']=='segment'
    assert samples()[0].sample_blob_path==''


def test_provider_invalid_vector_or_fake_model_fails_closed(client,tmp_path,monkeypatch):
    eid,mid,_=seed(tmp_path)
    monkeypatch.setattr(config,'voice_service_enabled',lambda:True)
    monkeypatch.setattr(speaker_id,'embed_voice_sample',lambda *a:dict(embedding=[1]*192,model_version='local-confirmed-v1'))
    post(client,eid,mid)
    row=samples()[0]
    assert row.embedding_status=='failed' and not row.encrypted_embedding


def test_duplicate_normalized_audio_across_meetings_rejected(client,tmp_path):
    eid,mid,path=seed(tmp_path)
    post(client,eid,mid)
    for _ in range(2):
        with SessionLocal() as s:
            m=Meeting(title='reimport',status='done',audio_path=str(path),transcript=[turn()]);s.add(m);s.commit();another=m.id
        post(client,eid,another)
    assert len(samples())==1
    assert speaker_id.independent_sample_count(samples())==0  # pending is not usable evidence


def test_concurrent_confirmation_db_unique(client,tmp_path):
    eid,mid,_=seed(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:post(client,eid,mid),range(2)))
    assert all(r.status_code==303 for r in results)
    assert len(samples())==1
    assert any(i['unique'] and i['column_names']==['content_sha256'] for i in inspect(db.engine).get_indexes('employee_voice_samples'))


def test_independence_readiness_validity_ambiguity_and_empty_roster():
    refs=[valid_sample(source=str(i),content=str(i)) for i in range(3)]
    assert speaker_id.independent_sample_count(refs)==3
    result=speaker_id.identification_readiness({1:refs})
    assert result['collection_sufficient'] and not result['identity_validated'] and not result['calibrated']
    assert not speaker_id.identification_readiness({1:refs,2:[]})['ready']
    assert not speaker_id.identification_readiness({1:refs,2:[valid_sample(eid=2)]})['ready']
    assert speaker_id.independent_sample_count([valid_sample(content='same',source=str(i)) for i in range(3)])==1
    assert speaker_id.independent_sample_count([valid_sample(content=str(i),source='same') for i in range(3)])==1
    for bad in [dict(consent_at=None),dict(confirmed_by=''),dict(embedding_status='pending'),dict(encrypted_embedding=b'x'),dict(model_version='unknown'),dict(expires_at=now()-dt.timedelta(seconds=1))]:
        r=valid_sample();vars(r).update(bad)
        assert not speaker_id.sample_has_usable_embedding(r)


def add_user(username='other',role='user'):
    with SessionLocal() as s:
        u=User(username=username,password_hash=accounts.hash_password('password123'),role=role,status='approved')
        s.add(u);s.commit();return u.id


def test_private_recording_scope_even_other_superuser(client,tmp_path):
    eid,mid,path=seed(tmp_path)
    with SessionLocal() as s:
        owner=s.query(User).filter_by(username='sysai').one()
        s.add(Recording(id='private-review',owner=f'user:{owner.id}',path=str(path),mime_type='audio/wav',meeting_id=mid,status='done'));s.commit()
    post(client,eid,mid);sid=samples()[0].id
    add_user(role='superuser')
    with TestClient(main.app) as other:
        other.post('/login',data={'username':'other','password':'password123'})
        assert other.get(f'/meetings/{mid}/speaker-clip?start=0&end=8').status_code==404
        assert post(other,eid,mid).status_code==404
        assert other.get(f'/employees/{eid}/voice-samples/{sid}/audio').status_code==404
        assert other.post(f'/employees/{eid}/voice-samples/{sid}/retry').status_code==404


def test_anonymous_and_user_forbidden_mutations(client,tmp_path):
    eid,mid,_=seed(tmp_path)
    with TestClient(main.app) as anon:
        assert anon.get(f'/meetings/{mid}/speaker-clip?start=0&end=8',follow_redirects=False).status_code==303
        assert post(anon,eid,mid).status_code==303
    add_user()
    settings_store.set_many({'voice_debug_mode':True})
    with TestClient(main.app) as user:
        user.post('/login',data={'username':'other','password':'password123'})
        assert post(user,eid,mid).status_code==403
        assert 'id="voice-sample-clips"' not in user.get(f'/meetings/{mid}').text


def test_candidate_only_finite_bounds_no_temp_leaks(client,tmp_path):
    from pathlib import Path
    eid,mid,_=seed(tmp_path)
    before=set(Path('/tmp').glob('sysai_clip_*'))
    assert client.get(f'/meetings/{mid}/speaker-clip?start=0&end=8').status_code==200
    assert set(Path('/tmp').glob('sysai_clip_*'))==before
    for query in ['start=nan&end=8','start=0&end=inf','start=-1&end=8','start=1&end=7']:
        assert client.get(f'/meetings/{mid}/speaker-clip?{query}').status_code==400


def test_delete_all_revoke_changes_fingerprint_and_audio_inaccessible(client,tmp_path,monkeypatch):
    eid,mid,_=seed(tmp_path)
    monkeypatch.setattr(config,'voice_service_enabled',lambda:True)
    monkeypatch.setattr(speaker_id,'embed_voice_sample',embedding_result)
    post(client,eid,mid);row=samples()[0]
    with SessionLocal() as s:
        s.add(EmployeeVoiceprint(employee_id=eid,encrypted_voiceprint=speaker_id.encrypt_voiceprint('legacy')));s.commit()
    before=speaker_id.profiles_fingerprint(speaker_id.enrolled_embeddings())
    assert client.post(f'/employees/{eid}/voice-sample/delete',follow_redirects=False).status_code==303
    assert not samples()
    assert client.get(f'/employees/{eid}/voice-samples/{row.id}/audio').status_code==404
    assert before != speaker_id.profiles_fingerprint(speaker_id.enrolled_embeddings())
    with SessionLocal() as s:
        assert not s.query(EmployeeVoiceprint).count()
        assert s.get(Employee,eid)


def test_employee_delete_and_expiry_purge(client,tmp_path):
    eid,mid,_=seed(tmp_path);post(client,eid,mid)
    sid=samples()[0].id
    with SessionLocal() as s:
        s.get(EmployeeVoiceSample,sid).expires_at=now()-dt.timedelta(seconds=1);s.commit()
    assert client.get(f'/employees/{eid}/voice-samples/{sid}/audio').status_code==404
    with SessionLocal() as s:
        assert speaker_id.purge_expired_samples(s)==1;s.commit()
    assert not samples()
    post(client,eid,mid)
    assert client.post(f'/employees/{eid}/delete',follow_redirects=False).status_code==303
    assert not samples()


def test_meeting_delete_source_provenance_cannot_poison_reused_id(client,tmp_path):
    eid,mid,path=seed(tmp_path);post(client,eid,mid)
    assert client.post(f'/meetings/{mid}/delete',follow_redirects=False).status_code==303
    assert not samples()
    make_wav(path,seconds=10,frequency=600)
    with SessionLocal() as s:
        m=Meeting(title='new',status='done',audio_path=str(path),transcript=[turn()]);s.add(m);s.commit()
        assert main._meeting_voice_enrollment_context(s,m,manual=True)[0]


def test_migration_real_legacy_notnull_insert_pending(client,tmp_path):
    with db.engine.begin() as c:
        c.execute(text('DROP TABLE employee_voice_samples'))
        c.execute(text('CREATE TABLE employee_voice_samples (id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, sample_blob_path VARCHAR(600) NOT NULL, encrypted_embedding BLOB NOT NULL, embedding_dimensions INTEGER NOT NULL DEFAULT 192, model_version VARCHAR(120) NOT NULL, sample_duration FLOAT NOT NULL DEFAULT 0, waveform_data JSON NOT NULL, consent_at TIMESTAMP NOT NULL, consent_version VARCHAR(40) NOT NULL, created_at TIMESTAMP NOT NULL)'))
        c.execute(text("INSERT INTO employee_voice_samples VALUES (17,1,'old-path',x'41',192,'old-model',8,'[]',CURRENT_TIMESTAMP,'v1',CURRENT_TIMESTAMP)"))
    db.init_db();db.init_db()
    with SessionLocal() as s:
        old=s.get(EmployeeVoiceSample,17)
        assert old.embedding_status=='pending' and old.source_kind=='manual' and old.expires_at is None
        assert not speaker_id.sample_has_usable_embedding(old)
    eid,mid,_=seed(tmp_path)
    post(client,eid,mid)
    assert len(samples())==2
    assert samples()[-1].encrypted_audio and not samples()[-1].encrypted_embedding


def test_settings_debug_presence_semantics(client):
    assert 'Отладочный режим' in client.get('/settings').text
    client.post('/settings',data={'present_voice_debug_mode':'1','voice_debug_mode':'on'})
    assert settings_store.get('voice_debug_mode') is True
    client.post('/settings',data={'present_voice_debug_mode':'1'})
    assert settings_store.get('voice_debug_mode') is False


def test_cross_origin_confirmation_blocked(client,tmp_path):
    eid,mid,_=seed(tmp_path)
    res=client.post(f'/meetings/{mid}/speaker-clips/confirm',data=dict(speaker='Спикер 1',start=0,end=8,employee_id=eid,consent='yes',listened='yes'),headers={'Origin':'https://evil.invalid'})
    assert res.status_code==403 and not samples()
