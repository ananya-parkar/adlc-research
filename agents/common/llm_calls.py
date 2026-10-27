# agents/common/llm_calls.py
"""
Shared LLM-call wrapper for planning agents.

llm_json(system, user, max_tokens, attempts, validate)
  * retries unusable answers (invalid JSON, failed validation)
  * on truncation retries with a bigger token budget
  * waits out short rate limits using the provider's "try again in …"
  * raises QuotaExhausted when the provider asks for a long wait (stop the run cleanly)
  * raises GenerationFailed when attempts run out

(The Architecture agent has its own copy of this logic; it can switch to this
module in a later clean-up.)
"""

import logging
import os
import re
import time
from typing import Callable, Optional

from llm_client import LLMResponseError, LLMTruncatedError, call_llm_json

logger = logging.getLogger(__name__)

MAX_TOKENS_CAP = int(os.environ.get("LLM_MAX_TOKENS_CAP", 8000))
MAX_RATE_LIMIT_WAIT = int(os.environ.get("LLM_MAX_RATE_LIMIT_WAIT", 240))  # seconds

_WAIT_RE = re.compile(r"try again in\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:([\d.]+)s)?", re.I)


class QuotaExhausted(Exception):
    """Provider asks us to wait longer than we are willing to."""


class GenerationFailed(Exception):
    """The LLM could not produce a usable answer after all attempts."""


def _retry_after(message: str) -> Optional[float]:
    m = _WAIT_RE.search(message)
    if not m or not any(m.groups()):
        return None
    h, mins, secs = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mins * 60 + secs


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return getattr(exc, "status_code", None) == 429 or "rate limit" in text or "rate_limit" in text


def llm_json(system: str, user: str, max_tokens: int, attempts: int, validate: Callable[[dict], object]):
    tokens, tried, waits, last_error = max_tokens, 0, 0, None
    while tried < attempts:
        try:
            raw = call_llm_json(system, user, max_tokens=tokens)
            return validate(raw)
        except LLMTruncatedError as exc:
            tried += 1
            last_error = exc
            tokens = min(int(tokens * 1.8), MAX_TOKENS_CAP)
            logger.warning("Answer truncated — retrying with max_tokens=%d", tokens)
        except (LLMResponseError, ValueError) as exc:
            tried += 1
            last_error = exc
            logger.warning("Unusable answer (%s) — attempt %d/%d", str(exc)[:160], tried, attempts)
        except Exception as exc:  # provider errors
            if not _is_rate_limit(exc):
                raise
            wait = _retry_after(str(exc)) or 60
            if wait > MAX_RATE_LIMIT_WAIT or waits >= 5:
                raise QuotaExhausted(f"provider asks to wait {wait:.0f}s: {str(exc)[:200]}") from exc
            waits += 1
            logger.warning("Rate limited — waiting %.0fs as the provider asks", wait + 2)
            time.sleep(wait + 2)
    raise GenerationFailed(str(last_error)[:300])