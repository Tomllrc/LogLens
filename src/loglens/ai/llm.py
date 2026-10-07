"""Provider-neutral LLM interface, plus the free-tier protections around any provider.

Business logic depends only on :class:`LLMClient` and the plain data types here, so
another provider can be added by implementing ``complete``. Implementations:

- :class:`loglens.ai.gemini.GeminiClient`: Google Gemini through ``google-genai``.
- :class:`FakeClient`: scripted, deterministic responses for tests (CI never calls an API).

:func:`create_client` wraps the real client in :class:`ResilientClient`, which adds what
the free tier needs: a response cache keyed by a hash of the request (re-running a command
costs no quota), a minimum delay between calls, exponential backoff on rate limits, a
clear error when the daily quota is exhausted, and token accounting
(:class:`UsageTracker`).
"""

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from loglens.config import API_KEY_VARIABLE, Settings, api_key_is_set

PRIVACY_WARNING: Final = (
    "Privacy: on Google's free tier, prompts and responses may be used to improve Google's "
    "products. Only send logs you are allowed to share (use --no-ai to disable AI)."
)
CACHE_VERSION: Final = 1


# --- Errors -----------------------------------------------------------------------------


class AIError(RuntimeError):
    """Base class of LLM failures; the message is meant for the user."""


class AIDisabledError(AIError):
    """AI is turned off (``--no-ai``, ``LOGLENS_AI=0`` or ``ai: false`` in config.yaml)."""


class MissingAPIKeyError(AIError):
    """No API key in the environment."""


class RateLimitError(AIError):
    """A transient limit (per-minute quota, overload): worth retrying after a delay."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExhaustedError(AIError):
    """The daily quota is used up: retrying today will not help."""


class ProviderError(AIError):
    """Any other provider failure (invalid request, bad key, network...)."""


# --- Data types ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """Tokens consumed by calls (output includes the model's thinking tokens)."""

    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True)
class ToolCall:
    """A function the model asked to call."""

    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolResult:
    """The result of a tool call, sent back to the model."""

    name: str
    content: dict[str, Any]


@dataclass(frozen=True)
class ToolSpec:
    """A function the model may call: name, description, JSON Schema of its arguments."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class Message:
    """One turn of a conversation.

    ``raw`` is the provider's own serialized form of a model turn (JSON-able); providers
    that need to see their turn back verbatim (Gemini's thought signatures) use it.
    """

    role: Literal["user", "model", "tool"]
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    raw: dict[str, Any] | None = field(default=None, compare=False)


@dataclass(frozen=True)
class LLMRequest:
    """Everything that determines a response (and therefore its cache key)."""

    messages: tuple[Message, ...]
    system: str | None = None
    response_schema: dict[str, Any] | None = None
    tools: tuple[ToolSpec, ...] = ()
    temperature: float | None = None

    @classmethod
    def from_prompt(cls, prompt: str, **kwargs: Any) -> "LLMRequest":
        """A single-turn request."""
        return cls(messages=(Message("user", prompt),), **kwargs)


@dataclass(frozen=True)
class LLMResponse:
    """A model answer: text, parsed JSON (with a response schema), tool calls, usage."""

    text: str = ""
    data: Any = None
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = Usage()
    model: str = ""
    cached: bool = False
    raw: dict[str, Any] | None = field(default=None, compare=False)

    def as_message(self) -> Message:
        """This answer as a model turn, to continue the conversation."""
        return Message("model", self.text or None, self.tool_calls, raw=self.raw)


class LLMClient(Protocol):
    """What business logic needs from an LLM provider."""

    @property
    def model(self) -> str:
        """The model answering requests."""
        ...

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Answer a request (one model turn)."""
        ...


# --- Usage ----------------------------------------------------------------------------------


@dataclass
class UsageTracker:
    """Token usage of a command, printed at its end, with any model switch."""

    calls: int = 0
    cached_calls: int = 0
    usage: Usage = Usage()
    models: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def record(self, response: LLMResponse) -> None:
        """Count a response (cached ones cost nothing)."""
        if response.model and response.model not in self.models:
            self.models.append(response.model)
        if response.cached:
            self.cached_calls += 1
        else:
            self.calls += 1
            self.usage = self.usage + response.usage

    def summary(self, model: str) -> str:
        """``AI tokens (model): 1,234 input, 567 output (3 calls, 1 cached)``, then notes."""
        line = (
            f"AI tokens ({', '.join(self.models) or model}): {self.usage.input_tokens:,} input, "
            f"{self.usage.output_tokens:,} output "
            f"({self.calls} call{'s' if self.calls != 1 else ''}, {self.cached_calls} cached)"
        )
        return "\n".join([line, *self.notes])


# --- Cache ----------------------------------------------------------------------------------


def request_key(model: str, request: LLMRequest) -> str:
    """A stable hash of the model and everything in the request."""
    payload = {"v": CACHE_VERSION, "model": model, "request": _jsonable(asdict(request))}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items() if k != "raw"}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


class ResponseCache:
    """Responses on disk, one JSON file per request hash."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def get(self, key: str) -> LLMResponse | None:
        """The cached response for ``key``, marked as cached, or None."""
        path = self.directory / f"{key}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return LLMResponse(
            text=data.get("text", ""),
            data=data.get("data"),
            tool_calls=tuple(ToolCall(c["name"], c["args"]) for c in data.get("tool_calls", [])),
            usage=Usage(**data.get("usage", {})),
            model=data.get("model", ""),
            cached=True,
            raw=data.get("raw"),
        )

    def put(self, key: str, response: LLMResponse) -> None:
        """Store a response (failures to write are ignored: the cache is an optimization)."""
        payload = {
            "text": response.text,
            "data": response.data,
            "tool_calls": [asdict(call) for call in response.tool_calls],
            "usage": asdict(response.usage),
            "model": response.model,
            "raw": response.raw,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{key}.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except (OSError, TypeError, ValueError):
            pass


# --- Resilience -------------------------------------------------------------------------------


class ResilientClient:
    """Cache, throttle, retry and fall back around any :class:`LLMClient`; count tokens.

    ``fallbacks`` are tried in order when the active client's daily quota is exhausted
    or it stays rate limited after every retry (free-tier quotas are per model).
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        fallbacks: Sequence[LLMClient] = (),
        cache: ResponseCache | None = None,
        min_delay_seconds: float = 0.0,
        max_retries: int = 3,
        base_backoff_seconds: float = 2.0,
        tracker: UsageTracker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.fallbacks = tuple(fallbacks)
        self._clients = (inner, *self.fallbacks)
        self._active = 0
        self.cache = cache
        self.min_delay_seconds = min_delay_seconds
        self.max_retries = max_retries
        self.base_backoff_seconds = base_backoff_seconds
        self.tracker = tracker if tracker is not None else UsageTracker()
        self._sleep = sleep
        self._clock = clock
        self._last_call: float | None = None

    @property
    def model(self) -> str:
        """The model currently answering (changes after a fallback)."""
        return self._clients[self._active].model

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Cached answer if any; else a throttled, retried call, falling back if needed."""
        while True:
            client = self._clients[self._active]
            try:
                return self._complete_with(client, request)
            except (QuotaExhaustedError, RateLimitError) as error:
                if self._active + 1 >= len(self._clients):
                    raise
                self._active += 1
                if isinstance(error, QuotaExhaustedError):
                    reason = "daily quota exhausted"
                else:
                    reason = "still unavailable after retries"
                self.tracker.notes.append(f"{client.model}: {reason}; switched to {self.model}")

    def _complete_with(self, client: LLMClient, request: LLMRequest) -> LLMResponse:
        key = request_key(client.model, request)
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.tracker.record(cached)
                return cached
        attempt = 0
        while True:
            self._throttle()
            try:
                response = client.complete(request)
            except RateLimitError as error:
                if attempt >= self.max_retries:
                    raise RateLimitError(
                        f"{error} (gave up after {self.max_retries} retries)", error.retry_after
                    ) from error
                backoff = self.base_backoff_seconds * 2**attempt
                self._sleep(max(backoff, error.retry_after or 0.0))
                attempt += 1
                continue
            break
        if self.cache is not None:
            self.cache.put(key, response)
        self.tracker.record(response)
        return response

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            wait = self.min_delay_seconds - (now - self._last_call)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_call = now


# --- Fake client for tests ----------------------------------------------------------------------

Scripted = LLMResponse | Exception | Callable[[LLMRequest], LLMResponse]


class FakeClient:
    """Deterministic client: returns scripted responses (or raises scripted errors) in order.

    Every request is recorded in ``requests``. A callable script entry computes the
    response from the request. Running out of script raises ``AssertionError``.
    """

    def __init__(self, script: Iterable[Scripted] = (), model: str = "fake-model") -> None:
        self._script = list(script)
        self._model = model
        self.requests: list[LLMRequest] = []

    @property
    def model(self) -> str:
        return self._model

    def add(self, *entries: Scripted) -> None:
        """Append to the script."""
        self._script.extend(entries)

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeClient: no scripted response left")
        entry = self._script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        response = entry(request) if callable(entry) else entry
        return replace(response, model=response.model or self._model)


FAKE_USAGE: Final = Usage(10, 5)


def fake_json(data: Any, usage: Usage = FAKE_USAGE) -> LLMResponse:
    """A scripted structured-output response."""
    return LLMResponse(text=json.dumps(data), data=data, usage=usage)


# --- Factory -------------------------------------------------------------------------------------


def create_client(settings: Settings, *, tracker: UsageTracker | None = None) -> ResilientClient:
    """The configured provider (Gemini), wrapped with cache, throttling and retries.

    Raises :class:`AIDisabledError` when AI is off and :class:`MissingAPIKeyError` when
    ``GEMINI_API_KEY`` is not set.
    """
    if not settings.ai_enabled:
        raise AIDisabledError(
            "AI is disabled (--no-ai, LOGLENS_AI=0 or `ai: false` in config.yaml)."
        )
    if not api_key_is_set():
        raise MissingAPIKeyError(
            f"No API key: set {API_KEY_VARIABLE} in a .env file (see .env.example); "
            "a free key is available at https://aistudio.google.com/apikey"
        )
    from loglens.ai.gemini import GeminiClient  # optional dependency, imported on use

    fallbacks = [GeminiClient(m) for m in settings.fallback_models if m != settings.model]
    return ResilientClient(
        GeminiClient(settings.model),
        fallbacks=fallbacks,
        cache=ResponseCache(settings.cache_dir),
        min_delay_seconds=settings.min_delay_seconds,
        max_retries=settings.max_retries,
        tracker=tracker,
    )


def complete_json(client: LLMClient, request: LLMRequest) -> tuple[Any, LLMResponse]:
    """Call with a response schema and return the parsed JSON (raises AIError if invalid)."""
    response = client.complete(request)
    data = response.data
    if data is None:
        try:
            data = json.loads(response.text)
        except ValueError as error:
            raise ProviderError(f"the model did not return valid JSON: {error}") from error
    return data, response


__all__: Sequence[str] = [
    "PRIVACY_WARNING",
    "AIDisabledError",
    "AIError",
    "FakeClient",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "MissingAPIKeyError",
    "ProviderError",
    "QuotaExhaustedError",
    "RateLimitError",
    "ResilientClient",
    "ResponseCache",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "UsageTracker",
    "complete_json",
    "create_client",
    "fake_json",
    "request_key",
]
