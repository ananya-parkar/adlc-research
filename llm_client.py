# llm_client.py
"""
Provider-agnostic LLM client.

.env controls:
    LLM_PROVIDER=groq          # "groq" | "anthropic" | "openai"
    LLM_MODEL=llama-3.3-70b-versatile   # optional — sensible default used if unset
    LLM_JSON_MODE=true         # optional — native JSON mode on groq/openai (default true)
    GROQ_API_KEY=...           # whichever provider you're using
    ANTHROPIC_API_KEY=...
    OPENAI_API_KEY=...

What changed vs v1:
  * Detects truncated responses (hit max_tokens) and raises
    LLMTruncatedError instead of a confusing JSONDecodeError, so callers
    can split their input and retry.
  * Native JSON mode on Groq/OpenAI (response_format=json_object), with
    automatic fallback if the model doesn't support it.
  * More forgiving JSON extraction (code fences, leading/trailing prose).
  * Low default temperature for JSON calls (more stable output).
  * More SDK retries, so rate limits (429) back off instead of failing.
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}

SDK_MAX_RETRIES = 5

_clients: Dict[str, Any] = {}  # cached per-provider client instances


class LLMResponseError(ValueError):
    """The LLM answered, but the answer is unusable (invalid JSON, truncated, ...)."""


class LLMTruncatedError(LLMResponseError):
    """The answer stopped because it hit max_tokens."""


def _get_provider() -> str:
    return os.environ.get("LLM_PROVIDER", "groq").lower()


def _get_model(provider: str) -> str:
    return os.environ.get("LLM_MODEL") or DEFAULT_MODELS[provider]


def _json_mode_enabled() -> bool:
    return os.environ.get("LLM_JSON_MODE", "true").lower() in ("1", "true", "yes")


# ----------------------------------------------------------------------
# providers — each returns (text, truncated)
# ----------------------------------------------------------------------
def _chat_kwargs(system_prompt, user_prompt, model, max_tokens, temperature, json_mode):
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def _call_groq(system_prompt, user_prompt, model, max_tokens, temperature, json_mode) -> Tuple[str, bool]:
    if "groq" not in _clients:
        from groq import Groq

        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set in environment")
        _clients["groq"] = Groq(api_key=api_key, max_retries=SDK_MAX_RETRIES)

    response = _clients["groq"].chat.completions.create(
        **_chat_kwargs(system_prompt, user_prompt, model, max_tokens, temperature, json_mode)
    )
    choice = response.choices[0]
    return choice.message.content or "", choice.finish_reason == "length"


def _call_openai(system_prompt, user_prompt, model, max_tokens, temperature, json_mode) -> Tuple[str, bool]:
    if "openai" not in _clients:
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        _clients["openai"] = OpenAI(api_key=api_key, max_retries=SDK_MAX_RETRIES)

    response = _clients["openai"].chat.completions.create(
        **_chat_kwargs(system_prompt, user_prompt, model, max_tokens, temperature, json_mode)
    )
    choice = response.choices[0]
    return choice.message.content or "", choice.finish_reason == "length"


def _call_anthropic(system_prompt, user_prompt, model, max_tokens, temperature, json_mode) -> Tuple[str, bool]:
    if "anthropic" not in _clients:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
        _clients["anthropic"] = anthropic.Anthropic(api_key=api_key, max_retries=SDK_MAX_RETRIES)

    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    response = _clients["anthropic"].messages.create(**kwargs)
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    return text, response.stop_reason == "max_tokens"


_PROVIDER_FUNCS = {
    "groq": _call_groq,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
}


def _complete(system_prompt, user_prompt, model, max_tokens, temperature, json_mode) -> Tuple[str, bool]:
    provider = _get_provider()
    if provider not in _PROVIDER_FUNCS:
        raise RuntimeError(f"Unknown LLM_PROVIDER '{provider}' — use groq, anthropic, or openai")

    resolved_model = model or _get_model(provider)
    func = _PROVIDER_FUNCS[provider]
    logger.debug("Calling %s with model %s (max_tokens=%d)", provider, resolved_model, max_tokens)

    try:
        return func(system_prompt, user_prompt, resolved_model, max_tokens, temperature, json_mode)
    except Exception as exc:  # provider SDK errors
        msg = str(exc).lower()
        if json_mode and "json_validate_failed" in msg:
            # Groq JSON mode: model produced invalid/truncated JSON
            raise LLMResponseError(f"Provider rejected invalid JSON output: {exc}") from exc
        if json_mode and "response_format" in msg:
            logger.warning("Model does not support JSON mode; retrying without it")
            return func(system_prompt, user_prompt, resolved_model, max_tokens, temperature, False)
        raise


# ----------------------------------------------------------------------
# public API
# ----------------------------------------------------------------------

def call_llm(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: Optional[float] = None,
    json_mode: bool = False,
) -> str:
    """Raw text response from whichever provider is configured in .env."""
    text, truncated = _complete(
        system_prompt,
        user_prompt,
        model,
        max_tokens,
        temperature,
        json_mode,
    )
    if truncated:
        logger.warning(
            "LLM response was truncated at max_tokens=%d",
            max_tokens,
        )
    return text




def _parse_json(raw: str) -> Dict[str, Any]:
    """Parse a JSON object, allowing fences or surrounding prose."""
    cleaned = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        (raw or "").strip(),
        flags=re.IGNORECASE,
    )

    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        result = None

    if result is None:
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start == -1 or end <= start:
            raise LLMResponseError("LLM did not return valid JSON")

        try:
            result = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMResponseError(
                "LLM did not return valid JSON"
            ) from exc

    if not isinstance(result, dict):
        raise LLMResponseError(
            f"Expected a JSON object, got {type(result).__name__}"
        )

    return result



def call_llm_json(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    temperature: Optional[float] = 0.1,
) -> Dict[str, Any]:
    """
    Generate a JSON object.
    Retry once for invalid JSON, but do not retry truncation
    or provider/API failures as if they were parsing errors.
    """
    provider = _get_provider()
    json_mode = _json_mode_enabled() and provider in ("groq", "openai")
    last_error = None

    for attempt in range(2):
        retry_prompt = user_prompt

        if attempt == 1:
            retry_prompt += (
                "\n\nIMPORTANT: Your previous response was not valid JSON. "
                "Regenerate the complete response from scratch. "
                "Return exactly one valid JSON object. "
                "Use double quotes for all JSON keys and string values. "
                "Use valid JSON numbers. "
                "Do not include Markdown fences, comments, or "
                "explanatory text. Do not omit required fields."
            )
            logger.warning(
                "Retrying LLM JSON generation after invalid response"
            )

        raw, truncated = _complete(
            system_prompt,
            retry_prompt,
            model,
            max_tokens,
            temperature,
            json_mode,
        )

        if truncated:
            raise LLMTruncatedError(
                f"LLM response truncated at max_tokens={max_tokens}"
            )

        try:
            return _parse_json(raw)
        except LLMResponseError as exc:
            last_error = exc
            logger.warning(
                "LLM JSON parse failed on attempt %s/2: %s",
                attempt + 1,
                exc,
            )

            if attempt == 1:
                logger.error(
                    "Invalid JSON after retry. Response excerpt: %s",
                    raw[:2000],
                )

    raise LLMResponseError(
        f"LLM did not return valid JSON after 2 attempts: {last_error}"
    ) from last_error
