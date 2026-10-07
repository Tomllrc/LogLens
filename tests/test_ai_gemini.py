"""Tests for the Gemini adapter, with SDK objects built locally (no network, no key)."""

from typing import Any

import pytest

pytest.importorskip("google.genai")

import httpx
from google.genai import errors, types

from loglens.ai.gemini import (
    GeminiClient,
    from_response,
    map_api_error,
    to_config,
    to_content,
)
from loglens.ai.llm import (
    LLMRequest,
    Message,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}}


def response_with(*parts: types.Part, usage: dict[str, int] | None = None) -> Any:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(role="model", parts=list(parts)))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(**(usage or {})),
    )


def test_config_for_structured_output() -> None:
    config = to_config(
        LLMRequest.from_prompt("x", system="sys", response_schema=SCHEMA, temperature=0.0)
    )
    assert config.system_instruction == "sys"
    assert config.temperature == 0.0
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == SCHEMA
    assert config.tools is None


def test_config_for_function_calling() -> None:
    tool = ToolSpec("count_events", "Count events", {"type": "object", "properties": {}})
    config = to_config(LLMRequest.from_prompt("x", tools=(tool,)))
    assert config.tools is not None
    declaration = config.tools[0].function_declarations[0]  # type: ignore[union-attr,index]
    assert declaration.name == "count_events"
    assert declaration.parameters_json_schema == {"type": "object", "properties": {}}
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True


def test_contents_for_each_role() -> None:
    user = to_content(Message("user", "hi"))
    assert (user.role, user.parts[0].text) == ("user", "hi")  # type: ignore[index]

    model = to_content(Message("model", "thinking", tool_calls=(ToolCall("f", {"a": 1}),)))
    assert model.role == "model"
    assert model.parts[0].text == "thinking"  # type: ignore[index]
    assert model.parts[1].function_call.name == "f"  # type: ignore[index,union-attr]

    tool = to_content(Message("tool", tool_results=(ToolResult("f", {"count": 3}),)))
    assert tool.parts[0].function_response.response == {"count": 3}  # type: ignore[index,union-attr]


def test_model_turns_are_replayed_verbatim_from_raw() -> None:
    raw = {"role": "model", "parts": [{"text": "a", "thought_signature": "c2ln"}]}
    content = to_content(Message("model", "ignored", raw=raw))
    assert content.parts[0].text == "a"  # type: ignore[index]
    assert content.parts[0].thought_signature == b"sig"  # type: ignore[index]


def test_response_text_json_and_usage() -> None:
    response = response_with(
        types.Part(text="hidden reasoning", thought=True),
        types.Part(text='{"a": 1}'),
        usage={"prompt_token_count": 12, "candidates_token_count": 4, "thoughts_token_count": 6},
    )
    result = from_response(response, LLMRequest.from_prompt("x", response_schema=SCHEMA), "m")
    assert result.text == '{"a": 1}'
    assert result.data == {"a": 1}
    assert result.usage == Usage(12, 10)
    assert result.model == "m"
    assert result.raw is not None and result.raw["role"] == "model"


def test_response_function_calls() -> None:
    response = response_with(
        types.Part(function_call=types.FunctionCall(name="count_events", args={"level": "ERROR"}))
    )
    result = from_response(response, LLMRequest.from_prompt("x"), "m")
    assert result.tool_calls == (ToolCall("count_events", {"level": "ERROR"}),)
    assert result.usage == Usage(0, 0)


def test_empty_or_invalid_responses_are_errors() -> None:
    with pytest.raises(ProviderError, match="empty answer"):
        from_response(
            types.GenerateContentResponse(candidates=[]), LLMRequest.from_prompt("x"), "m"
        )
    with pytest.raises(ProviderError, match="invalid JSON"):
        from_response(
            response_with(types.Part(text="not json")),
            LLMRequest.from_prompt("x", response_schema=SCHEMA),
            "m",
        )


def api_error(code: int, status: str, details: list[dict[str, Any]] | None = None) -> Any:
    body = {"error": {"code": code, "message": "something", "status": status}}
    if details is not None:
        body["error"]["details"] = details  # type: ignore[index]
    return errors.APIError(code, body)


def test_per_minute_rate_limit_is_retryable_with_server_delay() -> None:
    error = map_api_error(
        api_error(
            429,
            "RESOURCE_EXHAUSTED",
            [
                {
                    "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                    "violations": [
                        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}
                    ],
                },
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"},
            ],
        )
    )
    assert isinstance(error, RateLimitError)
    assert error.retry_after == 17.0


def test_daily_quota_is_reported_clearly() -> None:
    error = map_api_error(
        api_error(
            429,
            "RESOURCE_EXHAUSTED",
            [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}],
        )
    )
    assert isinstance(error, QuotaExhaustedError)
    assert "daily quota" in str(error)


@pytest.mark.parametrize("code", [500, 503])
def test_server_errors_are_retryable(code: int) -> None:
    assert isinstance(map_api_error(api_error(code, "UNAVAILABLE")), RateLimitError)


def test_bad_key_and_bad_request() -> None:
    forbidden = map_api_error(api_error(403, "PERMISSION_DENIED"))
    assert isinstance(forbidden, ProviderError)
    assert "GEMINI_API_KEY" in str(forbidden)
    assert isinstance(map_api_error(api_error(400, "INVALID_ARGUMENT")), ProviderError)


def test_client_needs_a_key() -> None:
    with pytest.raises(ProviderError, match="not set"):
        GeminiClient("m")


class _Models:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def client_with(outcome: Any) -> tuple[GeminiClient, _Models]:
    client = GeminiClient("gemini-test", api_key="test-key-not-real")
    models = _Models(outcome)
    client._client = type("C", (), {"models": models})()  # type: ignore[assignment]
    return client, models


def test_complete_sends_contents_and_config() -> None:
    client, models = client_with(response_with(types.Part(text="OK")))
    result = client.complete(LLMRequest.from_prompt("ping", system="sys"))
    assert result.text == "OK"
    assert client.model == "gemini-test"
    assert models.calls[0]["model"] == "gemini-test"
    assert models.calls[0]["config"].system_instruction == "sys"


def test_complete_maps_errors() -> None:
    client, _ = client_with(api_error(429, "RESOURCE_EXHAUSTED"))
    with pytest.raises(RateLimitError):
        client.complete(LLMRequest.from_prompt("ping"))
    client, _ = client_with(httpx.ConnectError("offline"))
    with pytest.raises(RateLimitError, match="network"):
        client.complete(LLMRequest.from_prompt("ping"))
