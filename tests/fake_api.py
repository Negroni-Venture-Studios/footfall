"""A stand-in for the Messages API, through the SDK's own client and `httpx2.MockTransport`.

No request leaves the process: the transport answers each one from a list, and keeps it.
"""

import json
from typing import Any

import anthropic
import httpx2

KEY = "sk-ant-test-not-a-key"  # gitleaks:allow


def message(
    content: list[dict[str, Any]],
    *,
    stop: str = "tool_use",
    model: str = "claude-sonnet-5-5",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A Messages API response body."""
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": usage
        or {
            "input_tokens": 1200,
            "output_tokens": 80,
            "cache_creation_input_tokens": 1000,
            "cache_read_input_tokens": 200,
        },
    }


class Api:
    """A Messages API that answers from a list, and keeps every request it is sent."""

    def __init__(self) -> None:
        self.answers: list[tuple[int, dict[str, Any]]] = []
        self.requests: list[httpx2.Request] = []
        self.clients: list[anthropic.AsyncAnthropic] = []

    def answer(self, body: dict[str, Any], status: int = 200) -> None:
        self.answers.append((status, body))

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, body = self.answers.pop(0)
        return httpx2.Response(status, json=body)

    def client(self) -> anthropic.AsyncAnthropic:
        client = anthropic.AsyncAnthropic(
            api_key=KEY,
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(
                transport=httpx2.MockTransport(self._handle)
            ),
        )
        self.clients.append(client)
        return client

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]

    async def close(self) -> None:
        for client in self.clients:
            await client.close()
