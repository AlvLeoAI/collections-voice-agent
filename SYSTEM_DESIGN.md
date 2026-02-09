# Design Decisions: Outbound Collections Voice Agent

## 1. Architecture: Single Agent, Phase-Based State Machine

The system uses a **single deterministic agent** rather than multiple specialized agents or an LLM-driven approach. A single agent avoids inter-agent handoff complexity, eliminates serialization overhead between components, and keeps the entire call flow auditable in one code path. For a compliance-critical domain like debt collection, this traceability matters more than architectural elegance.

The agent operates as a **phase-based state machine** with five phases: `pre_verification` → `verification` → `post_verification` (negotiation) → `closing/escalation` → `ended`. Each phase gates what the agent can say and do. Phase transitions are driven by **scored intent classification**: the `classify_utterance` function returns ranked intents with confidence scores, and the agent routes to the appropriate handler based on the current phase and top intent. For example, an `affirmation` intent during `pre_verification` transitions to `verification`, while the same intent during `post_verification` may confirm a promise-to-pay. Universal intents (`stop_request`, `goodbye`, `human_handoff`) are checked before phase routing and override any phase-specific logic.

If responsibilities were split across agents (e.g., a verifier agent and a negotiator agent), each would need its own state management, and handoff errors could leak sensitive data across phase boundaries. The single-agent design avoids this by construction.

## 2. Verification Sufficiency

Right-party verification requires the caller to confirm their **5-digit ZIP code** against the value stored in `account_context.expected_zip`. This is a **hard code gate**, not a prompt instruction: the function `_deliver_disclosure_and_start_negotiation` is only reachable through the code path where `provided_zip == expected_zip` evaluates to `True` at `agent.py:210`. Until that condition passes, `right_party_verified` remains `False`, `phase` stays in `verification`, and no debt amount, creditor name, or account details appear in any response.

This matters because prompt-level instructions can be bypassed through adversarial input or model drift. A structural gate cannot. The agent allows up to 3 verification attempts (`verification_attempts` counter); on failure, the call ends with reason `verification_failed`.

## 3. Conversation vs External Logic

The boundary is simple: **conversation handles persuasion; tools handle validation and system actions.**

- **Pure conversation** (no tools needed): greetings, empathy statements, objection handling, reconduction after refusal, silence recovery prompts. These are generated deterministically from the phase and intent.
- **External tool — date normalization** (`date_normalizer.py`): when a debtor proposes a payment date using ambiguous language ("mañana", "el viernes", "a fin de mes"), the `normalize_datetime_local` tool parses it against the current local date and returns a structured result with `needs_confirmation` flag. If ambiguous, the agent confirms before acting. If the resolved date falls outside the current month, the agent rejects it conversationally and reconducts.
- **External tools — call control and escalation** (`call_control.py`, `escalation.py`): system actions like `end_call`, `create_promise_to_pay`, `schedule_callback`, `escalate_to_human`, and `mark_do_not_contact` are emitted as action dictionaries in the response. The host system executes them. The agent never performs side effects directly.

This separation means the agent's conversational logic can be tested without any external dependencies, while action execution is the host's responsibility.

## 4. Call Ending and Escalation Rules

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

Both ending and escalation set `phase = "ended"` and are **idempotent**: once ended, any subsequent `handle_turn` call returns `"already_closed"` with an empty actions list, preventing duplicate side effects.

## 5. Loop Prevention

The system uses **phase-specific counters** as hard limits:
- `verification_attempts`: max 3 before call ends
- `reconduction_attempts`: max 2 callback offers before closing
- `negotiation_proposals_count`: max 2 before escalation
- `silence_count`: max 3 consecutive before timeout
- `clarification_attempts`: max 1 before escalation on low-confidence input
- `max_total_turns` (default 25): global safety net across all phases

Additionally, `last_assistant_question` is tracked in state to detect when the agent is about to repeat itself, and the prompt contracts instruct against asking the same question more than twice. Systematic repeated-question detection at the code level is a backlog item.

## 6. State Preservation

All call state lives in a single `CallState` Pydantic model with `extra="forbid"` (no undeclared fields). It tracks: current phase, verification status, turn counters, negotiation progress (`promise_to_pay.date`, `promise_to_pay.amount`, `promise_to_pay.confirmed`), callback state, escalation flags and reasons, user sentiment, and the last assistant question/intent for context continuity.

State is serialized as JSON per call in `runtime/calls/` via `call_store.py`. No raw PII is logged — the state contains only verification pass/fail flags, not the actual ZIP or identity data provided by the user.

## 7. AI Model Selection

This bot uses a **fully deterministic approach with no LLM** for response generation. This is a deliberate production choice:

- **Zero latency variance**: voice calls require sub-200ms response decisions. Deterministic code runs in single-digit milliseconds, while LLM inference introduces 200-2000ms of unpredictable latency.
- **100% instruction adherence**: in a regulated domain (FDCPA, TCPA compliance), the bot must never deviate from its script. Deterministic code cannot hallucinate a disclosure or skip a verification step.
- **Zero hallucination risk**: the bot will never fabricate debt amounts, invent payment terms, or disclose information from the wrong account.
- **No API costs or vendor dependency**: no per-call inference costs, no uptime dependency on external model providers.
- **Fully testable and auditable**: every code path can be unit-tested with deterministic scenario replay.

**Tradeoffs acknowledged**: the deterministic approach requires manual coverage of conversation scenarios and is less flexible with unexpected user phrasing. Intent classification uses keyword and pattern matching rather than semantic understanding, which may miss edge cases.

**If an LLM were introduced**, the recommended approach would be a **small, fast model (8B parameter class, fine-tuned)** for intent classification only, keeping response generation deterministic. This would improve intent coverage without sacrificing response predictability. The model would need to run on dedicated inference infrastructure to meet voice latency SLAs.

**For TTS output**, the system uses ElevenLabs `eleven_turbo_v2_5` with streaming playback via `mpv`, optimized for low-latency voice delivery. In production, a self-hosted TTS model would eliminate the vendor dependency.
