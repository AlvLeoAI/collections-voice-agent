# Design Decisions: Outbound Collections Voice Agent

## 1. Architecture: Single Agent, Phase-Based State Machine

The system uses a **single deterministic agent** rather than multiple specialized agents or an LLM-driven approach. A single agent avoids inter-agent handoff complexity, eliminates serialization overhead between components, and keeps the entire call flow auditable in one code path. For a compliance-critical domain like debt collection, this traceability matters more than architectural elegance.

The agent operates as a **phase-based state machine** with five phases: `pre_verification` → `verification` → `post_verification` (negotiation) → `closing/escalation` → `ended`. Each phase gates what the agent can say and do. Phase transitions are driven by **scored intent classification**: the `classify_utterance` function (`intent_classifier.py`) returns ranked intents with confidence scores, and the agent routes to the appropriate handler based on the current phase and top intent. For example, an `affirmation` intent during `pre_verification` transitions to `verification`, while the same intent during `post_verification` may confirm a promise-to-pay. Universal intents (`stop_request`, `goodbye`, `human_handoff`) are checked before phase routing at `agent.py:87-102` and override any phase-specific logic.

The function `handle_turn()` at `agent.py:44-123` is the single entry point for every turn. It receives a `TurnEvent` (transcript + temporal metadata) and returns a dict with `assistant_text`, `assistant_intent`, `actions[]`, and the updated `call_state`. Every response passes through `_wrap_response()` at `agent.py:685-696`, which enforces the voice-first constraint via `_enforce_voice_first()` at `agent.py:704-732` — hard-limiting output to 2 sentences and 1 question.

If responsibilities were split across agents (e.g., a verifier agent and a negotiator agent), each would need its own state management, and handoff errors could leak sensitive data across phase boundaries. The single-agent design avoids this by construction.

## 2. Verification Sufficiency

Right-party verification requires the caller to confirm their **5-digit ZIP code** against the value stored in `account_context.expected_zip`. This is a **hard code gate**, not a prompt instruction: the function `_deliver_disclosure_and_start_negotiation` is only reachable through the code path where `provided_zip == expected_zip` evaluates to `True` at `agent.py:210`. Until that condition passes, `right_party_verified` remains `False`, `phase` stays in `verification`, and no debt amount, creditor name, or account details appear in any response.

This matters because prompt-level instructions can be bypassed through adversarial input or model drift. A structural gate cannot. The agent allows up to 3 verification attempts (`verification_attempts` counter); on failure, the call ends with reason `verification_failed`.

ZIP extraction (`_extract_zip()` at `agent.py:497-539`) supports multiple input formats to handle real STT output:
- Direct digits: "78701"
- Split numeric forms: "78 and 701" → "78701"
- Spoken digits: "seven eight seven zero one" → "78701"
- Full number words: "seventy eight thousand seven hundred and one" → "78701"

## 3. Intent Classification

The intent classifier (`intent_classifier.py`) uses **regex pattern matching with priority ranking** to classify user utterances into 13 intent types:

`stop_request` > `goodbye` > `human_handoff` > `wrong_party` > `dispute` > `busy` > `uncomfortable` > `refusal` > `uncertain` > `identity_question` > `affirmation` > `negation` > `unknown`

Each intent has a base confidence score (0.72–0.93). When multiple intents match, confidence is reduced if competing intents are within 0.08 of the primary. Ambiguous yes/no combinations (both `affirmation` and `negation` matched without a stronger intent) are classified as `unknown` with low confidence (0.30), triggering the clarification flow.

The `is_low_confidence_unknown()` function flags utterances below a 0.45 threshold, routing them to the clarification handler (`_handle_low_confidence()` at `agent.py:469-495`). After 1 failed clarification, the call escalates to a human agent with reason `low_confidence`.

## 4. Conversation vs External Logic

The boundary is simple: **conversation handles persuasion; tools handle validation and system actions.**

- **Pure conversation** (no tools needed): greetings, empathy statements, objection handling, reconduction after refusal, silence recovery prompts. These are generated deterministically from the phase and intent.
- **External tool — date normalization** (`tools/date_normalizer.py`): when a debtor proposes a payment date using ambiguous language ("mañana", "el viernes", "a fin de mes"), the `normalize_datetime_local` tool parses it against the current local date and returns a structured result with `needs_confirmation` flag. If ambiguous, the agent confirms before acting. If the resolved date falls outside the current month, the agent rejects it conversationally and reconducts toward a valid alternative.
- **External tools — call control and escalation** (`tools/call_control.py`, `tools/escalation.py`): system actions like `end_call`, `create_promise_to_pay`, `schedule_callback`, `escalate_to_human`, and `mark_do_not_contact` are emitted as action dictionaries in the response. The host system executes them. The agent never performs side effects directly.

This separation means the agent's conversational logic can be tested without any external dependencies (all 44 tests run without a server or telephony), while action execution is the host's responsibility.

## 5. Call Ending and Escalation Rules

**A call ends when:**
- The debtor confirms a promise-to-pay → `ptp_set`
- The debtor says goodbye or requests to stop → `user_ended` / `cease_contact`
- Wrong party is confirmed → `wrong_party`
- 3 consecutive silences with no response → `silence_timeout`
- Global turn limit reached (default 25) → `max_turns`
- Verification fails after 3 attempts → `verification_failed`
- The debtor says it's not a good time → `busy`

**A call escalates to a human when:**
- The debtor explicitly requests a human → `user_requested_human`
- A dispute is raised → `dispute`
- Repeated refusal after 2 negotiation attempts → `hard_refusal` / `multiple_refusals`
- 2 consecutive low-confidence utterances the system cannot parse → `low_confidence`

Both ending and escalation set `phase = "ended"` and are **idempotent**: once ended, any subsequent `handle_turn` call returns `"already_closed"` with an empty actions list, preventing duplicate side effects (`agent.py:57-64`).

## 6. Loop Prevention

The system uses **phase-specific counters** as hard limits:

| Counter | Limit | Consequence |
|---------|-------|-------------|
| `verification_attempts` | 3 | Call ends with `verification_failed` |
| `reconduction_attempts` | 2 | Call ends with `verification_refused` |
| `negotiation_proposals_count` | 2 | Escalation with `multiple_refusals` |
| `silence_count` | 3 | Call ends with `silence_timeout` |
| `clarification_attempts` | 1 (then escalates) | Escalation with `low_confidence` |
| `max_total_turns` | 25 (default) | Global safety net, ends with `max_turns` |

Additionally, `last_assistant_question` is tracked in state to detect when the agent is about to repeat itself. The prompt contracts instruct against asking the same question more than twice. The `_enforce_voice_first()` guard at `agent.py:704-732` hard-limits every response to 2 sentences and 1 question, preventing responses from exceeding voice-first constraints.

## 7. State Preservation

All call state lives in a single `CallState` Pydantic model (`state.py`) with `extra="forbid"` (no undeclared fields). Fields are organized by responsibility:

- **Phase and progress**: `phase`, `turn_count`, `right_party_verified`, `disclosure_delivered`
- **Limit counters**: `verification_attempts`, `silence_count`, `negotiation_proposals_count`, `reconduction_attempts`, `clarification_attempts`
- **Promise-to-pay**: `promise_to_pay.{date, amount, confirmed}` (sub-model `PromiseToPay`)
- **Callback**: `callback.{requested, datetime_local}` (sub-model `Callback`)
- **Situation flags**: `wrong_party_indicated`, `dispute_flag`, `hardship_flag`, `cease_contact_requested`, `escalation_flag`, `escalation_reason`
- **Conversational context**: `last_user_utterance`, `last_assistant_question`, `last_assistant_intent`, `user_sentiment`
- **Closure**: `end_reason`

State is serialized as JSON per call in `runtime/calls/` via `call_store.py`. No raw PII is logged — the state contains only verification pass/fail flags, not the actual ZIP or identity data provided by the user.

## 8. Outbound Job Orchestration

The system includes a complete outbound job lifecycle (`src/api/outbound_orchestration.py`, `src/api/job_store.py`):

- **Trigger sources**: Cron schedules, webhooks, or manual enqueue via `POST /jobs/enqueue`
- **Job state machine**: `queued` → `leased` → `in_progress` → `completed` / `failed` / `dead_letter`
- **Lease model**: Workers lease jobs with a configurable timeout, preventing double-processing
- **Retry with exponential backoff**: Failed jobs requeue with `base_delay * 2^attempt` capped at `max_delay_seconds`
- **Dead-letter**: Jobs exceeding `max_attempts` move to `dead_letter` state for manual review
- **Compliance pre-dial checks** (`src/api/compliance.py`): Call windows (allowed local time ranges), daily attempt caps, minimum gap between attempts, and suppression flags (`dnc`, `cease_contact`, `legal_hold`) are enforced before any call is placed
- **Contact attempt ledger** (`src/api/contact_attempt_store.py`): Every attempt decision (allowed, blocked, reason) is persisted per account for audit

## 9. AI Model Selection

This bot uses a **fully deterministic approach with no LLM** for response generation. This is a deliberate production choice:

| Criterion | Deterministic | LLM-based |
|-----------|--------------|-----------|
| **Inference latency** | 0ms (pure code) | 200-2000ms unpredictable |
| **Production stability** | No external API dependency | Subject to provider uptime |
| **Instruction adherence** | 100% by design | Probabilistic, drift risk |
| **Per-call cost** | $0 | $0.01-0.10+ per turn |
| **Tool capability** | Native Python calls | Function calling via API |
| **Testability** | Deterministic, 100% reproducible | Probabilistic, flaky tests |
| **Hallucination risk** | Zero | Non-zero (can fabricate amounts, terms) |

**Tradeoffs acknowledged**: the deterministic approach requires manual coverage of conversation scenarios and is less flexible with unexpected user phrasing. Intent classification uses keyword and pattern matching rather than semantic understanding, which may miss edge cases.

**If an LLM were introduced**, the recommended approach would be a **small, fast model (8B parameter class, fine-tuned)** for intent classification only, keeping response generation deterministic. This would improve intent coverage without sacrificing response predictability. The model would need to run on dedicated inference infrastructure to meet voice latency SLAs.

**For TTS output**, the system uses ElevenLabs `eleven_turbo_v2_5` with streaming playback via `mpv`, optimized for low-latency voice delivery. In production, a self-hosted TTS model would eliminate the vendor dependency.

**For STT input**, the Streamlit frontend uses browser-native audio capture via `st.audio_input`. In production, Whisper or an equivalent self-hosted model would provide real-time transcription.

## 10. Testing Strategy

The project uses multiple testing layers to validate correctness:

- **44 unit/regression tests** (`tests/`): Intent classifier (8), agent regressions (15), compliance (5), orchestration (5), job/contact stores (5), call metrics (3), NLU report (1). All run in < 0.02s with zero external dependencies.
- **5 scenario replays** (`scripts/run_outbound_demo.py`): Full turn-by-turn conversation execution against predefined JSON scenarios — happy path PTP, dispute escalation, wrong party, silence callback, and date reconduction.
- **3 API smoke tests** (`scripts/smoke_api_demo.py`): Validate state transitions and final outcomes against the REST API (in-process or live HTTP).
- **Compliance smoke tests** (`scripts/smoke_worker_compliance.py`): Verify the outbound worker respects call windows, daily attempt caps, minimum gaps, and suppression flags.
- **NLU analytics** (`scripts/analyze_nlu_report.py`): Confidence reports, intent-vs-action confusion matrix, and outcome statistics across the call history corpus.
