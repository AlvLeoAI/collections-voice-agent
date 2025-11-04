# Testing Guide

## Core Outbound Verification

Run outbound scenario replays:

```bash
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/happy_path_ptp_end_of_month.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/dispute_escalation.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/wrong_party.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/silence_callback.json
python3 scripts/run_outbound_demo.py --scenario scripts/scenarios/date_too_late_reconduct.json
```

## Interactive Demo

```bash
python3 scripts/run_outbound_demo.py
```

- Press Enter on a blank line to simulate silence.
- Type `quit` to exit.

## API + Frontend Smoke Test

In terminal 1:

```bash
python3 src/api/server.py
```

In terminal 2:

```bash
streamlit run frontend/app.py
```

Then:

- Confirm initial outbound greeting appears.
- Confirm browser mic permission is allowed and the "Record your response" widget is visible.
- Send a few user turns and verify `call_state` updates in the sidebar.
- Verify actions are returned on close/escalation paths.

## Queue Worker Smoke Test

1. Start API:

```bash
python3 src/api/server.py
```

2. Enqueue one job:

```bash
curl -s -X POST http://127.0.0.1:8000/jobs/enqueue \
  -H "Content-Type: application/json" \
  -d '{
    "trigger_source":"manual",
    "campaign_id":"cmp_demo",
    "account_ref":"acct_demo_001",
    "party_profile":{"target_name":"Alex Morgan","callback_number":"+1 (555) 010-2000"},
    "account_context_ref":"ctx_demo_001",
    "scheduled_for_utc":"2026-02-08T12:00:00+00:00"
  }'
```

3. Run worker once:

```bash
python3 scripts/run_outbound_worker.py --worker-id worker_local --once
```

4. Validate:
- Job should be `succeeded` with outcome `call_initialized`.
- A new file should exist under `runtime/calls/` with the initial outbound turn.

## Compliance Gate Smoke Test

Enqueue with DNC suppression:

```bash
curl -s -X POST http://127.0.0.1:8000/jobs/enqueue \
  -H "Content-Type: application/json" \
  -d '{
    "trigger_source":"manual",
    "campaign_id":"cmp_demo",
    "account_ref":"acct_demo_dnc",
    "party_profile":{"target_name":"Alex Morgan","callback_number":"+1 (555) 010-2000"},
    "account_context_ref":"ctx_demo_dnc",
    "dnc": true
  }'
```

Run worker once:

```bash
python3 scripts/run_outbound_worker.py --worker-id worker_local --once
```

Validate:
- Job should be `canceled` with `failure_reason=blocked_suppression_dnc`.
- `GET /attempts` should include a `decision_code=blocked_suppression_dnc` row.

## One-command Worker Compliance Smoke

```bash
python3 scripts/smoke_worker_compliance.py
```

Expected:
- `PASS worker compliance smoke`
- One allowed job initializes a call.
- One DNC job is canceled before dialing.

## Expected Behaviors

- No debt/company/account disclosure before successful right-party verification.
- Wrong-party responses close with a non-sensitive outcome.
- Dispute language triggers escalation action.
- End-of-month policy rejects dates outside current month.
- Silence eventually ends the call after limit handling.
