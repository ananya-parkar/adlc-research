# llm_client.py
"""
Provider-agnostic LLM client.

Every agent calls call_llm_json(). The provider is configured through .env.

.env:
    LLM_PROVIDER=groq
    LLM_MODEL=llama-3.3-70b-versatile
    GROQ_API_KEY=...
    ANTHROPIC_API_KEY=...
    OPENAI_API_KEY=...
"""

import json
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "anthropic": "claude-haiku-4-5-20251001",
    "openai": "gpt-4o-mini",
}

_clients: Dict[str, Any] = {}


def _get_provider() -> str:
    return os.environ.get("LLM_PROVIDER", "groq").lower()


def _get_model(provider: str) -> str:
    return os.environ.get("LLM_MODEL") or DEFAULT_MODELS[provider]


def _call_groq(
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    json_mode: bool = False,
) -> str:
    if "groq" not in _clients:
        from groq import Groq

        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set in environment")

        _clients["groq"] = Groq(api_key=api_key)

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = _clients["groq"].chat.completions.create(**kwargs)
    content = response.choices[0].message.content

    if not content:
        raise ValueError("Groq returned an empty response")

    return content


def _call_anthropic(
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    json_mode: bool = False,
) -> str:
    if "anthropic" not in _clients:
        import anthropic

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment")

        _clients["anthropic"] = anthropic.Anthropic(api_key=api_key)

    response = _clients["anthropic"].messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    text_blocks = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    ]

    if not text_blocks:
        raise ValueError("Anthropic returned no text content")

    return "\n".join(text_blocks)


def _call_openai(
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    json_mode: bool = False,
) -> str:
    if "openai" not in _clients:
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")

        _clients["openai"] = OpenAI(api_key=api_key)

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = _clients["openai"].chat.completions.create(**kwargs)
    content = response.choices[0].message.content

    if not content:
        raise ValueError("OpenAI returned an empty response")

    return content


_PROVIDER_FUNCS = {
    "groq": _call_groq,
    "anthropic": _call_anthropic,
    "openai": _call_openai,
}


def call_llm(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 1024,
    *,
    json_mode: bool = False,
) -> str:
    """Return a raw response from the configured provider."""

    provider = _get_provider()

    if provider not in _PROVIDER_FUNCS:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}' — "
            "use groq, anthropic, or openai"
        )

    resolved_model = model or _get_model(provider)

    logger.debug(
        "Calling %s with model %s (json_mode=%s)",
        provider,
        resolved_model,
        json_mode,
    )

    return _PROVIDER_FUNCS[provider](
        system_prompt,
        user_prompt,
        resolved_model,
        max_tokens,
        json_mode,
    )


def _clean_json_response(raw: str) -> str:
    """Remove surrounding Markdown fences without changing JSON values."""

    cleaned = raw.strip()

    cleaned = re.sub(
        r"\A```(?:json)?[ \t]*\r?\n?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\r?\n?```[ \t]*\Z", "", cleaned)

    return cleaned.strip()


def call_llm_json(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """
    Call the configured provider and parse a JSON object.
    Retry once if the response is invalid JSON.
    """

    last_error = None

    for attempt in range(2):
        retry_prompt = user_prompt

        if attempt == 1:
            retry_prompt = (
                user_prompt
                + "\n\nIMPORTANT: Your previous response was not valid JSON. "
                "Regenerate the complete response from scratch. "
                "Return exactly one valid JSON object. "
                "Use double quotes for all JSON keys and string values. "
                "Use valid JSON numbers such as 0.9, never words inside numbers. "
                "Do not include Markdown fences, comments, or explanatory text. "
                "Do not omit any required fields."
            )

            logger.warning(
                "Retrying LLM JSON generation after invalid response"
            )

        raw = call_llm(
            system_prompt,
            retry_prompt,
            model=model,
            max_tokens=max_tokens,
            json_mode=True,
        )

        cleaned = _clean_json_response(raw)

        try:
            result = json.loads(cleaned)

            if not isinstance(result, dict):
                raise ValueError(
                    "Expected a JSON object at the top level, "
                    f"got {type(result).__name__}"
                )

            return result

        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc

            logger.warning(
                "LLM JSON parse failed on attempt %s/2: %s",
                attempt + 1,
                exc,
            )

            if attempt == 1:
                logger.error(
                    "LLM returned invalid JSON after retry. "
                    "Response excerpt: %s",
                    raw[:2000],
                )

    raise ValueError(
        f"LLM did not return valid JSON after 2 attempts: {last_error}"
    ) from last_error
