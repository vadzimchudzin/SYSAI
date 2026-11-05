# SYSAI Speaker Identity Service

A separate Vercel Python API that computes voice embeddings for SYSAI. It does not own employee records, meeting transcripts, or speaker-to-employee mappings; those remain in SYSAI's main database on Render.

## API

- `GET /health` — public health check; does not load the model.
- `POST /v1/embed` — returns a 192-dimensional normalized embedding for one profile sample or one short speaker segment. For `purpose=profile`, stores the original uploaded audio in **private** Vercel Blob and returns its pathname plus a 96-bin waveform.
- `POST /v1/identify` — accepts one diarized audio chunk and relative `start`, `end`, and `speaker` values; combines fragments per local speaker and returns one embedding per speaker. Optional form field `min_seconds` (0.3–1.0, default 1.0) also embeds shorter speakers — SYSAI sends every short reply («да», «нет, не так») as a speaker of its own to check who said it.
- `DELETE /v1/sample` — deletes a private reference sample. It only accepts paths under the supplied employee's profile prefix.

All endpoints except health require `X-SYSAI-Voice-Token`. Do not expose the token to browser JavaScript.

## Required Vercel configuration

- `SYSAI_VOICE_SERVICE_TOKEN`: long random secret shared only with the Render backend.
- `BLOB_READ_WRITE_TOKEN`: attached automatically when the private Blob store is connected to this project.

Create the Blob store with **Private** access. Source audio is uploaded under random paths such as `profiles/employee-12/<uuid>.wav`; the service returns the pathname, not a public URL.

## Model

The service uses an ONNX conversion of SpeechBrain ECAPA-TDNN from Hugging Face repo `vedk00/ecapa-voxceleb-speaker-embedding-onnx` at revision `a9cb932`, Apache-2.0 licensed. The 83 MB model and small fbank table are downloaded on first model use and cached under `/tmp` for warm invocations; they are not committed to this repository. Output vectors are L2-normalized and have 192 dimensions.

Features are SpeechBrain's `Fbank(n_mels=80)` with sentence mean normalisation, reproduced in NumPy: 25 ms periodic Hamming window, 10 ms hop, centred zero padding, power spectrum, the 80 mel filters, `10·log10` with `top_db=80`. Note that `fbank-80x201-f32.bin` stores SpeechBrain's matrix as 201 FFT bins × 80 filters despite its name; `_filters()` reads it that way and refuses a table that is not a set of triangular filters. Checked against SpeechBrain 1.0.3 itself: features within 0.002 dB, embeddings identical (cos 1.000); on LibriSpeech test-clean the equal error rate is 0.7% for whole utterances, 1.5% for 3 s and 3.5% for 1.5 s. Model version `speechbrain-ecapa-voxceleb-onnx-a9cb932-fbank2`; prints of the earlier `…-a9cb932` came from wrong features (EER ≈ 30%, every voice ~0.93 alike) and are not compared with the new ones.

This model was trained on VoxCeleb data. Its performance on Russian-language meetings, noisy microphones, short turns, and overlapping speech is **not yet validated**. Thresholds must be calibrated against consented recordings before production auto-labeling.

## Expected audio

SYSAI should submit WAV or MP3 through the server-to-server API. Profile samples must be 5–30 seconds; incoming samples should be clean, single-speaker speech. Speaker-match segments can be shorter, but very short segments are less reliable. Upload limit is 4 MiB per request.

The API authenticates every operation with a shared secret, has no CORS/browser access configured, does not accept employee names, and never performs the final employee match itself.
