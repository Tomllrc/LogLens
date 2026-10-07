"""Google Gemini through the official ``google-genai`` SDK (install ``loglens[ai]``).

Provider: Gemini via google-genai. The API key comes only from ``GEMINI_API_KEY``. JSON
answers use structured output (``response_json_schema``); tools use function calling with
automatic calling disabled, so the caller runs tools itself. Model turns are kept as the
SDK's serialized ``Content`` so that Gemini's thought signatures survive multi-turn tool
conversations (and the response cache). SDK retries are off: :class:`ResilientClient`
retries, with backoff that honors the server's ``retryDelay``.
"""

import json
import os
import re
from typing import Any, Final

from loglens.ai.llm import (
    LLMRequest,
    LLMResponse,
    Message,
    ProviderError,
    QuotaExhaustedError,
    RateLimitError,
    ToolCall,
    Usage,
)
from loglens.config import API_KEY_VARIABLE

try:
    import httpx
    from google import genai
    from google.genai import errors, types
except ImportError as error:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "Gemini support needs the optional dependency: pip install 'loglens[ai]'"
    ) from error

TIMEOUT_MS: Final = 120_000
OVERLOAD_RETRY_SECONDS: Final = 10.0
"""Minimum wait after a 5xx: retrying an overloaded model at once only spends quota."""
_RETRY_DELAY: Final = re.compile(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"')


class GeminiClient:
    """:class:`loglens.ai.llm.LLMClient` for Gemini."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        key = api_key or os.environ.get(API_KEY_VARIABLE, "").strip()
        if not key:
            raise ProviderError(f"{API_KEY_VARIABLE} is not set")
        self._model = model
        self._client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=TIMEOUT_MS, retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )

    @property
    def model(self) -> str:
        return self._model

    def complete(self, request: LLMRequest) -> LLMResponse:
        """One generate_content call, with provider errors mapped to LogLens errors."""
        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=[to_content(message) for message in request.messages],
                config=to_config(request),
            )
        except errors.APIError as error:
            raise map_api_error(error) from error
        except httpx.TransportError as error:
            raise RateLimitError(f"network error talking to Gemini: {error}") from error
        return from_response(response, request, self._model)


def to_config(request: LLMRequest) -> types.GenerateContentConfig:
    """The SDK config for a request (system prompt, JSON schema, tools)."""
    config: dict[str, Any] = {}
    if request.system:
        config["system_instruction"] = request.system
    if request.temperature is not None:
        config["temperature"] = request.temperature
    if request.response_schema is not None:
        config["response_mime_type"] = "application/json"
        config["response_json_schema"] = request.response_schema
    if request.tools:
        config["tools"] = [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=tool.name,
                        description=tool.description,
                        parameters_json_schema=tool.parameters,
                    )
                    for tool in request.tools
                ]
            )
        ]
    # LogLens runs tools itself; disabled always (the SDK warns when it is left on).
    config["automatic_function_calling"] = types.AutomaticFunctionCallingConfig(disable=True)
    return types.GenerateContentConfig(**config)


def to_content(message: Message) -> types.Content:
    """A conversation turn in the SDK's form (model turns replayed verbatim if possible)."""
    if message.role == "model":
        if message.raw is not None:
            return types.Content.model_validate(message.raw)
        parts = [types.Part.from_text(text=message.text)] if message.text else []
        parts += [
            types.Part(function_call=types.FunctionCall(name=call.name, args=call.args))
            for call in message.tool_calls
        ]
        return types.Content(role="model", parts=parts)
    if message.role == "tool":
        return types.Content(
            role="tool",
            parts=[
                types.Part.from_function_response(name=result.name, response=result.content)
                for result in message.tool_results
            ],
        )
    return types.Content(role="user", parts=[types.Part.from_text(text=message.text or "")])


def from_response(
    response: types.GenerateContentResponse, request: LLMRequest, model: str
) -> LLMResponse:
    """A LogLens response from the SDK's response (raises ProviderError if empty)."""
    candidate = response.candidates[0] if response.candidates else None
    content = candidate.content if candidate is not None else None
    parts = content.parts if content is not None and content.parts else []
    text = "".join(part.text for part in parts if part.text and not part.thought)
    calls = tuple(
        ToolCall(part.function_call.name or "", dict(part.function_call.args or {}))
        for part in parts
        if part.function_call is not None
    )
    if not text and not calls:
        reason = candidate.finish_reason if candidate is not None else "no candidate"
        raise ProviderError(f"Gemini returned an empty answer (finish reason: {reason})")
    data = None
    if request.response_schema is not None and text:
        try:
            data = json.loads(text)
        except ValueError as error:
            raise ProviderError(f"Gemini returned invalid JSON: {error}") from error
    usage = response.usage_metadata
    tokens = Usage(
        input_tokens=(usage.prompt_token_count or 0) if usage else 0,
        output_tokens=((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0))
        if usage
        else 0,
    )
    raw = content.model_dump(mode="json", exclude_none=True) if content is not None else None
    return LLMResponse(text=text, data=data, tool_calls=calls, usage=tokens, model=model, raw=raw)


def map_api_error(error: errors.APIError) -> Exception:
    """Rate limits become RateLimitError (retryable), the daily quota QuotaExhaustedError."""
    details = json.dumps(getattr(error, "details", None) or {}, default=str)
    message = f"Gemini API error {error.code}: {error.message or error.status or ''}".strip()
    if error.code == 429:
        if "PerDay" in details or "per day" in details.lower():
            return QuotaExhaustedError(
                "The Gemini free-tier daily quota is exhausted. It resets daily (Pacific time); "
                "cached answers still work. Use --no-ai to continue without AI."
            )
        delay = _RETRY_DELAY.search(details)
        return RateLimitError(message, float(delay.group(1)) if delay else None)
    if error.code in {500, 502, 503, 504}:
        return RateLimitError(message, OVERLOAD_RETRY_SECONDS)
    if error.code in {401, 403}:
        return ProviderError(f"{message}. Check that {API_KEY_VARIABLE} holds a valid key.")
    return ProviderError(message)
