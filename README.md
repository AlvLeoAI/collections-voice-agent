# Outbound Collections Voice Agent

A portfolio demonstration of **engineering judgment on when *not* to use an LLM** in a regulated consumer-finance domain (outbound collections). Deterministic Python controls every phase transition, right-party ZIP verification gate, payment-date bound (`today <= ptp_date <= end_of_month`), `Decimal`-based partial-offer calculation, and fail-closed pre-dial compliance check. Optional **Google Gemini 3.5 Transcribe** (`gemini-3.5-transcribe`, `store=False`) and **ElevenLabs** (`eleven_turbo_v2_5`) adapters are strictly lazy-loaded for voice I/O — the core agent, FastAPI backend, Streamlit text mode, CLI replays, and all 103 tests (+ 49 subtests) run completely offline with **zero API keys**.

**Repository**: https://github.com/AlvLeoAI/collections-voice-agent

## Features

- **Deterministic phase-based state machine** — `pre_verification` → `verification` → `post_verification` → `ended` with sub-millisecond turn routing and reproducible scenario replay
- **Hard-coded right-party verification gate** — ZIP confirmation enforced structurally inside `_handle_verification()` before `_deliver_disclosure_and_start_negotiation()` is ever reachable; no debt, amount, or creditor details are disclosed before `right_party_verified == True`
- **Bounded payment-date validation** — `_validate_ptp_date()` enforces `today <= ptp_date <= end_of_month`, rejecting past dates (`in_past`) and out-of-month dates (`after_month_end`) with conversational reconduction
- **Confirm-before-record promises** — A promise-to-pay is recorded directly only for an unambiguous full-balance date. A date that is negated (`"No, not February 25"`), hedged, tied to a different amount, or given after a refusal is read back with its amount and needs an explicit yes
- **Dynamic partial payment offers** — `_build_partial_offer()` computes a 50% `Decimal` (`ROUND_HALF_UP`) partial offer clamped to the current month-end and routes through `confirm_payment_date`; a partial amount the caller states with a currency marker (`"$50"`, `"fifty dollars"`) is parsed by `_extract_stated_amount()` and confirmed at that amount, never at the full balance
- **Ambiguous date normalization** — ES/EN date utility (`date_normalizer.py`) parses ISO dates, `"tomorrow"` / `"mañana"`, `"end of month"` / `"fin de mes"`, `"next month"` / `"próximo mes"`, explicit month-day phrases, and weekday names with `needs_confirmation` gating
- **Scored English-only intent classification** — Regex-based classifier (`intent_classifier.py`) with 13 intent labels, priority ranking, curly-apostrophe normalization, negation-aware guards, and low-confidence ambiguity detection (`is_low_confidence_unknown()`)
- **Voice-first response guard** — `_enforce_voice_first()` keeps every assistant turn to ≤ 2 short sentences and ≤ 1 question while preserving question sentences
- **Fail-closed pre-dial compliance & cease-contact persistence** — `evaluate_pre_dial_gate()` blocks dialing (`allowed=False, retryable=False`) on invalid timezones, missing/malformed/inverted call windows, or invalid cap/gap settings, and merges persisted per-account suppressions (`dnc`, `cease_contact`, `legal_hold`) with job flags via logical OR
- **Outbound job orchestration** — Queue-based worker with cron/webhook/manual triggers, expired-lease reclamation (`JobEvent.LEASE_EXPIRED`), deterministic UTC-day or explicit `idempotency_key` deduplication, exponential backoff retries, and dead-letter handling
- **PII-safe atomic persistence** — Atomic temp-file + `fsync` + `os.replace()` JSON writes (`storage_utils.py`), path-traversal ID validation (`validate_safe_id()`), unconditional `last_user_utterance = ""` redaction on disk (`_sanitize_call_state()`), and metadata-only voice logs
- **Opt-in voice adapters** — Lazy-loaded Gemini 3.5 Transcribe STT (`store=False`, in-memory browser transcription) and ElevenLabs in-browser TTS (`st.audio()`) plus optional local CLI playback (`--voice`)
- **Offline CI & test suite** — 103 tests (+ 49 subtests) running under `pytest` with outbound TCP/UDP sockets blocked by `tests/conftest.py`, `ruff` linting, and GitHub Actions CI

## Project Structure

```text
src/
├── outbound_voice_agent/           # Core deterministic agent (zero AI SDK imports)
│   ├── agent.py                    # State machine: start_call(), handle_turn()
│   ├── state.py                    # CallState (Pydantic, extra="forbid")
│   ├── intent_classifier.py        # English-only scored regex intent classifier
│   └── tools/
│       └── date_normalizer.py      # ES/EN relative & explicit date normalization
├── api/                            # FastAPI backend & local JSON stores
│   ├── server.py                   # REST endpoints (/health, /call/*, /jobs/*, /attempts/*, /metrics/summary)
│   ├── storage_utils.py            # validate_safe_id() + atomic_write_json()
│   ├── call_store.py               # PII-safe atomic call persistence
│   ├── compliance.py               # Fail-closed pre-dial gate + suppression merge
│   ├── outbound_orchestration.py   # Job state machine + lease reclamation
│   ├── job_store.py                # Atomic job queue with retry/dead-letter
│   ├── contact_attempt_store.py    # Per-account attempt & suppression ledger
│   └── metrics.py                  # Aggregated call/job metrics
└── voice_handler.py                # Optional lazy Gemini 3.5 Transcribe STT + ElevenLabs TTS
frontend/
└── app.py                          # Streamlit sandbox (offline text mode + opt-in browser voice)
scripts/
├── run_outbound_demo.py            # CLI: offline interactive + scenario replay (--voice opt-in)
├── smoke_api_demo.py               # API smoke flows (--temp-runtime supported)
├── smoke_worker_compliance.py      # Worker pre-dial compliance smoke test
├── run_outbound_worker.py          # Queue consumer worker
├── analyze_nlu_report.py           # NLU confidence analytics
└── scenarios/                      # 5 predefined scenario JSON files
tests/
├── conftest.py                     # Autouse offline socket & env-var test guard
├── test_intent_classifier.py       # Intent & negation-aware pattern tests
├── test_outbound_agent_regressions.py  # State machine & policy regression tests
├── test_compliance.py              # Fail-closed pre-dial compliance tests
├── test_contact_attempt_store.py   # Attempt & suppression ledger tests
├── test_call_store.py              # PII redaction, path traversal & atomic write tests
├── test_job_store.py               # Queue idempotency, retry & lease reclamation tests
├── test_outbound_orchestration.py  # Job state-machine transition tests
├── test_server.py                  # FastAPI TestClient & cease-contact end-to-end tests
├── test_voice_handler.py           # Lazy voice adapter & metadata-only logging tests
├── test_call_metrics.py            # Metrics aggregation tests
└── test_nlu_report.py              # NLU report tests
.github/workflows/ci.yml            # Offline GitHub Actions CI pipeline
pyproject.toml                      # uv project metadata, ruff & pytest config
uv.lock                             # Locked dependency graph
.env.example                        # Optional environment variable template
Dockerfile                          # Non-root Python 3.12 + uv Cloud Run image
.dockerignore                       # Container build exclusions
DEPLOYMENT.md                       # Local Docker & Google Cloud Run deployment guide
```

## Quick Start

### 1. Install dependencies with `uv`

Core offline environment (zero AI SDKs required):

```bash
uv sync --locked
```

Optional voice extras (Gemini 3.5 Transcribe STT + ElevenLabs TTS):

```bash
uv sync --locked --extra voice
```

### 2. Run linting and the offline test suite

```bash
uv run ruff check .
uv run pytest -v
```

### 3. Run offline CLI scenario replays

```bash
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/happy_path_ptp_end_of_month.json
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/dispute_escalation.json
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/wrong_party.json
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/silence_callback.json
uv run python scripts/run_outbound_demo.py --scenario scripts/scenarios/date_too_late_reconduct.json
```

Interactive CLI mode (text-only by default; add `--voice` when `ELEVENLABS_API_KEY` is configured):

```bash
uv run python scripts/run_outbound_demo.py
```

### 4. Run FastAPI + Streamlit sandbox

```bash
# Terminal 1 — FastAPI (http://127.0.0.1:8000)
uv run uvicorn src.api.server:app --host 0.0.0.0 --port 8000 --workers 1

# Terminal 2 — Streamlit (http://127.0.0.1:8501)
uv run streamlit run frontend/app.py
```

Text mode works offline with zero API keys. Set `GEMINI_API_KEY` and/or `ELEVENLABS_API_KEY` (see `.env.example`) to unlock optional browser mic transcription and in-browser TTS playback.

### 5. Run smoke tests & NLU analytics

```bash
# In-process API smoke flows without modifying runtime/calls/
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario happy_path
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario dispute
uv run python scripts/smoke_api_demo.py --mode inprocess --temp-runtime --scenario wrong_party

# Worker compliance gate smoke test
uv run python scripts/smoke_worker_compliance.py

# NLU analytics over persisted calls
uv run python scripts/analyze_nlu_report.py --calls-dir runtime/calls --top 8
```

### 6. Container & Google Cloud Run deployment

See [DEPLOYMENT.md](DEPLOYMENT.md) for local Docker verification and Google Cloud Run deployment instructions (including the single-instance persistence constraint and optional two-service UI setup).

## API Endpoints

### Service Health & Call Lifecycle

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health probe returning `{"status": "ok", "service": "collections-voice-agent", "llm_in_core_path": false}` |
| `POST` | `/call/start` | Start a new call for a required `account_ref` (`422` if missing or empty), returns `call_id`, initial response, and `call_state` |
| `POST` | `/call/turn` | Advance call state with a `TurnEvent`; an in-call cease-contact request is persisted against the account bound at `/call/start` before the ended call is saved, and reported as `cease_contact_persisted` |
| `GET` | `/call/{call_id}` | Retrieve sanitized call summary with turn/action history and final outcome |
| `GET` | `/metrics/summary` | Aggregated call metrics (PTP rate, time-to-PTP, daily trends) and job/compliance counts |

### Outbound Job Queue & Contact Attempts

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/jobs/enqueue` | Enqueue an outbound job (supports suppression flags and optional `idempotency_key`) |
| `GET` | `/jobs` | List jobs with optional `state` and `campaign_id` filters |
| `GET` | `/jobs/{job_id}` | Fetch a single job by ID (`400` on invalid ID, `404` if missing) |
| `POST` | `/jobs/lease` | Reclaim expired leases, requeue due retries, and lease next due job |
| `POST` | `/jobs/{job_id}/start` | Mark leased job as `running` and open a new attempt record |
| `POST` | `/jobs/{job_id}/success` | Mark running job as `succeeded` with outcome code |
| `POST` | `/jobs/{job_id}/failure` | Mark running job as `failed`, scheduling retry (`waiting_retry`) or `dead_letter` |
| `GET` | `/attempts` | Recent pre-dial decision and suppression audit events |
| `GET` | `/attempts/{account_ref}` | Decision and suppression audit history for one account |

## PII & Privacy Controls

- `CallState.last_user_utterance` is never populated by `handle_turn()` and is unconditionally cleared (`""`) before persistence in `JsonCallStore._sanitize_call_state()`.
- `src/voice_handler.py` logs only non-sensitive metadata (provider, model, MIME type, byte length, char/word counts, latency) — never raw user transcripts or assistant text.
- Streamlit STT transcribes audio bytes in memory via `VoiceHandler.transcribe_audio_bytes()` with zero local audio file writes, and passes `store=False` to the Gemini Interactions API so audio is not retained server-side.
- `validate_safe_id()` rejects invalid or path-traversal IDs before touching the filesystem (`HTTP 400`).

## Test Suite

- **103 tests (+ 49 subtests)** via `uv run pytest -v`, running in < 1 second with **zero API keys** and all outbound non-UNIX sockets blocked by `tests/conftest.py`.
- See [TESTING.md](TESTING.md) for the full verification checklist and [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) for architecture and trade-offs.

## Honest Scope & Known Limitations

1. **Single-process JSON persistence (`runtime/`):** Call records, jobs, and attempt/suppression ledgers use local JSON files with in-process `threading.Lock` and atomic `os.replace()` writes. On Cloud Run, the API service must run with `--max-instances 1` and `--workers 1`, and local container disk is ephemeral across restarts. Production deployments require a shared database (e.g., Cloud SQL PostgreSQL) and managed queue (e.g., Cloud Tasks or Pub/Sub).
2. **English-only (`en-US`) conversation & NLU vs. ES/EN date utility:** Disclosures, templates, and `intent_classifier.py` are explicitly scoped to English (`en-US`), while `date_normalizer.py` retains ES/EN phrase helpers. Non-English utterances (such as `"sí"`) intentionally fall back to low-confidence `unknown` → clarification → human escalation rather than risking missed non-English disputes or cease-contact requests.
3. **Pattern-based NLU coverage limits:** Regex rules trade semantic generalization for deterministic auditability. Unusual phrasing outside covered patterns relies on ambiguity checks and the 2-strike low-confidence escalation path. Stated amounts are recognized only with a currency marker (`$`, `dollars`, `bucks`), and `"today"` is handled as a yes/no answer rather than as a parsed date.
4. **No real telephony or media streaming:** There is no SIP/PSTN/Twilio integration, real-time duplex WebRTC transport, or barge-in handling.
5. **Opt-in external voice adapters vs. offline tests:** Live STT (`gemini-3.5-transcribe`) and TTS (`eleven_turbo_v2_5`) depend on external APIs when enabled; unit tests exercise those code paths using deterministic in-memory test doubles.
6. **Public, unauthenticated synthetic-data sandbox:** The documented Cloud Run deployment uses `--allow-unauthenticated` because it is a public synthetic-data sandbox where Streamlit calls the FastAPI backend server-side with plain `httpx` and no built-in authentication middleware. Production deployments handling real debtor data must enforce IAM / ID-token (`--no-allow-unauthenticated`) or OAuth2 authentication.
