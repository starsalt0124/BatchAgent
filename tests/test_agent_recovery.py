from __future__ import annotations

import unittest

from batchagent.agent import _assistant_message_for_wire


class AgentRecoveryTests(unittest.TestCase):
    def test_assistant_wire_message_preserves_reasoning_content(self) -> None:
        raw = {
            "role": "assistant",
            "content": "",
            "reasoning_content": "tool reasoning",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        }

        message = _assistant_message_for_wire(raw)

        self.assertEqual(message["reasoning_content"], "tool reasoning")
        self.assertEqual(message["tool_calls"], raw["tool_calls"])

    def test_reasoning_only_assistant_message_is_not_sent_back(self) -> None:
        raw = {
            "role": "assistant",
            "content": None,
            "reasoning_content": "thinking but no final content",
        }

        message = _assistant_message_for_wire(raw)

        self.assertIsNone(message)


if __name__ == "__main__":
    unittest.main()
