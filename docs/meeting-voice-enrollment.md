# Confirmed meeting voice samples — experimental implementation

## Status and scope

This change was implemented offline against the sanitized source supplied from parent checkout `5c69bab3130cd67c7abfb3115a81325a7322e10f`. Remote HEAD and experimental branch have NOT been verified. No push, deployment, production database access, production audio, external inference, or real biometric evaluation occurred. ASR provider/model defaults are unchanged.

The app is a single shared directory/meeting workspace. It has no tenant column. Existing approved-user access to shared meetings remains; private microphone Recording ownership is also enforced on new clip/read/confirm paths, including other superusers. Enrollment and saved-sample management require superuser role. This is not a new multi-tenant isolation layer.

## User flow

- Settings → **Отладочный режим** defaults OFF. OFF introduces no automatic enrollment prompts; a manual sample-selection link remains on authorized meeting pages.
- ON offers bounded candidate turns after transcription. These are candidates based on timestamps/overlap exclusion, NOT proof of a clean single speaker.
- All labelled and unlabelled interval overlaps are excluded. Missing/malformed/non-finite/negative timestamps are rejected. The existing transcript pipeline sometimes derives end times from the next turn, so human review is essential.
- Preview and confirmation use the same bounded mono PCM16/16kHz extraction. Truncated extraction is rejected. Confirm stores the exact preview-compatible WAV, PCM hash, actual duration/interval, source recording hash and meeting.
- Signal-quality checks reject silence/low signal, excessive inactive frames and heavy clipping. These are **not a trained VAD, music/noise classifier or speaker-count detector**. They cannot prove clean speech. The operator must listen to the entire clip, check a separate listening/one-speaker confirmation, choose an employee and attest separate employee consent.
- No automatic inferred identity is enrolled. Unknown/ambiguous voices are left unknown.
- Used candidates are filtered before the eight-item display limit. Missing source audio is not offered. Old source deletion cannot hide clips of a reused SQLite meeting id.

## Storage, inference and consent

Meeting audio samples are Fernet-encrypted in `employee_voice_samples.encrypted_audio`, using the existing stable SECRET_KEY-derived encryption. MIME and byte count are recorded; WAV data is limited to 1 MB (at most 30 seconds). No permanent local WAV or URL stands in for DB audio. Temp files are scoped and removed. Playback is authorized and `private, no-store`.

`embedding_status` is pending/failed/ready. An unavailable service saves audio with an **empty binary embedding**, never a synthetic vector. Empty binary keeps compatibility with the original NOT NULL encrypted_embedding column without a destructive SQLite table rebuild. Pending/failed/legacy unverified rows are excluded from recognition and readiness. A ready row must have consent, confirmation actor, valid 192D encrypted features, compatible non-placeholder model version, quality metadata, independent source metadata, DB audio and unexpired retention.

The existing voice-service `/v1/embed` is reused in `purpose=segment` mode: transient feature extraction, no reference Blob copy. `speaker-service/app.py` no longer persists automatically inferred `/v1/identify` candidates. Deployment must keep this companion service change in sync; the old deployed identify service may retain candidates. No companion service was deployed here.

Pending/failed audio is retryable through the authorized employee-card retry POST. The actor, consent version/time and original source-owner restriction are retained. Consent text discloses configured external processing and later identification use. Real provider response metadata is validated; failures do not become successful enrollment.

## Evidence versus accuracy

Collection sufficiency is an uncalibrated heuristic, **not an 80–90% accuracy statement or probability**:

- At least three distinct source recordings with distinct normalized PCM content, consented and actually embedded.
- Compatible model version, within-person feature agreement and separation from rival profiles.
- Empty active roster entries are included. Pending, corrupt, expired and unconfirmed evidence does not count.
- Distinct clips from the same source recording count once. Exact PCM reimports are rejected by a database unique index; altered/re-encoded near-duplicates are not solved by cryptographic hashing and require operator review.
- Sufficiently sampled known identities stop automatic collection, but unknown candidates are never hidden just because the enrolled subset is sufficient. Explicit manual enrollment remains available.
- Matching checks model version, minimum independent evidence, conservative score floor and runner-up margin; same-identity collisions and ambiguous/unknowns fall back. Cosine is never displayed as a percent. No held-out evaluation or calibration was performed; false acceptance/rejection rates are unknown.

## Retention, deletion and cache behavior

New samples expire after 180 days. Expired rows are ineligible immediately, playback is denied, and the existing background loop purges expired DB rows hourly. The admin cleanup route also runs purge. Source-meeting deletion, employee deletion, individual sample deletion and full voice revocation remove new DB biometrics. SQLite-specific delete triggers protect biometric rows even though legacy app FK enforcement was historically disabled. Other SQLite table behavior is not changed.

Legacy filesystem/Blob references are placed in `voice_sample_cleanup` before deletion, so failure to remove a legacy external copy is not forgotten. Only expected per-employee Blob paths or files beneath DATA_DIR/voice_samples are touched. Unknown references remain for operator review. New samples have no such external copy. External cleanup can be retried by an authorized admin; do not claim remote erasure if cleanup remains queued. Backup retention/erasure belongs to the database operator and is not managed by this code.

Recognition profiles are re-read from eligible rows. Profile fingerprint includes sample id/model/content; deleting, expiring or replacing evidence changes future checkpoint keys. Matching rechecks active profiles before and after inference. Already-issued reports are historical and are not rewritten on sample deletion.

## Migration / rollout checklist (not executed on production)

1. Obtain permitted GitHub access, verify the actual experimental branch/remote HEAD, compare the base source and review the complete patch. Never push this archive's synthetic local Git history onto main.
2. Back up a staging database. Run migrations on staging with the declared application dependencies. `init_db` adds only columns/indexes/new cleanup table; pending defaults prevent legacy data from silently becoming identification evidence. The old NOT NULL embedding column is preserved.
3. The normalized PCM content hash has a partial UNIQUE index (excluding empty legacy hashes), enforcing idempotency across concurrent workers. SQLite legacy row preservation, repeated migration, new pending insert, index creation, delete/recreate and concurrent POST paths were tested. If staging contains duplicate nonempty hashes from an unpublished intermediate build, review/deduplicate them before creating the unique index; migration must not silently discard consented data.
4. **PostgreSQL runtime migration was not tested.** PostgreSQL DDL compatibility has been reviewed, not proven by a live server. Test fresh and legacy Postgres databases, concurrent inserts and delete/retry transactions before rollout. Do not point tests at Neon production.
5. Keep a strong stable SECRET_KEY. Rotation invalidates old encrypted templates/audio unless deliberately migrated. Verify authenticated playback/denial, retention worker and consent copy in staging.
6. Update/review the companion voice service with transient identification; verify configured processor retention and privacy policy. Existing privacy page describes legacy pyannote enrollment, so operator legal review must include this new encrypted-audio/180-day/transient-service flow before enabling it.
7. Validate on consented held-out TicNote examples covering speakers, devices, noise and unknowns before assigning any accuracy target. Debug defaults OFF.

## Tests and limitations

Tests generate local tone/silence WAV fixtures and run real ffmpeg, ffprobe, encryption, database and FastAPI/Jinja routes. External model responses are stubbed where explicitly tested. Synthetic signals test handling, not recognition quality.

- Focused author/regression tests: 48 passed; zero network attempts.
- Independent safety contracts: 31 passed after documented interface adaptations. Original contracts are preserved; complete adaptation diff is included in the safety evidence artifact for independent judgment.
- Full revised tree: 1105 passed, 1 failed. Original baseline: 1080 passed, the same 1 failed. The shared failure is missing installed `telethon`, not an introduced regression. `requirements.txt` explicitly declares `telethon==1.45.0`; the reviewer can install declared dependencies and rerun.
- Both broad suites attempted six DNS operations, all blocked before resolution/connection by the offline runner. Safety/focused suites attempted none.
- Tested environment: Python 3.12, FastAPI 0.162.0, Starlette 1.10.0, SQLAlchemy 2.1.6, httpx 0.28.1, pytest 9.2.0; this differs from repository pins. A fully pinned production-like staging test remains required.

No production-readiness, deployment-success, calibrated confidence or real-world accuracy claim is made.
