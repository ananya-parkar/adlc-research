"""
Provider-agnostic LLM client. Every agent calls call_llm_json() —
which provider actually runs underneath is controlled entirely by
.env, no code changes needed to swap providers later.

.env controls:
    LLM_PROVIDER=groq          # "groq" | "anthropic" | "openai"
    LLM_MODEL=llama-3.3-70b-versatile   # optional — sensible default used if unset
    GROQ_API_KEY=...           # whichever provider you're using
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

_clients: Dict[str, Any] = {}  # cached per-provider client instances


def _get_provider() -> str:
    return os.environ.get("LLM_PROVIDER", "groq").lower()


def _get_model(provider: str) -> str:
    return os.environ.get("LLM_MODEL") or DEFAULT_MODELS[provider]


def _call_groq(system_prompt: str, user_prompt: str, model: str, max_tokens: int) -> str:
    if "groq" not in _clients:
        from groq import Groq
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set in environment")
        _clients["groq"] = Groq(api_key=api_key)

    response = _clients["groq"].chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content


def _call_anthropic(system_prompt: str, user_prompt: str, model: str, max_tokens: int) -> str:
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
    return response.content[0].text


def _call_openai(system_prompt: str, user_prompt: str, model: str, max_tokens: int) -> str:
    if "openai" not in _clients:
        from openai import OpenAI
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set in environment")
        _clients["openai"] = OpenAI(api_key=api_key)

    response = _clients["openai"].chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content


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
) -> str:
    """Raw text response from whichever provider is configured in .env."""
    provider = _get_provider()
    if provider not in _PROVIDER_FUNCS:
        raise ValueError(f"Unknown LLM_PROVIDER '{provider}' — use groq, anthropic, or openai")

    resolved_model = model or _get_model(provider)
    logger.debug("Calling %s with model %s", provider, resolved_model)
    return _PROVIDER_FUNCS[provider](system_prompt, user_prompt, resolved_model, max_tokens)


def call_llm_json(
    system_prompt: str,
    user_prompt: str,
    model: Optional[str] = None,
    max_tokens: int = 1024,
) -> Dict[str, Any]:
    """
    Same as call_llm(), but strips markdown code fences if present
    and parses the result as JSON. Raises ValueError if the response
    isn't valid JSON after cleanup.
    """
    raw = call_llm(system_prompt, user_prompt, model=model, max_tokens=max_tokens)
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse LLM response as JSON: %s\nRaw response: %s", exc, raw)
        raise ValueError(f"LLM did not return valid JSON: {exc}") from exc