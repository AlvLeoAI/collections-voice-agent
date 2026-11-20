# Design Decisions: Outbound Collections Voice Agent

## 1. Architecture: Single Agent, Phase-Based State Machine

The system uses a **single deterministic agent** rather than multiple LLM agents. In a regulated outbound collections workflow, keeping phase routing, disclosure gating, and policy limits in one auditable Python code path avoids inter-agent handoff drift and makes every state transition directly unit-testable.

The `Phase` literal defines `pre_verification`, `verification`, `post_verification`, `closing`, `escalation`, and `ended`. In the active runtime flow, calls progress through `pre_verification` → `verification` → `post_verification` → `ended` (`closing` and `escalation` are reserved phase literals in `state.py` for future multi-turn closing/handoff flows; currently terminal closings and escalations transition directly to `ended` with an explicit `end_reason`).

Phase transitions are driven by scored intent classification in `classify_utterance()` (`src/outbound_voice_agent/intent_classifier.py`). Before phase routing, `handle_turn()` (`src/outbound_voice_agent/agent.py`) evaluates universal guards (`stop_request`, `goodbye`, `human_handoff`) so opt-out and escalation requests take precedence in any active phase. Every assistant response passes through `_wrap_response()` and `_enforce_voice_first()`, keeping spoken output to ≤ 2 short sentences and ≤ 1 question while preserving question sentences if truncation ever occurs.

## 2. Verification Sufficiency

Right-party verification requires the caller to confirm their **5-digit ZIP code** against `account_context["expected_zip"]`. This is enforced structurally inside `_handle_verification()`: `_deliver_disclosure_and_start_negotiation()` is only reachable when `provided_zip == expected_zip` evaluates to `True`. Until that branch succeeds, `right_party_verified` remains `False`, `phase` stays in `verification`, and no debt amount, creditor name, or account details are emitted.

Wrong-party responses (`nlu.matched("wrong_party")`) are checked in both `_handle_pre_verification()` and `_handle_verification()` before discomfort or ZIP checks, closing cleanly as `wrong_party` without disclosure. Up to 3 failed ZIP attempts (`verification_attempts`) end the call with `verification_failed`.

ZIP extraction in `_extract_zip()` supports common speech-to-text output formats:
- Direct 5-digit strings: `"78701"`
- Split numeric forms: `"78 and 701"` → `"78701"`
- Spoken digits: `"seven eight seven zero one"` → `"78701"`
- Full number words: `"seventy eight thousand and seven hundred and one"` → `"78701"`

## 3. Intent Classification

`classify_utterance()` (`src/outbound_voice_agent/intent_classifier.py`) uses deterministic regex matching with priority ranking over 13 intent labels:

`stop_request` > `goodbye` > `human_handoff` > `wrong_party` > `dispute` > `busy` > `uncomfortable` > `refusal` > `uncertain` > `identity_question` > `affirmation` > `negation` > `unknown`

Key design details:
- **Explicit English-only (`en-US`) scope:** Patterns are intentionally scoped to English. Non-English utterances (for example, `"sí"`) fall back to low-confidence `unknown` and route through `_handle_low_confidence()` (1 clarification, then human escalation) rather than risking misrouted non-English disputes or cease-contact requests.
- **Apostrophe normalization & negation guards:** Curly apostrophes (`\u2019`) are normalized to `'` before matching. `affirmation` uses negation-aware `i can(?!'t\b|\s+not\b)` so `"I can't afford it"` never matches `affirmation`. `human_handoff` requires explicit handoff phrases (not bare `"person"`), and `busy` requires explicit callback/busy phrases (not bare `"later"`).
- **Ambiguity & confidence degradation:** Competing intents within 0.08 of the primary reduce confidence. Conflicting `affirmation` + `negation` matches without a stronger business-critical intent return low-confidence `unknown` (`0.30`), flagged by `is_low_confidence_unknown()` (`< 0.45`).

## 4. Conversation, Date Validation & Host Actions

The boundary is **conversation handles flow; deterministic helpers handle date/amount validation; the host executes side effects**:

- **Date normalization (`src/outbound_voice_agent/tools/date_normalizer.py`):** `normalize_datetime_local()` is an ES/EN utility supporting ISO dates, `"tomorrow"` / `"mañana"`, `"end of month"` / `"fin de mes"`, `"next month"` / `"próximo mes"`, explicit month-day dates, and weekday names. Ambiguous phrases (weekdays, `"next month"`) set `needs_confirmation=True`.
- **Payment-date policy (`_validate_ptp_date()`):** Inside `_handle_negotiation()`, extracted dates are validated against `today <= proposed_date <= last_day_of_current_month`. Past dates (`in_past`) and post-month-end dates (`after_month_end`) are rejected with reconduction before any PTP is recorded.
- **Dynamic partial offers (`_build_partial_offer()`):** When full payment is declined, `_build_partial_offer()` computes a 50% `Decimal` (`ROUND_HALF_UP`) partial amount and clamps the proposed date (`min(today + 5 days, end_of_month)`), storing both `last_proposed_payment_date` and `last_proposed_payment_amount` and routing through `confirm_payment_date`.
- **Host-executed actions:** `agent.py` emits minimal, non-sensitive action dictionaries directly (`set_outcome`, `create_promise_to_pay`, `mark_do_not_contact`, `escalate_to_human`, `end_call`). The API server and worker execute persistence and suppression side effects based on these actions.

## 5. Call Ending and Escalation Rules

**Calls end (`phase = "ended"`) when:**
- Promise-to-pay confirmed → `ptp_set`
- User says goodbye → `user_ended`
- User requests cease contact (`stop_request`) → `cease_contact` (sets `cease_contact_requested = True` and emits `mark_do_not_contact`)
- Wrong party confirmed → `wrong_party`
- 3 consecutive silences → `silence_timeout`
- Global turn limit reached (default 25) → `max_turns`
- Verification fails (3 attempts) or refused (after 2 reconduction prompts) → `verification_failed` / `verification_refused`
- User is busy (with no payment date extracted) → `busy`

**Calls escalate (`escalation_flag = True`, `phase = "ended"`) when:**
- Caller requests a human → `escalated_user_requested_human`
- Caller disputes the obligation → `escalated_dispute`
- Repeated refusal after 2 negotiation proposals → `escalated_hard_refusal` / `escalated_multiple_refusals`
- 2 consecutive low-confidence utterances → `escalated_low_confidence`

Ended calls are **idempotent**: once `phase == "ended"`, subsequent `handle_turn()` calls return `assistant_intent="already_closed"` with `actions=[]`.

## 6. Loop Prevention

| Counter | Limit | Consequence |
|---------|-------|-------------|
| `verification_attempts` | 3 | Call ends with `verification_failed` |
| `reconduction_attempts` | 2 | Call ends with `verification_refused` |
| `negotiation_proposals_count` | 2 | Escalation (`hard_refusal` / `multiple_refusals`) |
| `silence_count` | 3 | Call ends with `silence_timeout` |
| `clarification_attempts` | 1 (then escalates) | Escalation with `low_confidence` |
| `max_total_turns` | 25 (default) | Global safety net, ends with `max_turns` |

`_wrap_response()` updates `last_assistant_question` strictly from the voice-first constrained output (`constrained_text if "?" in constrained_text else ""`) so state never diverges from spoken text.

## 7. State Preservation & PII Hygiene

All call state lives in `CallState` (`src/outbound_voice_agent/state.py`, Pydantic `ConfigDict(extra="forbid", validate_assignment=True)`):
- **Phase & verification**: `phase`, `turn_count`, `right_party_verified`, `right_party_confidence`, `disclosure_delivered`
- **Counters**: `verification_attempts`, `silence_count`, `negotiation_proposals_count`, `reconduction_attempts`, `clarification_attempts`
- **Proposals & commitments**: `last_proposed_payment_date`, `last_proposed_payment_amount`, `promise_to_pay.{date, amount, confirmed}`, `callback.{requested, datetime_local}`
- **Flags & closure**: `wrong_party_indicated`, `dispute_flag`, `hardship_flag`, `cease_contact_requested`, `escalation_flag`, `escalation_reason`, `end_reason`
- **Compatibility field**: `last_user_utterance` is retained as `""` for schema compatibility; `handle_turn()` never populates it and `JsonCallStore._sanitize_call_state()` unconditionally clears it before writing to disk.

`src/voice_handler.py` logs only non-sensitive metadata (provider, model, MIME type, bytes, char/word counts, latency) and never logs raw transcripts or assistant text.

## 8. Outbound Job Orchestration & Fail-Closed Compliance

The orchestration and compliance layer (`src/api/outbound_orchestration.py`, `src/api/job_store.py`, `src/api/compliance.py`, `src/api/contact_attempt_store.py`, `src/api/storage_utils.py`) enforces:

- **Job states**: `queued`, `leased`, `running`, `waiting_retry`, `succeeded`, `failed`, `dead_letter`, `canceled`
- **Expired-lease reclamation**: `JobEvent.LEASE_EXPIRED` transitions `leased` → `queued` via `reclaim_expired_lease()`. `JsonJobStore.lease_next_due_job()` automatically reclaims expired leases and requeues due retries before selecting the next candidate.
- **Deterministic idempotency**: `create_job()` / `enqueue_job()` accept an explicit `idempotency_key`; when omitted, jobs without `scheduled_for_utc` bucket by UTC day (`now.date().isoformat()`) so rapid retries deduplicate cleanly while remaining immediately due.
- **Fail-closed pre-dial gate (`evaluate_pre_dial_gate()`)**:
  - Merges persisted account suppressions (`attempt_store.get_suppressions(account_ref)`) with per-job `suppression_flags` via logical OR (`dnc`, `cease_contact`, `legal_hold`).
  - Blocks (`allowed=False, retryable=False`) on invalid IANA timezones (`blocked_policy_invalid_timezone`), invalid `daily_attempt_cap < 1` or `min_gap_minutes < 0` (`blocked_policy_invalid_config`), empty call windows (`blocked_policy_missing_call_window`), or malformed/inverted windows such as `"20:00-08:00"` (`blocked_policy_invalid_call_window`).
  - Defers (`retryable=True`) when outside valid call windows, at daily attempt cap, or within min-gap.
- **In-call cease-contact propagation**: When `/call/turn` processes a `stop_request` (and `assistant_intent != "already_closed"`), `server.py` persists `cease_contact=True` in `JsonContactAttemptStore.set_suppression()`, immediately blocking future dials for that `account_ref` even if a new job arrives with `cease_contact=False`.
- **Atomic writes & safe IDs**: All JSON stores write via `atomic_write_json()` (same-directory temp file + `flush` + `os.fsync` + `os.replace()`) and validate IDs with `validate_safe_id()` (`^[A-Za-z0-9_-]{1,64}$`).

## 9. Deterministic Core vs. Optional Voice Adapters

The core conversation and compliance engine intentionally uses **zero LLM calls**:

| Criterion | Deterministic Core | LLM-Driven Core |
|-----------|--------------------|-----------------|
| **Turn decision latency** | Single-digit ms (local Python) | ~200–2000 ms variable |
| **External dependency** | None for core decisions | Subject to model API availability |
| **Rule & disclosure adherence** | Template-bound, structurally gated in code (bugs caught by regression tests) | Probabilistic; requires guardrails against prompt drift |
| **Per-turn inference cost** | \$0 | Variable token cost per turn |
| **Generative hallucination** | Eliminated (responses come from bounded templates and validated state) | Non-zero risk of fabricated amounts or terms |
| **Testability** | Offline, repeatable unit and scenario tests | Requires eval harnesses for stochastic outputs |

**Acknowledged trade-off:** Deterministic regex NLU is less flexible on unanticipated phrasing than a semantic classifier, so unusual inputs rely on ambiguity detection and human escalation. If an LLM were introduced later, the highest-leverage placement would be a small, bounded intent-classification sidecar while keeping phase transitions, verification gates, and disclosures deterministic.

**Optional voice adapters (`src/voice_handler.py`):**
- **STT (opt-in):** **Google Gemini 3.5 Transcribe** (`gemini-3.5-transcribe`) via the lazy-loaded `google-genai` Interactions API (`client.interactions.create(..., store=False)`), accepting inline audio bytes (≤ 20 MB) in memory from the Streamlit UI.
- **TTS (opt-in):** **ElevenLabs** (`eleven_turbo_v2_5`) returning MP3 bytes rendered in-browser via `st.audio()` (and optional local CLI streaming via `mpv` when `--voice` is passed).

## 10. Testing & CI Strategy

- **84 unit & API tests (+ 17 subtests)** under `tests/` (`uv run pytest -v`), running offline in < 1 second with outbound non-UNIX sockets blocked and all vendor API keys cleared by `tests/conftest.py`:
  - `test_intent_classifier.py`, `test_outbound_agent_regressions.py`, `test_compliance.py`, `test_contact_attempt_store.py`, `test_call_store.py`, `test_job_store.py`, `test_outbound_orchestration.py`, `test_server.py`, `test_voice_handler.py`, `test_call_metrics.py`, `test_nlu_report.py`.
- **5 offline CLI scenario replays** (`scripts/run_outbound_demo.py --scenario ...`).
- **3 in-process API smoke flows** (`scripts/smoke_api_demo.py --mode inprocess --temp-runtime`) and **worker compliance smoke test** (`scripts/smoke_worker_compliance.py`).
- **Automated CI** (`.github/workflows/ci.yml`): verifies `uv lock --check`, `uv sync --locked`, `uv run ruff check .`, `uv run pytest -v`, all 5 scenarios, and all smoke scripts with blank API keys.
