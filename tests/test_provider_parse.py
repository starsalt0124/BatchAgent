from __future__ import annotations

import unittest

from batchagent.provider import _parse_chat_response, _parse_stream_response


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

    def test_parse_streaming_content_and_tool_call(self) -> None:
        lines = [
            b'data: {"choices":[{"delta":{"role":"assistant","content":"hello "}}]}\n',
            b'data: {"choices":[{"delta":{"content":"world"}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"submit_artifact","arguments":"{\\"summary\\""}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":":\\"ok\\",\\"metadata\\":{}}"}}]}}]}\n',
            b"data: [DONE]\n",
        ]
        deltas: list[str] = []
        message = _parse_stream_response(lines, deltas.append)
        self.assertEqual(deltas, ["hello ", "world"])
        self.assertEqual(message.content, "hello world")
        self.assertEqual(message.tool_calls[0].name, "submit_artifact")
        self.assertEqual(message.tool_calls[0].arguments["summary"], "ok")


if __name__ == "__main__":
    unittest.main()
