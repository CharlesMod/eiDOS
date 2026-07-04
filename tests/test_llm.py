"""Tests for LLM client behavior and error handling."""

import io
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import patch
import urllib.error

from config import Config
from llm import complete, LLMError, ReasoningExhausted


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestLLMComplete(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.llm_url = "http://localhost:1234"
        self.config.llm_model = "test-model"
        self.messages = [{"role": "user", "content": "hello"}]

    @patch("llm.urllib.request.urlopen")
    def test_complete_success(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {"content": "hi there"}}]
        })

        out = complete(self.messages, self.config)

        self.assertEqual(out, "hi there")

    @patch("llm.urllib.request.urlopen")
    def test_grammar_disables_thinking(self, mock_urlopen):
        """A GBNF grammar masks the model's think-opening tokens from the first sampled token —
        off-distribution babble to max_tokens (the newborn's incoherent first ticks). Constrained
        calls must therefore disable the thinking phase via the chat template; unconstrained calls
        must NOT carry the kwarg (thinking stays available)."""
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {"content": "ok"}}]
        })
        complete(self.messages, self.config, grammar='root ::= "x"')
        sent = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent["chat_template_kwargs"], {"enable_thinking": False})
        self.assertIn("grammar", sent)

        complete(self.messages, self.config)
        sent = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        self.assertNotIn("chat_template_kwargs", sent)
        self.assertNotIn("grammar", sent)

    @patch("llm.urllib.request.urlopen")
    def test_complete_http_error_includes_body(self, mock_urlopen):
        http_error = urllib.error.HTTPError(
            url="http://localhost:1234/v1/chat/completions",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"bad"}'),
        )
        mock_urlopen.side_effect = http_error

        with self.assertRaises(LLMError) as ctx:
            complete(self.messages, self.config)

        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertIn("bad", str(ctx.exception))

    @patch("llm.urllib.request.urlopen")
    def test_complete_url_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        with self.assertRaises(LLMError) as ctx:
            complete(self.messages, self.config)

        self.assertIn("Connection failed", str(ctx.exception))

    @patch("llm.urllib.request.urlopen")
    def test_complete_timeout_error(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError()

        with self.assertRaises(LLMError) as ctx:
            complete(self.messages, self.config)

        self.assertIn("timed out", str(ctx.exception).lower())

    @patch("llm.urllib.request.urlopen")
    def test_complete_unexpected_response_format(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse({"unexpected": True})

        with self.assertRaises(LLMError) as ctx:
            complete(self.messages, self.config)

        self.assertIn("Unexpected response format", str(ctx.exception))

    # --- Thinking model (reasoning_content) tests ---

    @patch("llm.urllib.request.urlopen")
    def test_thinking_model_normal_content(self, mock_urlopen):
        """When both content and reasoning_content are present, return content."""
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {
                "content": "<tool>bash</tool>\n<args>{\"cmd\": \"ls\"}</args>",
                "reasoning_content": "I should list files first.",
            }}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30,
                      "completion_tokens_details": {"reasoning_tokens": 15}},
        })
        out = complete(self.messages, self.config)
        self.assertIn("<tool>bash</tool>", out)

    @patch("llm.urllib.request.urlopen")
    def test_thinking_model_empty_content_raises_reasoning_exhausted(self, mock_urlopen):
        """When content is empty but reasoning_content exists, raise ReasoningExhausted."""
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {
                "content": "",
                "reasoning_content": "I was still thinking about the approach...",
            }}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 32,
                      "completion_tokens_details": {"reasoning_tokens": 32}},
        })
        with self.assertRaises(ReasoningExhausted) as ctx:
            complete(self.messages, self.config)
        self.assertEqual(ctx.exception.reasoning_tokens, 32)
        self.assertIn("I was still thinking", ctx.exception.reasoning)

    @patch("llm.urllib.request.urlopen")
    def test_thinking_model_null_content_raises_reasoning_exhausted(self, mock_urlopen):
        """When content is null/None but reasoning exists, raise ReasoningExhausted."""
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {
                "content": None,
                "reasoning_content": "Still processing...",
            }}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 32,
                      "completion_tokens_details": {"reasoning_tokens": 32}},
        })
        with self.assertRaises(ReasoningExhausted) as ctx:
            complete(self.messages, self.config)
        self.assertIn("Still processing", ctx.exception.reasoning)

    @patch("llm.urllib.request.urlopen")
    def test_reasoning_exhausted_has_token_info(self, mock_urlopen):
        """ReasoningExhausted carries reasoning_tokens and max_tokens."""
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {
                "content": "",
                "reasoning_content": "deep thoughts...",
            }}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 1024,
                      "completion_tokens_details": {"reasoning_tokens": 1024}},
        })
        with self.assertRaises(ReasoningExhausted) as ctx:
            complete(self.messages, self.config, max_tokens=1024)
        self.assertEqual(ctx.exception.reasoning_tokens, 1024)
        self.assertEqual(ctx.exception.max_tokens, 1024)

    @patch("llm.urllib.request.urlopen")
    def test_thinking_model_both_empty_raises(self, mock_urlopen):
        """When both content and reasoning_content are empty, raise LLMError."""
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {
                "content": "",
                "reasoning_content": "",
            }}],
        })
        with self.assertRaises(LLMError) as ctx:
            complete(self.messages, self.config)
        self.assertIn("Empty response", str(ctx.exception))

    @patch("llm.urllib.request.urlopen")
    def test_thinking_model_no_reasoning_field(self, mock_urlopen):
        """Standard model without reasoning_content — content returned normally."""
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {"content": "hello"}}],
        })
        out = complete(self.messages, self.config)
        self.assertEqual(out, "hello")

    @patch("llm.urllib.request.urlopen")
    def test_token_usage_with_reasoning_tokens(self, mock_urlopen):
        """Token usage info is available and doesn't break anything."""
        mock_urlopen.return_value = _FakeResponse({
            "choices": [{"message": {
                "content": "done",
                "reasoning_content": "thinking...",
            }}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "completion_tokens_details": {"reasoning_tokens": 30},
            },
        })
        out = complete(self.messages, self.config)
        self.assertEqual(out, "done")


class TestEnsureModelLoaded(unittest.TestCase):

    def setUp(self):
        self.config = Config()
        self.config.llm_url = "http://localhost:1234"
        self.config.llm_model = "test-model"

    @patch("llm.urllib.request.urlopen")
    def test_model_already_loaded(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse({
            "data": [{"id": "test-model"}]
        })
        from llm import ensure_model_loaded
        result = ensure_model_loaded(self.config)
        self.assertEqual(result, "already_loaded")

    @patch("llm.urllib.request.urlopen")
    def test_model_needs_loading(self, mock_urlopen):
        # First call: list models (model not present)
        # Second call: load model
        responses = [
            _FakeResponse({"data": [{"id": "other-model"}]}),
            _FakeResponse({"load_time_seconds": 5.2}),
        ]
        mock_urlopen.side_effect = responses
        from llm import ensure_model_loaded
        result = ensure_model_loaded(self.config)
        self.assertEqual(result, "loaded")
        self.assertEqual(mock_urlopen.call_count, 2)

    @patch("llm.urllib.request.urlopen")
    def test_model_unreachable(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        from llm import ensure_model_loaded
        with self.assertRaises(LLMError) as ctx:
            ensure_model_loaded(self.config)
        self.assertIn("Cannot reach", str(ctx.exception))

    @patch("llm.urllib.request.urlopen")
    def test_model_load_http_error(self, mock_urlopen):
        import io
        import urllib.error
        # First call succeeds (model not in list)
        # Second call fails (HTTP error on load)
        def side_effect(req, **kwargs):
            if "/v1/models" in req.full_url and req.get_method() != "POST":
                return _FakeResponse({"data": []})
            raise urllib.error.HTTPError(
                url="", code=500, msg="Internal Server Error",
                hdrs=None, fp=io.BytesIO(b"model too large"),
            )
        mock_urlopen.side_effect = side_effect
        from llm import ensure_model_loaded
        with self.assertRaises(LLMError) as ctx:
            ensure_model_loaded(self.config)
        self.assertIn("Failed to load", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
