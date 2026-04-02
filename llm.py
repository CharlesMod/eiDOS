"""LLM client for llama.cpp / LM Studio / any OpenAI-compatible endpoint."""

import json
import logging
import urllib.request
import urllib.error

from config import Config

logger = logging.getLogger("kairos.llm")


class LLMError(Exception):
    """Raised on LLM request failure."""
    pass


def complete(
    messages: list[dict],
    config: Config,
    temperature: float = None,
    max_tokens: int = None,
) -> str:
    """Send a chat completion request, return the assistant's content string.

    Uses the OpenAI-compatible /v1/chat/completions endpoint.
    """
    if temperature is None:
        temperature = config.llm_temperature
    if max_tokens is None:
        max_tokens = config.llm_max_tokens

    url = config.llm_url.rstrip("/") + "/v1/chat/completions"

    payload = {
        "model": config.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": config.llm_top_p,
        "top_k": config.llm_top_k,
        "min_p": config.llm_min_p,
        "presence_penalty": config.llm_presence_penalty,
        "stream": False,
        "seed": 42,
    }

    body = json.dumps(payload).encode("utf-8")
    logger.debug("llm payload_bytes=%d messages=%d", len(body), len(messages))
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=config.llm_request_timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise LLMError(f"HTTP {e.code}: {error_body}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"Connection failed: {e.reason}") from e
    except TimeoutError:
        raise LLMError(f"Request timed out after {config.llm_request_timeout_s}s")
    except OSError as e:
        raise LLMError(f"Network error: {e}") from e

    try:
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""

        # Thinking models (Qwen 3.5, etc.) put reasoning in a separate field.
        # If content is empty but reasoning_content exists, the model exhausted
        # max_tokens during thinking — log it and return what we have.
        reasoning = msg.get("reasoning_content") or ""

        # Log token usage if available
        usage = data.get("usage", {})
        reasoning_tokens = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        if reasoning_tokens or completion_tokens:
            logger.info("llm tokens: completion=%d reasoning=%d prompt=%d",
                        completion_tokens, reasoning_tokens, usage.get("prompt_tokens", 0))

        if not content and reasoning:
            logger.warning("llm returned empty content but %d chars of reasoning — "
                           "max_tokens likely exhausted during thinking", len(reasoning))
            # Return the reasoning so the caller has *something* to work with
            return reasoning

        if not content:
            raise LLMError(f"Empty response content. Full response: {data}")

        return content
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected response format: {data}") from e
