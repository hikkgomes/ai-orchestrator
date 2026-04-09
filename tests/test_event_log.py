from __future__ import annotations

import json

from ai_orchestrator.event_log import EventLogger


class TestEventLogger:
    def test_log_writes_timestamped_jsonl_record(self, artifact_root):
        logger = EventLogger(artifact_root, "run123")
        logger.log("state_transition", from_state="INIT", to_state="PLANNING")

        lines = (artifact_root / "logs" / "run-run123.log").read_text().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "state_transition"
        assert record["from_state"] == "INIT"
        assert record["to_state"] == "PLANNING"
        assert "timestamp" in record
