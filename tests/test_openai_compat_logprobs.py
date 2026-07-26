"""logprobs must be opt-in — sending it unconditionally 400s on Gemini/Ollama.

Regression for ``OpenAICompatProvider.create()``: the ``logprobs`` request param
is added only when the provider was constructed with ``logprobs=True``. Sending
it unconditionally broke every OpenAI-compatible endpoint that rejects unknown
fields instead of ignoring them (Gemini's ``/v1beta/openai/`` layer returns
``400 Unknown name "logprobs"``; Ollama behaves similarly). The LiteLLM backend
inherits the same gate via the shared ``create()``.

Self-contained: run directly with any Python 3.10+ that can import ``arbor``
(``python tests/test_openai_compat_logprobs.py``) or collect with pytest.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if "arbor" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "arbor", _ROOT / "src" / "__init__.py",
        submodule_search_locations=[str(_ROOT / "src")],
    )
    assert _spec and _spec.loader
    _arbor = importlib.util.module_from_spec(_spec)
    sys.modules["arbor"] = _arbor
    _spec.loader.exec_module(_arbor)

from arbor.core.llm.openai_compat import OpenAICompatProvider  # noqa: E402


def _fake_raw() -> SimpleNamespace:
    """Minimal chat-completions response that _parse_response can consume."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=None, tool_calls=None),
                logprobs=None,
            )
        ],
        usage=None,
        model="test-model",
    )


def _capturing_provider(**kwargs: Any) -> tuple[OpenAICompatProvider, dict[str, Any]]:
    """Provider whose _acompletion records the params it was called with."""
    provider = OpenAICompatProvider(model="test-model", api_key="dummy", **kwargs)
    captured: dict[str, Any] = {}

    async def _capture(**params: Any) -> Any:
        captured.update(params)
        return _fake_raw()

    provider._acompletion = _capture  # type: ignore[assignment]
    return provider, captured


def test_logprobs_defaults_off_in_config() -> None:
    from arbor.core.config_schema import LLMConfig

    assert LLMConfig().logprobs is False
    assert LLMConfig(logprobs=True).logprobs is True


def test_logprobs_not_sent_by_default() -> None:
    provider, captured = _capturing_provider()
    asyncio.run(provider.create(system="s", messages=[{"role": "user", "content": "hi"}]))
    assert "logprobs" not in captured, f"expected no logprobs, got: {captured}"


def test_logprobs_sent_when_enabled() -> None:
    provider, captured = _capturing_provider(logprobs=True)
    asyncio.run(provider.create(system="s", messages=[{"role": "user", "content": "hi"}]))
    assert captured.get("logprobs") is True


def test_litellm_provider_inherits_gate() -> None:
    """LiteLLMProvider subclasses OpenAICompatProvider and shares create(); its
    logprobs attribute must default off and be settable via the constructor."""
    from arbor.core.llm.litellm_provider import LiteLLMProvider

    assert LiteLLMProvider(model="gpt-4o", api_key="dummy").logprobs is False
    assert LiteLLMProvider(model="gpt-4o", api_key="dummy", logprobs=True).logprobs is True


def test_create_provider_plumbs_logprobs() -> None:
    """create_provider must forward config.logprobs to the constructed provider."""
    from arbor.core import create_provider

    base = dict(
        provider="openai-chat", openai_api="chat", model="m", api_key="k",
        base_url=None, llm_provider_retries=1, llm_timeout=10.0,
        reasoning_effort="high",
    )
    off = create_provider(SimpleNamespace(**base, logprobs=False))
    on = create_provider(SimpleNamespace(**base, logprobs=True))
    assert off.logprobs is False
    assert on.logprobs is True


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")
