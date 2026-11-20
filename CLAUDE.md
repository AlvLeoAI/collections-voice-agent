# Outbound Collections Voice Agent — Claude Code Entry Point

@AGENTS.md

## Hard Rules
- **Zero LLMs in the core decision path:** Keep `src/outbound_voice_agent/` and `src/api/` 100% deterministic Python with no AI SDK imports.
- **No OpenAI:** Never re-introduce `openai` or `OPENAI_API_KEY`. Optional voice STT uses lazy-loaded `google-genai` (`gemini-3.5-transcribe`, `store=False`) in `src/voice_handler.py` only.
- **No live paid calls or credentials:** Never read, print, or commit `.env` or real API keys; keep all tests and smoke checks 100% offline (`uv run pytest -v` blocks outbound sockets via `tests/conftest.py`).
- **Pre-verification & PII safety:** Never leak debt/creditor info before ZIP verification, never store raw transcripts in `last_user_utterance`, and never log raw transcripts/assistant text.
- **Do not commit, push, or deploy to Cloud Run without explicit user approval.**
