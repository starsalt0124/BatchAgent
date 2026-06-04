from __future__ import annotations

import unittest

from batchagent.provider import _parse_chat_response


class ProviderParseTests(unittest.TestCase):
    def test_parse_tool_call(self) -> None:
        message = _parse_chat_response(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "submit_artifact", "arguments": "{\"summary\":\"ok\",\"metadata\":{}}"},
                                }
                            ],
                        }
                    }
                ]
            }
        )
        self.assertEqual(message.tool_calls[0].name, "submit_artifact")
        self.assertEqual(message.tool_calls[0].arguments["summary"], "ok")


if __name__ == "__main__":
    unittest.main()

