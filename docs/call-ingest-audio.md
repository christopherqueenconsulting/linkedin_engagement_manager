# Call ingest — audio (issue #2729, Phase 2 of #2629)

A recording the user uploads becomes story-bank facts through the SAME redact → extract → verify →
review path as a text transcript. Everything is behind `CALL_INGEST_ENABLED` (default off) and the
privacy text stays DRAFT.

## Flow

1. `POST /user/call-ingest/enroll` — a 5 s to 2 min sample of the user alone. Stores ONE sealed
   voice embedding (`call_voice_enrollment`, AAD `call_ingest.voice_embedding`); the sample is
   deleted before the response. Removed by `DELETE /user/call-ingest`.
2. `POST /user/call-ingest/upload` with an `.m4a/.mp3/.wav/.webm` file and `source_type=audio`.
   409 `needs_enrollment` without an enrollment. Size cap 60 MB (streamed), duration cap 60 min
   (checked by the worker BEFORE any transcription spend). The file lands in
   `assets/call_audio/` and `transcribe_call_audio` is queued.
3. `transcribe_call_audio`: decode (ffmpeg, 16 kHz mono) → Whisper through the proxy
   (`lem-transcribe`, about $0.006/min, booked as an `llm_call` with `feature=call_ingest`) →
   each segment is matched to the enrollment → only the user's turns are redacted and stored →
   the audio is deleted → `ingest_call_transcript` takes over.

## Invariants

* **UNKNOWN, never a guess.** A segment is the user's only when its cosine similarity to the
  enrollment reaches `CALL_SPEAKER_ACCEPT` (default 0.65). Segments under 1.5 s, unembeddable
  segments and every non-match are `unknown` and dropped by `select_own_turns`; another
  speaker's words are never stored.
* **Audio never outlives the attempt.** Deleted on success, on any failure, when the flag is off,
  and by the daily `purge_expired_call_transcripts` beat once `audio_purge_after` (24 h) passes.
* Only `SpeakerEngine.embed` is used (L2-normalised, length-standardised by the class — never
  pre-pad). Matching is our own cosine, so no unverified profile API is relied on.
* **Model file:** `CALL_SPEAKER_MODEL_PATH` must name the sherpa-onnx CAM++ `.onnx`, baked into
  the image and pinned by SHA-256 — never fetched at runtime. Unset or missing raises
  `SpeakerSetupError` (503 on enroll, a failed ingest with one ERROR on upload).
* Never add this feature to `LLM_PROMPT_LOGGING_FEATURES`.

## Image size

The API and main Celery worker share the runtime image. Both use
`CALL_SPEAKER_MODEL_PATH=/opt/models/campplus_en_16k.onnx`. The `final` stage inherits
`ffmpeg` from `base` for decoding audio in both services.

The baked CAM++ model adds 29,596,979 bytes. On Linux x86-64, the Python 3.13
`sherpa-onnx` 2.0.3 wheel pinned in `poetry.lock` is 4,644,006 bytes, and its locked
`sherpa-onnx-core` 2.0.3 wheel is 10,943,178 bytes. Wheel sizes describe the compressed
downloads. The post-merge CI image build will provide the exact image size delta.
