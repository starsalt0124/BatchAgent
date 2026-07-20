from __future__ import annotations

import unittest

from batchagent.models import BatchConfig
from batchagent.provider import _parse_chat_response, _parse_stream_response
from batchagent.provider import create_provider


class ProviderParseTests(unittest.TestCase):
    def test_accepts_opencodego_provider_alias(self) -> None:
        provider = create_provider(BatchConfig(provider="opencodego", base_url="https://opencode.ai/zen/go/v1"))
        self.assertEqual(provider.config.provider, "opencodego")

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

    def test_preserves_non_stream_usage_for_persistence(self) -> None:
        message = _parse_chat_response(
            {
                "id": "response-1",
                "model": "model-a",
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
            }
        )
        metadata = message.raw["_bagent_response"]
        self.assertEqual(metadata["id"], "response-1")
        self.assertEqual(metadata["model"], "model-a")
        self.assertEqual(metadata["usage"]["total_tokens"], 18)

    def test_parse_streaming_content_and_tool_call(self) -> None:
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"thinking "}}]}\n',
            b'data: {"choices":[{"delta":{"reasoning_content":"done"}}]}\n',
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
        self.assertEqual(message.raw["reasoning_content"], "thinking done")
        self.assertEqual(message.tool_calls[0].name, "submit_artifact")
        self.assertEqual(message.tool_calls[0].arguments["summary"], "ok")

    def test_parse_stream_usage_only_chunk(self) -> None:
        lines = [
            b'data: {"id":"response-2","model":"model-b","choices":[{"delta":{"content":"ok"}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
            b"data: [DONE]\n",
        ]
        message = _parse_stream_response(lines, lambda _delta: None)
        metadata = message.raw["_bagent_response"]
        self.assertEqual(metadata["id"], "response-2")
        self.assertEqual(metadata["model"], "model-b")
        self.assertEqual(metadata["usage"]["total_tokens"], 7)


if __name__ == "__main__":
    unittest.main()
