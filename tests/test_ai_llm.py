"""Tests for the provider-neutral LLM layer: fake client, cache, throttle, retries, usage."""

from pathlib import Path

import pytest

from loglens.ai.llm import (
    AIDisabledError,
    FakeClient,
    LLMRequest,
    LLMResponse,
    Message,
    MissingAPIKeyError,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
    ResilientClient,
    ResponseCache,
    ToolCall,
    ToolSpec,
    Usage,
    UsageTracker,
    complete_json,
    create_client,
    fake_json,
    request_key,
)
from loglens.config import Settings


class FakeTime:
    """A clock that only moves when the code sleeps."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def resilient(inner: FakeClient, time: FakeTime, **kwargs: object) -> ResilientClient:
    return ResilientClient(inner, sleep=time.sleep, clock=time.clock, **kwargs)  # type: ignore[arg-type]


REQUEST = LLMRequest.from_prompt("hello")


# --- FakeClient -------------------------------------------------------------------------------


def test_fake_client_replays_its_script_in_order() -> None:
    fake = FakeClient([LLMResponse(text="one"), LLMResponse(text="two")])
    assert fake.complete(REQUEST).text == "one"
    assert fake.complete(REQUEST).text == "two"
    assert fake.requests == [REQUEST, REQUEST]
    with pytest.raises(AssertionError, match="no scripted response"):
        fake.complete(REQUEST)


def test_fake_client_raises_scripted_errors_and_computes_callables() -> None:
    fake = FakeClient([ProviderError("boom")])
    fake.add(lambda request: LLMResponse(text=request.messages[0].text or ""))
    with pytest.raises(ProviderError):
        fake.complete(REQUEST)
    response = fake.complete(REQUEST)
    assert response.text == "hello"
    assert response.model == "fake-model"


def test_fake_json_carries_data_and_text() -> None:
    response = fake_json({"a": 1})
    assert response.data == {"a": 1}
    assert response.text == '{"a": 1}'
    assert response.usage == Usage(10, 5)


# --- Requests and cache -----------------------------------------------------------------------


def test_request_key_depends_on_model_and_every_field_but_raw() -> None:
    base = request_key("m", REQUEST)
    assert base == request_key("m", LLMRequest.from_prompt("hello"))
    assert base != request_key("other", REQUEST)
    assert base != request_key("m", LLMRequest.from_prompt("hello", system="be brief"))
    assert base != request_key("m", LLMRequest.from_prompt("hello", response_schema={}))
    tool = ToolSpec("count", "Count events", {"type": "object"})
    assert base != request_key("m", LLMRequest.from_prompt("hello", tools=(tool,)))
    with_raw = LLMRequest(messages=(Message("user", "hello", raw={"x": 1}),))
    assert request_key("m", with_raw) == base


def test_response_cache_round_trips(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path / "cache")
    assert cache.get("k") is None
    response = LLMResponse(
        text="t",
        data={"x": [1]},
        tool_calls=(ToolCall("f", {"a": "b"}),),
        usage=Usage(3, 4),
        model="m",
        raw={"role": "model"},
    )
    cache.put("k", response)
    loaded = cache.get("k")
    assert loaded is not None
    assert loaded.cached
    assert (loaded.text, loaded.data, loaded.tool_calls, loaded.usage, loaded.model) == (
        "t",
        {"x": [1]},
        (ToolCall("f", {"a": "b"}),),
        Usage(3, 4),
        "m",
    )
    assert loaded.raw == {"role": "model"}


def test_response_cache_ignores_corrupt_files(tmp_path: Path) -> None:
    (tmp_path / "k.json").write_text("{not json", encoding="utf-8")
    assert ResponseCache(tmp_path).get("k") is None


def test_response_cache_write_failures_are_ignored(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("", encoding="utf-8")
    ResponseCache(blocker / "sub").put("k", LLMResponse(text="t"))  # parent is a file


# --- ResilientClient --------------------------------------------------------------------------


def test_cached_responses_cost_nothing(tmp_path: Path) -> None:
    fake = FakeClient([LLMResponse(text="answer", usage=Usage(7, 3))])
    tracker = UsageTracker()
    client = resilient(fake, FakeTime(), cache=ResponseCache(tmp_path), tracker=tracker)
    first = client.complete(REQUEST)
    second = client.complete(REQUEST)
    assert not first.cached
    assert second.cached and second.text == "answer"
    assert len(fake.requests) == 1
    assert (tracker.calls, tracker.cached_calls, tracker.usage) == (1, 1, Usage(7, 3))
    assert client.model == "fake-model"


def test_calls_are_spaced_by_the_minimum_delay() -> None:
    time = FakeTime()
    fake = FakeClient([LLMResponse(text="a"), LLMResponse(text="b"), LLMResponse(text="c")])
    client = resilient(fake, time, min_delay_seconds=4.0)
    client.complete(REQUEST)
    client.complete(REQUEST)
    time.now += 10
    client.complete(REQUEST)
    assert time.sleeps == [4.0]


def test_rate_limits_are_retried_with_exponential_backoff() -> None:
    time = FakeTime()
    fake = FakeClient(
        [RateLimitError("slow down"), RateLimitError("again"), LLMResponse(text="ok")]
    )
    client = resilient(fake, time, base_backoff_seconds=2.0)
    assert client.complete(REQUEST).text == "ok"
    assert time.sleeps == [2.0, 4.0]


def test_server_retry_delay_wins_when_longer() -> None:
    time = FakeTime()
    fake = FakeClient([RateLimitError("slow down", retry_after=30.0), LLMResponse(text="ok")])
    resilient(fake, time, base_backoff_seconds=2.0).complete(REQUEST)
    assert time.sleeps == [30.0]


def test_retries_give_up_after_the_limit() -> None:
    fake = FakeClient([RateLimitError("busy")] * 3)
    client = resilient(fake, FakeTime(), max_retries=2)
    with pytest.raises(RateLimitError, match="gave up after 2 retries"):
        client.complete(REQUEST)
    assert len(fake.requests) == 3


def test_daily_quota_is_not_retried() -> None:
    fake = FakeClient([QuotaExhaustedError("daily quota")])
    with pytest.raises(QuotaExhaustedError):
        resilient(fake, FakeTime()).complete(REQUEST)
    assert len(fake.requests) == 1


def test_failed_calls_are_not_cached(tmp_path: Path) -> None:
    fake = FakeClient([ProviderError("bad"), LLMResponse(text="ok")])
    client = resilient(fake, FakeTime(), cache=ResponseCache(tmp_path))
    with pytest.raises(ProviderError):
        client.complete(REQUEST)
    assert client.complete(REQUEST).text == "ok"


# --- Usage, JSON, factory ---------------------------------------------------------------------


def test_usage_summary() -> None:
    tracker = UsageTracker()
    tracker.record(LLMResponse(usage=Usage(1000, 20)))
    tracker.record(LLMResponse(usage=Usage(5, 5), cached=True))
    assert tracker.summary("m") == "AI tokens (m): 1,000 input, 20 output (1 call, 1 cached)"


def test_complete_json_parses_text_when_data_is_missing() -> None:
    data, _ = complete_json(FakeClient([LLMResponse(text='{"a": 2}')]), REQUEST)
    assert data == {"a": 2}
    with pytest.raises(ProviderError, match="valid JSON"):
        complete_json(FakeClient([LLMResponse(text="nope")]), REQUEST)


def test_as_message_continues_the_conversation() -> None:
    response = LLMResponse(text="", tool_calls=(ToolCall("f", {}),), raw={"r": 1})
    message = response.as_message()
    assert message.role == "model"
    assert message.text is None
    assert message.tool_calls == (ToolCall("f", {}),)
    assert message.raw == {"r": 1}


def test_create_client_refuses_when_ai_is_off(tmp_path: Path) -> None:
    with pytest.raises(AIDisabledError):
        create_client(Settings(home=tmp_path, ai_enabled=False))


def test_create_client_needs_a_key(tmp_path: Path) -> None:
    with pytest.raises(MissingAPIKeyError, match="GEMINI_API_KEY"):
        create_client(Settings(home=tmp_path))


def test_create_client_wraps_gemini(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("google.genai")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    client = create_client(Settings(home=tmp_path, model="gemini-test", min_delay_seconds=1.5))
    assert isinstance(client, ResilientClient)
    assert client.model == "gemini-test"
    assert client.min_delay_seconds == 1.5
    assert client.cache is not None and client.cache.directory == tmp_path / "cache" / "llm"
