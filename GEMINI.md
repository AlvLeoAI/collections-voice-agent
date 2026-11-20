# Outbound Collections Voice Agent — Gemini Entry Point

@AGENTS.md

## Hard Rules
- **Zero LLMs in the core decision path:** Keep `src/outbound_voice_agent/` and `src/api/` 100% deterministic Python with no AI SDK imports.
- **Optional Gemini STT only:** Use `google-genai` (`>=3.2.0`) strictly as a lazy-loaded STT adapter (`client.interactions.create`, default `gemini-3.5-transcribe`, `store=False`) inside `src/voice_handler.py`. Never re-introduce `openai` or `OPENAI_API_KEY`.
- **No live paid calls or credentials:** Never read, print, or commit `.env` or real API keys; keep all automated verification 100% offline (`uv run pytest -v` blocks outbound sockets via `tests/conftest.py`).
- **Pre-verification & PII safety:** Never leak debt/creditor info before ZIP verification, never store raw transcripts in `last_user_utterance`, and never log raw transcripts/assistant text.
- **Do not commit, push, or deploy/delete Cloud Run services without explicit user approval.**
