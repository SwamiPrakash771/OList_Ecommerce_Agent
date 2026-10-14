"""Claude caches only what a request marks: the settings every assistant sends must mark the system prompt,
the tool definitions and the conversation. Checked on the request Pydantic AI builds (no network)."""

import asyncio
import json

import pytest

httpx = pytest.importorskip("httpx2")
from anthropic import AsyncAnthropic  # noqa: E402
from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.anthropic import AnthropicModel  # noqa: E402
from pydantic_ai.providers.anthropic import AnthropicProvider  # noqa: E402

from ai.answer_config import PROMPT_CACHE_SETTINGS  # noqa: E402


def claude_request(settings: dict) -> dict:
    """The body of the first request a small agent sends to Claude with these settings."""
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(500, json={"type": "error", "error": {"type": "api_error", "message": "stop"}})

    client = AsyncAnthropic(api_key="test", max_retries=0,
                            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    agent = Agent(AnthropicModel("claude-haiku-4-5", provider=AnthropicProvider(anthropic_client=client)),
                  instructions="You are the store assistant.")

    @agent.tool_plain
    def find_products(query: str) -> str:
        """Search products."""
        return "[]"

    @agent.tool_plain
    def update_cart(product_id: str) -> str:
        """Change the cart."""
        return "{}"

    with pytest.raises(Exception):
        asyncio.run(agent.run("Add the costliest watch to my cart", model_settings=settings))
    return sent[0]


def test_claude_requests_mark_prompt_tools_and_conversation_for_caching():
    body = claude_request(PROMPT_CACHE_SETTINGS)
    assert any("cache_control" in block for block in body["system"])          # the system prompt
    assert "cache_control" in body["tools"][-1]                               # every tool definition up to the last
    assert body["cache_control"]["type"] == "ephemeral"                       # the conversation so far


def test_without_the_settings_claude_caches_nothing():
    body = claude_request({})
    assert "cache_control" not in body and not any("cache_control" in t for t in body["tools"])
