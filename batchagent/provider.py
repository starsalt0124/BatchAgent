from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .models import BatchConfig, ChatMessage, ToolCall


class ProviderError(RuntimeError):
    pass


SUPPORTED_PROVIDERS = {"deepseek", "openai-compatible", "opencodego"}


@dataclass
class OpenAICompatibleProvider:
    config: BatchConfig

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        delta_callback: Callable[[str], None] | None = None,
    ) -> ChatMessage:
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise ProviderError(f"missing API key environment variable: {self.config.api_key_env}")

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": delta_callback is not None,
            "temperature": self.config.temperature,
        }
        if delta_callback is not None:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self.config.max_tokens is not None:
            payload["max_tokens"] = self.config.max_tokens
        if self.config.reasoning_effort:
            payload["reasoning_effort"] = self.config.reasoning_effort
        if self.config.thinking:
            payload["thinking"] = {"type": self.config.thinking}

        url = self.config.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "bagent/0.2",
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_error: Exception | None = None
        for attempt in range(self.config.provider_retries + 1):
            try:
                request = urllib.request.Request(url, data=data, headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                    if delta_callback is not None:
                        return _parse_stream_response(response, delta_callback)
                    body = response.read().decode("utf-8")
                    return _parse_chat_response(json.loads(body))
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                last_error = ProviderError(f"provider HTTP {exc.code}: {body}")
                if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                    break
            except (urllib.error.URLError, TimeoutError, OSError, ConnectionError, json.JSONDecodeError) as exc:
                last_error = exc

            if attempt < self.config.provider_retries:
                time.sleep(min(2**attempt, 8))

        raise ProviderError(str(last_error) if last_error else "provider request failed")

    def list_models(self) -> dict[str, Any]:
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise ProviderError(f"missing API key environment variable: {self.config.api_key_env}")
        url = self.config.base_url.rstrip("/") + "/models"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "bagent/0.2",
        }
        request = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))


def create_provider(config: BatchConfig) -> OpenAICompatibleProvider:
    if config.provider not in SUPPORTED_PROVIDERS:
        raise ProviderError(f"unsupported provider: {config.provider}")
    return OpenAICompatibleProvider(config)


def _parse_chat_response(payload: dict[str, Any]) -> ChatMessage:
    choices = payload.get("choices") or []
    if not choices:
        raise ProviderError(f"provider response has no choices: {payload}")
    message = choices[0].get("message") or {}
    tool_calls: list[ToolCall] = []
    for item in message.get("tool_calls") or []:
        function = item.get("function") or {}
        args_raw = function.get("arguments") or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except json.JSONDecodeError:
            args = {"_invalid_json": args_raw}
        if not isinstance(args, dict):
            args = {"value": args}
        tool_calls.append(
            ToolCall(
                id=str(item.get("id") or ""),
                name=str(function.get("name") or ""),
                arguments=args,
            )
        )
    raw_message = dict(message)
    response_metadata = {
        key: payload[key]
        for key in ("id", "model", "created", "system_fingerprint")
        if payload.get(key) is not None
    }
    usage = payload.get("usage")
    if isinstance(usage, dict):
        response_metadata["usage"] = usage
    if response_metadata:
        # Provider response metadata is kept out of the assistant wire message,
        # but remains available to the persistence layer for durable statistics.
        raw_message["_bagent_response"] = response_metadata
    return ChatMessage(
        role=str(message.get("role") or "assistant"),
        content=message.get("content"),
        tool_calls=tool_calls,
        raw=raw_message,
    )


def _parse_stream_response(response, delta_callback: Callable[[str], None]) -> ChatMessage:
    content_parts: list[str] = []
    reasoning_content_parts: list[str] = []
    tool_calls_by_index: dict[int, dict[str, Any]] = {}
    role = "assistant"
    response_metadata: dict[str, Any] = {}
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        for key in ("id", "model", "created", "system_fingerprint"):
            if payload.get(key) is not None:
                response_metadata[key] = payload[key]
        if isinstance(payload.get("usage"), dict):
            response_metadata["usage"] = payload["usage"]
        choices = payload.get("choices") or []
        if not choices:
            continue
        delta = choices[0].get("delta") or {}
        role = str(delta.get("role") or role)
        reasoning_content = delta.get("reasoning_content")
        if reasoning_content:
            reasoning_content_parts.append(str(reasoning_content))
        content = delta.get("content")
        if content:
            content_parts.append(str(content))
            delta_callback(str(content))
        for item in delta.get("tool_calls") or []:
            index = int(item.get("index", len(tool_calls_by_index)))
            current = tool_calls_by_index.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            if item.get("id"):
                current["id"] = item["id"]
            if item.get("type"):
                current["type"] = item["type"]
            function = item.get("function") or {}
            current_function = current.setdefault("function", {"name": "", "arguments": ""})
            if function.get("name"):
                current_function["name"] = str(current_function.get("name") or "") + str(function["name"])
            if function.get("arguments"):
                current_function["arguments"] = str(current_function.get("arguments") or "") + str(function["arguments"])

    tool_calls = [tool_calls_by_index[index] for index in sorted(tool_calls_by_index)]
    message = {
        "role": role,
        "content": "".join(content_parts) if content_parts else None,
    }
    if reasoning_content_parts:
        message["reasoning_content"] = "".join(reasoning_content_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    parsed_payload: dict[str, Any] = {"choices": [{"message": message}]}
    parsed_payload.update(response_metadata)
    return _parse_chat_response(parsed_payload)
