from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.api.call_store import JsonCallStore
from src.api.storage_utils import InvalidIdError
from src.outbound_voice_agent import CallState
from src.outbound_voice_agent.agent import TurnEvent


class CallStoreTests(unittest.TestCase):
    def test_pii_redaction_and_account_ref_and_atomic_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonCallStore(root_dir=tmpdir)
            populated_state = CallState(last_user_utterance="My SSN is 123-45-6789")

            store.create_call(
                call_id="call_safe_001",
                assistant_intent="request_target",
                call_state=populated_state,
                account_ref="acct_pii_1",
            )

            path = Path(tmpdir) / "call_safe_001.json"
            self.assertTrue(path.exists())
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["account_ref"], "acct_pii_1")
            self.assertEqual(on_disk["last_call_state"]["last_user_utterance"], "")

            event = TurnEvent(
                event_type="user_utterance",
                transcript="78701 is my ZIP code",
                timestamp_utc="2026-02-08T16:00:00+00:00",
                current_local_date="2026-02-08",
                current_local_time="10:00",
                timezone="America/Chicago",
                language="en-US",
            )
            populated_turn_state = CallState(last_user_utterance="78701 is my ZIP code")
            store.append_turn(
                call_id="call_safe_001",
                turn_event=event,
                assistant_intent="verify_identity",
                actions=[],
                call_state=populated_turn_state,
            )

            on_disk_after = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk_after["last_call_state"]["last_user_utterance"], "")

            summary = store.summarize_call("call_safe_001")
            self.assertEqual(summary["account_ref"], "acct_pii_1")
            self.assertEqual(summary["last_call_state"]["last_user_utterance"], "")

            tmp_files = list(Path(tmpdir).glob("*.tmp"))
            self.assertEqual(tmp_files, [])

    def test_rejects_path_traversal_call_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = JsonCallStore(root_dir=tmpdir)
            for bad_id in ("../evil", "../../etc/passwd", "bad/id", "bad.id", ""):
                with self.subTest(bad_id=bad_id):
                    with self.assertRaises(InvalidIdError):
                        store.create_call(
                            call_id=bad_id,
                            assistant_intent="request_target",
                            call_state=CallState(),
                        )
                    with self.assertRaises(InvalidIdError):
                        store.get_call(bad_id)


if __name__ == "__main__":
    unittest.main()
