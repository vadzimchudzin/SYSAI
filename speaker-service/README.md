# SYSAI Speaker Identity Service

A separate Vercel Python API that computes voice embeddings for SYSAI. It does not own employee records, meeting transcripts, or speaker-to-employee mappings; those remain in SYSAI's main database on Render.

## API

- `GET /health` — public health check; does not load the model.
- `POST /v1/embed` — returns a 192-dimensional normalized embedding for one profile sample or one short speaker segment. For `purpose=profile`, stores the original uploaded audio in **private** Vercel Blob and returns its pathname plus a 96-bin waveform.
- `POST /v1/identify` — accepts one diarized audio chunk and relative `start`, `end`, and `speaker` values; combines fragments per local speaker and returns one embedding per speaker.
- `DELETE /v1/sample` — deletes a private reference sample. It only accepts paths under the supplied employee's profile prefix.

All endpoints except health require `X-SYSAI-Voice-Token`. Do not expose the token to browser JavaScript.

## Required Vercel configuration

- `SYSAI_VOICE_SERVICE_TOKEN`: long random secret shared only with the Render backend.
- `BLOB_READ_WRITE_TOKEN`: attached automatically when the private Blob store is connected to this project.

Create the Blob store with **Private** access. Source audio is uploaded under random paths such as `profiles/employee-12/<uuid>.wav`; the service returns the pathname, not a public URL.

## Model

The service uses an ONNX conversion of SpeechBrain ECAPA-TDNN from Hugging Face repo `vedk00/ecapa-voxceleb-speaker-embedding-onnx` at revision `a9cb932`, Apache-2.0 licensed. The 83 MB model and small fbank table are downloaded on first model use and cached under `/tmp` for warm invocations; they are not committed to this repository. Output vectors are L2-normalized and have 192 dimensions.

This model was trained on VoxCeleb data. Its performance on Russian-language meetings, noisy microphones, short turns, and overlapping speech is **not yet validated**. Thresholds must be calibrated against consented recordings before production auto-labeling.

## Expected audio

SYSAI should submit WAV or MP3 through the server-to-server API. Profile samples must be 5–30 seconds; incoming samples should be clean, single-speaker speech. Speaker-match segments can be shorter, but very short segments are less reliable. Upload limit is 4 MiB per request.

The API authenticates every operation with a shared secret, has no CORS/browser access configured, does not accept employee names, and never performs the final employee match itself.
