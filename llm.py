"""LLM client for llama.cpp / LM Studio / any OpenAI-compatible endpoint."""

import json
import logging
import time
import urllib.request
import urllib.error
from pathlib import Path

from config import Config

logger = logging.getLogger("eidos.llm")


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
    on_token: callable = None,
) -> str:
    """Send a chat completion request, return the assistant's content string.

    Uses the OpenAI-compatible /v1/chat/completions endpoint.
    When *on_token* is provided, uses SSE streaming and calls
    ``on_token(partial_content)`` after each chunk so the dashboard
    can display live output.
    Raises ReasoningExhausted if a thinking model uses all tokens on
    reasoning_content with zero content tokens — callers should catch
    this and retry with a larger budget or smaller prompt.
    """
    if temperature is None:
        temperature = config.llm_temperature
    if max_tokens is None:
        max_tokens = config.llm_max_tokens

    url = config.llm_url.rstrip("/") + "/v1/chat/completions"

    use_stream = on_token is not None

    payload = {
        "model": config.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": config.llm_top_p,
        "top_k": config.llm_top_k,
        "min_p": config.llm_min_p,
        "presence_penalty": config.llm_presence_penalty,
        "stream": use_stream,
        # Reuse the KV of the unchanging prompt prefix (system + stable durable head) across ticks
        # instead of re-prefilling it every tick — llama.cpp's biggest latency/FLOP win. Pairs with
        # the STABLE→VOLATILE context ordering in context.py so the cached prefix is long.
        "cache_prompt": True,
    }

    body = json.dumps(payload).encode("utf-8")
    logger.debug("llm payload_bytes=%d messages=%d stream=%s", len(body), len(messages), use_stream)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    start = time.monotonic()
    try:
        resp = urllib.request.urlopen(req, timeout=config.llm_request_timeout_s)
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
        if use_stream:
            content, reasoning, usage = _read_stream(resp, on_token)
        else:
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            usage = data.get("usage", {})
    except (KeyError, IndexError) as e:
        raise LLMError(f"Unexpected response format") from e
    finally:
        resp.close()

    elapsed = time.monotonic() - start

    # Log token usage if available
    reasoning_tokens = usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    if reasoning_tokens or completion_tokens:
        logger.info("llm tokens: completion=%d reasoning=%d prompt=%d",
                     completion_tokens, reasoning_tokens, usage.get("prompt_tokens", 0))

    # Build a response_data dict for logging (matches non-stream format)
    response_data = {"usage": usage, "choices": [{"message": {"content": content, "reasoning_content": reasoning}}]}

    if not content and reasoning:
        _log_interaction(config, messages, payload, response_data, reasoning, elapsed,
                         run_id=run_id, tick=tick)
        raise ReasoningExhausted(reasoning, reasoning_tokens, max_tokens)

    if not content:
        raise LLMError(f"Empty response content. Usage: {usage}")

    _log_interaction(config, messages, payload, response_data, content, elapsed,
                     run_id=run_id, tick=tick)
    return content


def _read_stream(resp, on_token):
    """Read SSE stream, call on_token with partial content, return (content, reasoning, usage)."""
    content_parts = []
    reasoning_parts = []
    usage = {}
    last_cb = 0.0

    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line.startswith("data: "):
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                chunk = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})
            c = delta.get("content") or ""
            r = delta.get("reasoning_content") or ""
            if c:
                content_parts.append(c)
            if r:
                reasoning_parts.append(r)

            # Merge usage from final chunk if present
            if "usage" in chunk:
                usage = chunk["usage"]

            # Call back with partial content (throttle to every ~300ms)
            now = time.monotonic()
            if on_token and (c or r) and (now - last_cb > 0.3):
                on_token("".join(content_parts) if content_parts else "".join(reasoning_parts))
                last_cb = now

    # Final callback with complete text
    final = "".join(content_parts) if content_parts else "".join(reasoning_parts)
    if on_token and final:
        on_token(final)

    return "".join(content_parts), "".join(reasoning_parts), usage


# ---------------------------------------------------------------------------
# Planning model hot-swap
# ---------------------------------------------------------------------------

def planning_complete(
    messages: list[dict],
    config: Config,
    *,
    run_id: str = "",
    tick: int = 0,
) -> str:
    """Call the planning model (larger, hot-swapped) for subgoal generation.

    Hot-swap sequence:
    1. Rewrite llama-server.service with the planning model path/params
    2. Restart llama-server, wait for it to be healthy
    3. Run the completion
    4. Restore the original service file and restart

    Falls back to the current model if no restart_cmd is configured.
    """
    import subprocess

    planning_max_tokens = config.planning_max_tokens

    if not config.llm_restart_cmd:
        # No hot-swap available — just use the current model
        logger.info("planning_complete: no restart_cmd, using current model")
        return complete(messages, config, temperature=0.6, max_tokens=planning_max_tokens,
                        run_id=run_id, tick=tick)

    service_path = "/etc/systemd/system/llama-server.service"

    # Read current service file
    try:
        original_service = Path(service_path).read_text()
    except OSError as e:
        logger.warning("Cannot read service file, falling back: %s", e)
        return complete(messages, config, temperature=0.6, max_tokens=planning_max_tokens,
                        run_id=run_id, tick=tick)

    # Check if planning model is different from currently loaded model
    import re as _re
    _cur_model = _re.search(r'-m\s+(\S+)', original_service)
    if _cur_model and Path(_cur_model.group(1)).resolve() == Path(config.planning_model_path).resolve():
        # Same model — no need to hot-swap (just use current server)
        logger.info("planning_complete: planning model same as active, no swap")
        return complete(messages, config, temperature=0.6, max_tokens=planning_max_tokens,
                        run_id=run_id, tick=tick)

    # Build the planning service config
    planning_service = _build_planning_service(original_service, config)

    logger.info("planning_complete: hot-swapping to planning model")
    try:
        # Write planning service file and restart
        _sudo_write(service_path, planning_service)
        _restart_llama(config)

        # Run the completion on the bigger model
        result = complete(messages, config, temperature=0.6, max_tokens=planning_max_tokens,
                          run_id=run_id, tick=tick)
        return result
    finally:
        # Always restore original service and restart
        logger.info("planning_complete: restoring original model")
        try:
            _sudo_write(service_path, original_service)
            _restart_llama(config, timeout_s=120)
        except Exception as e:
            logger.error("Failed to restore original model: %s", e)


def _build_planning_service(original: str, config: Config) -> str:
    """Rewrite the llama-server service file for the planning model."""
    import re
    result = original
    # Swap model path
    result = re.sub(r'-m\s+\S+', f'-m {config.planning_model_path}', result)
    # Swap context size
    result = re.sub(r'-c\s+\d+', f'-c {config.planning_context_size}', result)
    # Swap or add reasoning budget
    if '--reasoning-budget' in result:
        result = re.sub(r'--reasoning-budget\s+\d+',
                         f'--reasoning-budget {config.planning_reasoning_budget}', result)
    return result


def _sudo_write(path: str, content: str) -> None:
    """Write a file via sudo tee (service files are root-owned)."""
    import subprocess
    proc = subprocess.run(
        ["sudo", "tee", path],
        input=content.encode(),
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        raise OSError(f"sudo tee {path} failed: {proc.stderr.decode()[:200]}")


def _restart_llama(config: Config, *, timeout_s: int = 90) -> None:
    """Restart llama-server and wait for health.

    Raises LLMError if the server doesn't become healthy within *timeout_s*.
    """
    import subprocess
    subprocess.run(
        "sudo systemctl daemon-reload && sudo systemctl restart llama-server",
        shell=True, timeout=30, capture_output=True,
    )
    # Poll health endpoint
    import urllib.request
    health_url = config.llm_url.rstrip("/") + "/health"
    polls = timeout_s // 2
    for _ in range(polls):
        time.sleep(2)
        try:
            with urllib.request.urlopen(health_url, timeout=5) as resp:
                data = json.loads(resp.read().decode())
                if data.get("status") == "ok":
                    logger.info("llama-server healthy after restart")
                    return
        except Exception:
            continue
    raise LLMError(f"llama-server did not become healthy within {timeout_s}s")
