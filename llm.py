"""LLM client for llama.cpp / LM Studio / any OpenAI-compatible endpoint."""

import json
import logging
import time
import urllib.request
import urllib.error

from config import Config

logger = logging.getLogger("kairos.llm")


def _log_interaction(config: Config, messages, payload, response_data, content, elapsed_s):
    """Append request/response summary to workspace/llm_log.jsonl."""
    try:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_s": round(elapsed_s, 2),
            "model": payload.get("model", ""),
            "temperature": payload.get("temperature"),
            "max_tokens": payload.get("max_tokens"),
            "messages": [{"role": m["role"], "content": m["content"][:500]} for m in messages],
            "response_content": (content or "")[:1000],
            "usage": response_data.get("usage", {}),
        }
        log_path = config.workspace / "llm_log.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


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

    start = time.monotonic()
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

    elapsed = time.monotonic() - start

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
            _log_interaction(config, messages, payload, data, reasoning, elapsed)
            return reasoning

        if not content:
            raise LLMError(f"Empty response content. Full response: {data}")

        _log_interaction(config, messages, payload, data, content, elapsed)
        return content
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected response format: {data}") from e
