"""LLM client for llama.cpp / LM Studio / any OpenAI-compatible endpoint."""

import json
import logging
import time
import urllib.request
import urllib.error

from config import Config

logger = logging.getLogger("kairos.llm")


def _log_interaction(config: Config, messages, payload, response_data, content, elapsed_s,
                     *, run_id: str = "", tick: int = 0):
    """Append request/response summary to workspace/llm_log.jsonl."""
    try:
        usage = response_data.get("usage", {})
        details = usage.get("completion_tokens_details", {})
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "run_id": run_id,
            "tick": tick,
            "elapsed_s": round(elapsed_s, 2),
            "model": payload.get("model", ""),
            "temperature": payload.get("temperature"),
            "max_tokens": payload.get("max_tokens"),
            "prompt_chars": prompt_chars,
            "response_chars": len(content or ""),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "reasoning_tokens": details.get("reasoning_tokens", 0),
            # Full content — untruncated for post-run analysis
            "messages_preview": [{"role": m["role"], "content": m["content"]} for m in messages],
            "response_preview": (content or ""),
        }
        log_path = config.workspace / "llm_log.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


class LLMError(Exception):
    """Raised on LLM request failure."""
    pass


class ReasoningExhausted(LLMError):
    """Raised when a thinking model used all tokens on reasoning, producing no content.

    Callers should catch this to implement adaptive retry strategies
    (raise max_tokens, shrink context, add budget feedback to prompt).
    """
    def __init__(self, reasoning: str, reasoning_tokens: int, max_tokens: int):
        self.reasoning = reasoning
        self.reasoning_tokens = reasoning_tokens
        self.max_tokens = max_tokens
        super().__init__(
            f"Reasoning exhausted token budget "
            f"({reasoning_tokens}/{max_tokens} tokens, 0 content tokens)")


def ensure_model_loaded(config: Config, ttl: int = 3600) -> str:
    """Ensure the configured model is loaded in LM Studio, loading it if needed.

    Uses GET /v1/models to check, then POST /api/v1/models/load if absent.
    Sets a TTL (default 1 hour) to prevent idle eviction during long runs.

    Returns a status string: 'already_loaded', 'loaded', or raises LLMError.
    """
    base = config.llm_url.rstrip("/")
    model = config.llm_model

    # Step 1: Check if model is already loaded
    list_url = base + "/v1/models"
    try:
        req = urllib.request.Request(list_url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        loaded_ids = [m.get("id", "") for m in data.get("data", [])]
        if model in loaded_ids:
            logger.info("model already loaded: %s", model)
            return "already_loaded"
        logger.info("model not loaded (have: %s), requesting load: %s",
                     loaded_ids, model)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise LLMError(f"Cannot reach LM Studio at {base}: {e}") from e

    # Step 2: Request model load via LM Studio REST API
    load_url = base + "/api/v1/models/load"
    payload = {"model": model}
    req = urllib.request.Request(
        load_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        load_time = result.get("load_time_seconds", "?")
        logger.info("model loaded: %s in %ss", model, load_time)
        return "loaded"
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise LLMError(f"Failed to load model '{model}': HTTP {e.code}: {error_body}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise LLMError(f"Failed to load model '{model}': {e}") from e


def complete(
    messages: list[dict],
    config: Config,
    temperature: float = None,
    max_tokens: int = None,
    *,
    run_id: str = "",
    tick: int = 0,
) -> str:
    """Send a chat completion request, return the assistant's content string.

    Uses the OpenAI-compatible /v1/chat/completions endpoint.
    Raises ReasoningExhausted if a thinking model uses all tokens on
    reasoning_content with zero content tokens — callers should catch
    this and retry with a larger budget or smaller prompt.
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
            _log_interaction(config, messages, payload, data, reasoning, elapsed,
                             run_id=run_id, tick=tick)
            raise ReasoningExhausted(reasoning, reasoning_tokens, max_tokens)

        if not content:
            raise LLMError(f"Empty response content. Full response: {data}")

        _log_interaction(config, messages, payload, data, content, elapsed,
                         run_id=run_id, tick=tick)
        return content
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected response format: {data}") from e
