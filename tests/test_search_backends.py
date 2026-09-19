"""Tests for the pluggable search backends + the web-tools factory.

Network is never touched: ``requests`` is monkeypatched and the alphaXiv SDK is
faked via ``ax._load_axv``.
"""

from __future__ import annotations

import asyncio
import json

import arbor.core.tools.web.backends as B
from arbor.coordinator.config import SearchConfig
from arbor.core.tools.web.backends import (
    ExaBackend,
    ExaMcpBackend,
    JinaSearchBackend,
    SerpBaseBackend,
    SerperBackend,
    SerplyBackend,
    build_search_backends,
    resolve_backend_names,
)
from arbor.core.tools.web.factory import build_web_search_tool, build_web_visit_tool
from arbor.core.tools.web.search import WebSearchTool


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Type": "application/json"}
        self.text = "ok"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


# ── resolve_backend_names ────────────────────────────────────────────────────

def test_resolve_legacy_alphaxiv():
    assert resolve_backend_names(SearchConfig(builtin_backend="alphaxiv")) == ["alphaxiv"]


def test_resolve_legacy_endpoint():
    sc = SearchConfig(web_search_endpoint="http://x/search")
    assert resolve_backend_names(sc) == ["endpoint"]


def test_resolve_explicit_list_keyless():
    sc = SearchConfig(backends=["alphaxiv", "jina"])
    assert resolve_backend_names(sc) == ["alphaxiv", "jina"]


def test_resolve_drops_keyless_missing_creds(monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("SERPBASE_API_KEY", raising=False)
    monkeypatch.delenv("SERPLY_API_KEY", raising=False)
    sc = SearchConfig(backends=["serper", "serpbase", "serply", "exa", "jina"])
    # serper/serpbase/serply/exa dropped (no key), jina kept (keyless)
    assert resolve_backend_names(sc) == ["jina"]


def test_resolve_keeps_keyed_when_key_present():
    sc = SearchConfig(backends=["serper", "serpbase", "serply", "exa"],
                      serper_api_key="s", serpbase_api_key="b",
                      serply_api_key="p", exa_api_key="e")
    assert resolve_backend_names(sc) == ["serper", "serpbase", "serply", "exa"]


def test_resolve_serply_from_env(monkeypatch):
    monkeypatch.setenv("SERPLY_API_KEY", "from-env")
    assert resolve_backend_names(SearchConfig(backends=["serply"])) == ["serply"]


def test_resolve_dedup_and_unknown_dropped():
    sc = SearchConfig(backends=["alphaxiv", "alphaxiv", "bogus"])
    assert resolve_backend_names(sc) == ["alphaxiv"]


# ── adapter parsing ──────────────────────────────────────────────────────────

def test_serper_backend_parses_organic(monkeypatch):
    payload = {"organic": [
        {"link": "https://a.com", "title": "A", "snippet": "sa"},
        {"link": "https://b.com", "title": "B", "snippet": "sb"},
    ]}
    monkeypatch.setattr(B.requests, "post", lambda *a, **k: _Resp(payload))
    items = asyncio.run(SerperBackend(api_key="k").search("q", 5))
    assert items == [
        {"url": "https://a.com", "title": "A", "snippets": "sa"},
        {"url": "https://b.com", "title": "B", "snippets": "sb"},
    ]


def test_exa_backend_parses_results(monkeypatch):
    payload = {"results": [
        {"url": "https://p.com", "title": "P", "text": "body text", "author": "Ann", "publishedDate": "2025"},
    ]}
    monkeypatch.setattr(B.requests, "post", lambda *a, **k: _Resp(payload))
    items = asyncio.run(ExaBackend(api_key="k").search("q", 5))
    assert items[0]["url"] == "https://p.com"
    assert items[0]["title"] == "P"
    assert "body text" in items[0]["snippets"]
    assert "Ann" in items[0]["snippets"]


# ── SerpBase adapter ─────────────────────────────────────────────────────────

def test_serpbase_backend_parses_organic(monkeypatch):
    payload = {"status": 0, "organic": [
        {"position": 1, "link": "https://a.com", "title": "A", "snippet": "sa"},
        {"position": 2, "link": "https://b.com", "title": "B", "snippet": "sb"},
    ]}
    monkeypatch.setattr(B.requests, "post", lambda *a, **k: _Resp(payload))
    items = asyncio.run(SerpBaseBackend(api_key="k").search("q", 5))
    assert items == [
        {"url": "https://a.com", "title": "A", "snippets": "sa"},
        {"url": "https://b.com", "title": "B", "snippets": "sb"},
    ]


def test_serpbase_backend_truncates_to_max_results(monkeypatch):
    payload = {"status": 0, "organic": [
        {"position": i, "link": f"https://x{i}.com", "title": f"X{i}", "snippet": ""}
        for i in range(1, 12)
    ]}
    monkeypatch.setattr(B.requests, "post", lambda *a, **k: _Resp(payload))
    items = asyncio.run(SerpBaseBackend(api_key="k").search("q", 10))
    assert len(items) == 10


def test_serpbase_backend_error_envelope_raises(monkeypatch):
    # HTTP 200 with a business error in the envelope (status != 0).
    payload = {"status": 1, "error": "Invalid API key"}
    monkeypatch.setattr(B.requests, "post", lambda *a, **k: _Resp(payload))
    try:
        asyncio.run(SerpBaseBackend(api_key="bad").search("q", 5))
    except RuntimeError as exc:
        assert "Invalid API key" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for error envelope")


def test_serpbase_backend_uses_post_x_api_key(monkeypatch):
    captured = {}

    def _fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        captured["headers"] = kwargs.get("headers")
        return _Resp({"status": 0, "organic": []})

    monkeypatch.setattr(B.requests, "post", _fake_post)
    asyncio.run(SerpBaseBackend(api_key="secret").search("hello", 5))
    assert captured["url"] == "https://api.serpbase.dev/google/search"
    assert captured["json"] == {"q": "hello", "num": 5}
    assert captured["headers"]["X-API-Key"] == "secret"


# ── Serply adapter ───────────────────────────────────────────────────────────

def test_serply_backend_parses_articles(monkeypatch):
    payload = {"articles": [
        {"title": "Active Retrieval Augmented Generation",
         "link": "https://doi.org/10.18653/v1/2023.emnlp-main.495",
         "description": "Z Jiang, F Xu, L Gao - EMNLP, 2023 - aclanthology.org"},
        {"title": "RAG for Knowledge-Intensive NLP Tasks",
         "link": "https://proceedings.neurips.cc/paper/2020/hash/6b493230.html",
         "description": "P Lewis, E Perez - NeurIPS, 2020 - proceedings.neurips.cc"},
    ], "results": []}
    monkeypatch.setattr(B.requests, "get", lambda *a, **k: _Resp(payload))
    items = asyncio.run(SerplyBackend(api_key="k").search("q", 5))
    assert items == [
        {"url": "https://doi.org/10.18653/v1/2023.emnlp-main.495",
         "title": "Active Retrieval Augmented Generation",
         "snippets": "Z Jiang, F Xu, L Gao - EMNLP, 2023 - aclanthology.org"},
        {"url": "https://proceedings.neurips.cc/paper/2020/hash/6b493230.html",
         "title": "RAG for Knowledge-Intensive NLP Tasks",
         "snippets": "P Lewis, E Perez - NeurIPS, 2020 - proceedings.neurips.cc"},
    ]


def test_serply_backend_reads_articles_not_results(monkeypatch):
    # The Scholar envelope also carries an always-empty "results" list; reading
    # that one instead of "articles" would silently return nothing.
    payload = {"results": [], "articles": [
        {"title": "A", "link": "https://a.org", "description": "d"},
    ]}
    monkeypatch.setattr(B.requests, "get", lambda *a, **k: _Resp(payload))
    items = asyncio.run(SerplyBackend(api_key="k").search("q", 5))
    assert items == [{"url": "https://a.org", "title": "A", "snippets": "d"}]


def test_serply_backend_truncates_to_max_results(monkeypatch):
    payload = {"articles": [
        {"title": f"X{i}", "link": f"https://x{i}.org", "description": ""}
        for i in range(1, 26)
    ]}
    monkeypatch.setattr(B.requests, "get", lambda *a, **k: _Resp(payload))
    items = asyncio.run(SerplyBackend(api_key="k").search("q", 10))
    assert len(items) == 10


def test_serply_backend_missing_title_falls_back(monkeypatch):
    payload = {"articles": [{"link": "https://a.org", "description": "d"}]}
    monkeypatch.setattr(B.requests, "get", lambda *a, **k: _Resp(payload))
    items = asyncio.run(SerplyBackend(api_key="k").search("q", 5))
    assert items[0]["title"] == "No Title"


def test_serply_backend_uses_get_x_api_key(monkeypatch):
    captured = {}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["headers"] = kwargs.get("headers")
        return _Resp({"articles": []})

    monkeypatch.setattr(B.requests, "get", _fake_get)
    asyncio.run(SerplyBackend(api_key="secret").search("hello", 5))
    assert captured["url"] == "https://api.serply.io/v1/scholar"
    assert captured["params"] == {"q": "hello", "num": 5}
    assert captured["headers"]["X-Api-Key"] == "secret"


def test_serply_backend_http_error_raises(monkeypatch):
    # A bad key is a plain HTTP 401 ({"detail": "Invalid API key"}), so the
    # error surfaces through raise_for_status rather than an envelope check.
    monkeypatch.setattr(
        B.requests, "get",
        lambda *a, **k: _Resp({"detail": "Invalid API key"}, status=401),
    )
    try:
        asyncio.run(SerplyBackend(api_key="bad").search("q", 5))
    except RuntimeError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for HTTP 401")


def test_jina_search_backend_parses(monkeypatch):
    payload = {"data": [
        {"url": "https://j.com", "title": "J", "description": "desc"},
    ]}
    monkeypatch.setattr(B.requests, "get", lambda *a, **k: _Resp(payload))
    items = asyncio.run(JinaSearchBackend().search("q", 5))
    assert items == [{"url": "https://j.com", "title": "J", "snippets": "desc"}]


# ── build_search_backends ────────────────────────────────────────────────────

def test_build_search_backends_types():
    sc = SearchConfig(backends=["alphaxiv", "jina", "serper", "serpbase", "serply", "exa"],
                      serper_api_key="s", serpbase_api_key="b",
                      serply_api_key="p", exa_api_key="e")
    backends = build_search_backends(sc)
    names = [b.name for b in backends]
    assert names == ["alphaxiv", "jina", "serper", "serpbase", "serply", "exa"]


# ── WebSearchTool multi-backend fan-out + merge ──────────────────────────────

class _FakeBackend:
    def __init__(self, name, items):
        self.name = name
        self._items = items

    async def search(self, query, max_results):
        return self._items


def test_websearch_tool_merges_across_backends():
    # Same URL from two backends → should merge (support=2 after dedup).
    b1 = _FakeBackend("x", [{"url": "https://shared.com", "title": "S", "snippets": "one"}])
    b2 = _FakeBackend("y", [{"url": "https://shared.com", "title": "S", "snippets": "one"},
                            {"url": "https://only.com", "title": "O", "snippets": "two"}])
    tool = WebSearchTool(cwd=".", backends=[b1, b2])
    out = asyncio.run(tool.execute(query=["q"]))
    assert "shared.com" in out
    assert "only.com" in out


def test_websearch_tool_backend_failure_is_reported():
    class _Boom:
        name = "boom"
        async def search(self, q, n):
            raise RuntimeError("down")
    ok = _FakeBackend("ok", [{"url": "https://ok.com", "title": "K", "snippets": "s"}])
    tool = WebSearchTool(cwd=".", backends=[_Boom(), ok])
    out = asyncio.run(tool.execute(query=["q"]))
    assert "ok.com" in out  # surviving backend still produces results


def test_websearch_tool_requires_source():
    import pytest
    with pytest.raises(ValueError):
        WebSearchTool(cwd=".")  # neither endpoint_url nor backends


# ── factory tool selection ───────────────────────────────────────────────────

def test_factory_alphaxiv_only():
    from arbor.core.tools.web.alphaxiv import AlphaXivSearchTool, AlphaXivVisitTool
    sc = SearchConfig(builtin_backend="alphaxiv")
    st = build_web_search_tool(sc, cwd=".")
    vt = build_web_visit_tool(sc, cwd=".")
    assert isinstance(st, AlphaXivSearchTool)
    assert isinstance(vt, AlphaXivVisitTool)  # alphaXiv-only default preserved


def test_factory_multi_backend_uses_routing_visit():
    from arbor.core.tools.web.keyless_visit import RoutingVisitTool
    sc = SearchConfig(backends=["alphaxiv", "jina"])
    st = build_web_search_tool(sc, cwd=".")
    vt = build_web_visit_tool(sc, cwd=".")
    assert isinstance(st, WebSearchTool)
    assert isinstance(vt, RoutingVisitTool)  # alphaXiv papers + jina general


def test_factory_jina_only_visit():
    from arbor.core.tools.web.keyless_visit import JinaVisitTool
    sc = SearchConfig(backends=["jina"])
    vt = build_web_visit_tool(sc, cwd=".")
    assert isinstance(vt, JinaVisitTool)


def test_factory_browse_endpoint_visit():
    from arbor.core.tools.web.visit import WebVisitTool
    sc = SearchConfig(web_search_endpoint="http://x/s", web_browse_endpoint="http://x/b")
    vt = build_web_visit_tool(sc, cwd=".")
    assert isinstance(vt, WebVisitTool)


def test_factory_no_backend_returns_none():
    sc = SearchConfig()
    assert build_web_search_tool(sc, cwd=".") is None
    assert build_web_visit_tool(sc, cwd=".") is None


# ── Exa MCP backend ──────────────────────────────────────────────────────────

def test_exa_mcp_resolve_and_build():
    sc = SearchConfig(backends=["exa-mcp"], exa_api_key="k")
    assert resolve_backend_names(sc) == ["exa-mcp"]
    backends = build_search_backends(sc)
    assert len(backends) == 1 and backends[0].name == "exa-mcp"


def test_exa_mcp_is_keyless(monkeypatch):
    # The hosted Exa MCP server works without a key — exa-mcp must resolve
    # even when no EXA_API_KEY is set.
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    sc = SearchConfig(backends=["exa-mcp"])
    assert resolve_backend_names(sc) == ["exa-mcp"]
    assert build_search_backends(sc)[0].name == "exa-mcp"


def test_exa_mcp_uses_configured_url():
    sc = SearchConfig(backends=["exa-mcp"], exa_api_key="k", exa_mcp_url="https://my/mcp")
    backend = build_search_backends(sc)[0]
    assert backend._url == "https://my/mcp"


def test_exa_mcp_parse_text_format():
    # The real hosted web_search_exa returns a plain-text block, not JSON.
    text = (
        "Title: Self-Consistency Improves CoT\n"
        "URL: https://arxiv.org/abs/2203.11171\n"
        "Published: 2022-03-21\n"
        "Author: N/A\n"
        "Highlights:\n"
        "Self-consistency samples diverse reasoning paths and votes.\n"
        "It improves arithmetic reasoning.\n"
        "\n"
        "Title: RAG\n"
        "URL: https://en.wikipedia.org/wiki/RAG\n"
        "Published: N/A\n"
        "Author: N/A\n"
        "Highlights:\n"
        "Retrieval augmented generation pulls external docs.\n"
    )
    items = ExaMcpBackend._parse(text)
    assert [i["url"] for i in items] == [
        "https://arxiv.org/abs/2203.11171",
        "https://en.wikipedia.org/wiki/RAG",
    ]
    assert items[0]["title"] == "Self-Consistency Improves CoT"
    assert "diverse reasoning paths" in items[0]["snippets"]
    assert "2022-03-21" in items[0]["snippets"]  # Published kept (not N/A)
    assert "N/A" not in items[1]["snippets"]      # N/A author/date dropped


def test_exa_mcp_parse_results():
    payload = json.dumps({"results": [
        {"url": "https://e.com", "title": "E", "text": "body", "author": "Ann", "publishedDate": "2025"},
        {"url": "https://f.com", "title": "F", "highlights": ["h1", "h2"]},
    ]})
    items = ExaMcpBackend._parse(payload)
    assert items[0]["url"] == "https://e.com"
    assert "body" in items[0]["snippets"] and "Ann" in items[0]["snippets"]
    assert items[1]["url"] == "https://f.com"
    assert "h1" in items[1]["snippets"]


def test_exa_mcp_parse_handles_garbage():
    assert ExaMcpBackend._parse("random prose with no markers") == []
    assert ExaMcpBackend._parse("") == []


def test_exa_mcp_search_parses_tool_output(monkeypatch):
    """search() maps the MCP tool's text output without touching the network."""
    canned = "Title: E\nURL: https://e.com\nPublished: N/A\nAuthor: N/A\nHighlights:\nbody\n"

    async def fake_call(self, query, max_results):
        return canned

    monkeypatch.setattr(ExaMcpBackend, "_call_tool", fake_call)
    items = asyncio.run(ExaMcpBackend().search("q", 5))
    assert items == [{"url": "https://e.com", "title": "E", "snippets": "body"}]

