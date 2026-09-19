"""Pluggable search backends for :class:`WebSearchTool`.

Each backend turns one query into a list of ``{url, title, snippets}`` items —
the exact contract :meth:`WebSearchTool._extract_candidates` already consumes —
so the proven dedup / merge / ranking / formatting pipeline is reused unchanged
and several backends can be fanned out and merged in one search.

Backends:
- ``alphaxiv`` — public alphaXiv paper search (keyless, papers; Python ≥ 3.12).
- ``jina``     — Jina search ``s.jina.ai`` (keyless general web; ``JINA_API_KEY`` optional).
- ``serper``   — Serper Google API (needs ``SERPER_API_KEY``).
- ``serpbase`` — SerpBase Google SERP API (needs ``SERPBASE_API_KEY``).
- ``serply``   — Serply Google Scholar API (papers; needs ``SERPLY_API_KEY``).
- ``exa``      — Exa REST API (needs ``EXA_API_KEY``).
- ``endpoint`` — the legacy self-hosted BrowseComp-style HTTP endpoint.

``resolve_backend_names`` / ``build_search_backends`` read a duck-typed
``SearchConfig`` (attributes only, no import) and drop any backend whose
credentials are missing, so the layer stays decoupled from coordinator config.
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import quote

import requests

_HTTP_TIMEOUT = (5, 30)
_KNOWN = ("alphaxiv", "jina", "serper", "serpbase", "serply", "exa", "exa-mcp", "endpoint")


class SearchBackend(ABC):
    """One query → a list of ``{url, title, snippets}`` items."""

    name: str

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[dict]:
        ...


def _snippet(prefix: str, body: str, limit: int = 300) -> str:
    body = (body or "").strip()
    if len(body) > limit:
        body = body[:limit].rsplit(" ", 1)[0] + "…"
    return f"{prefix} — {body}" if prefix and body else (prefix or body)


# ── HTTP backends (blocking requests run in a worker thread) ───────────────

class _SyncBackend(SearchBackend):
    """Base for blocking-HTTP backends: implement ``_sync``; ``search`` threads it."""

    async def search(self, query: str, max_results: int) -> list[dict]:
        return await asyncio.to_thread(self._sync, query, max_results)

    def _sync(self, query: str, max_results: int) -> list[dict]:  # pragma: no cover
        raise NotImplementedError


class EndpointBackend(_SyncBackend):
    """Legacy self-hosted BrowseComp-style HTTP search endpoint."""

    name = "endpoint"

    def __init__(self, *, endpoint_url: str, provider: str = "google",
                 api_key: str | None = None, timeout: tuple[int, int] = _HTTP_TIMEOUT):
        self._url = endpoint_url
        self._provider = provider
        self._api_key = api_key
        self._timeout = timeout

    def _sync(self, query: str, max_results: int) -> list[dict]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        resp = requests.post(
            self._url,
            json={"query": query, "max_num_results": max_results, "provider": self._provider},
            headers=headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("overall_success", True):
            raise RuntimeError(data.get("error_message") or "endpoint search failed")
        return list(data.get("items", []))


class SerperBackend(_SyncBackend):
    """Serper Google Search API (https://serper.dev) — needs an API key."""

    name = "serper"

    def __init__(self, *, api_key: str, endpoint: str = "https://google.serper.dev/search",
                 timeout: tuple[int, int] = _HTTP_TIMEOUT):
        self._api_key = api_key
        self._url = endpoint
        self._timeout = timeout

    def _sync(self, query: str, max_results: int) -> list[dict]:
        resp = requests.post(
            self._url,
            json={"q": query, "num": max_results},
            headers={"X-API-KEY": self._api_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        items: list[dict] = []
        for o in data.get("organic", []) or []:
            items.append({
                "url": o.get("link", ""),
                "title": o.get("title", ""),
                "snippets": o.get("snippet", ""),
            })
        return items


class SerpBaseBackend(_SyncBackend):
    """SerpBase Google SERP API (https://serpbase.dev) — needs an API key.

    POST-only JSON contract with the key sent as ``X-API-Key``. The response
    is an envelope: ``status`` is 0 on success and ``organic`` holds the
    results (``position`` / ``title`` / ``link`` / ``snippet``). Unlike the
    other HTTP backends this API returns HTTP 200 for business errors, so the
    envelope is checked explicitly instead of relying on ``raise_for_status``.
    """

    name = "serpbase"

    def __init__(self, *, api_key: str,
                 endpoint: str = "https://api.serpbase.dev/google/search",
                 timeout: tuple[int, int] = _HTTP_TIMEOUT):
        self._api_key = api_key
        self._url = endpoint
        self._timeout = timeout

    def _sync(self, query: str, max_results: int) -> list[dict]:
        resp = requests.post(
            self._url,
            json={"q": query, "num": max_results},
            headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", 0) != 0:
            raise RuntimeError(data.get("error") or f"serpbase search failed (status {data.get('status')})")
        items: list[dict] = []
        for o in data.get("organic", []) or []:
            items.append({
                "url": o.get("link", ""),
                "title": o.get("title", ""),
                "snippets": o.get("snippet", ""),
            })
        return items[:max_results]


class SerplyBackend(_SyncBackend):
    """Serply Google Scholar API (https://serply.io) — needs an API key.

    The papers counterpart to ``alphaxiv``: Scholar indexes the published
    record (ACL Anthology, AAAI / NeurIPS proceedings, journals) that an
    arXiv-only backend cannot return, so the two merge into broader prior-art
    coverage for the novelty audit.

    ``GET /v1/scholar`` with the key in an ``X-Api-Key`` header. Results live
    in ``articles`` (``title`` / ``link`` / ``description``); the envelope also
    carries an always-empty ``results`` list, so the array name is passed
    explicitly rather than guessed — keying the wrong one yields zero hits
    silently. ``description`` is the Scholar byline ("Authors - Venue, Year"),
    not an abstract.
    """

    name = "serply"

    def __init__(self, *, api_key: str,
                 endpoint: str = "https://api.serply.io/v1/scholar",
                 timeout: tuple[int, int] = _HTTP_TIMEOUT):
        self._api_key = api_key
        self._url = endpoint
        self._timeout = timeout

    def _sync(self, query: str, max_results: int) -> list[dict]:
        params: dict[str, str | int] = {"q": query, "num": max_results}
        resp = requests.get(
            self._url,
            params=params,
            headers={"X-Api-Key": self._api_key, "User-Agent": "Arbor"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        items: list[dict] = []
        for a in resp.json().get("articles", []) or []:
            items.append({
                "url": a.get("link", ""),
                "title": a.get("title", "") or "No Title",
                "snippets": a.get("description", ""),
            })
        return items[:max_results]


class ExaBackend(_SyncBackend):
    """Exa REST search API (https://exa.ai) — needs an API key."""

    name = "exa"

    def __init__(self, *, api_key: str, endpoint: str = "https://api.exa.ai/search",
                 timeout: tuple[int, int] = _HTTP_TIMEOUT):
        self._api_key = api_key
        self._url = endpoint
        self._timeout = timeout

    def _sync(self, query: str, max_results: int) -> list[dict]:
        resp = requests.post(
            self._url,
            json={
                "query": query,
                "numResults": max_results,
                "type": "auto",
                "contents": {"text": {"maxCharacters": 400}},
            },
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        items: list[dict] = []
        for r in data.get("results", []) or []:
            prefix = " · ".join(p for p in (r.get("author") or "", r.get("publishedDate") or "") if p)
            items.append({
                "url": r.get("url", ""),
                "title": r.get("title", "") or "No Title",
                "snippets": _snippet(prefix, r.get("text") or r.get("summary") or ""),
            })
        return items


class JinaSearchBackend(_SyncBackend):
    """Jina search (https://s.jina.ai) — keyless general web search.

    A ``JINA_API_KEY`` is optional and only raises rate limits.
    """

    name = "jina"

    def __init__(self, *, api_key: str | None = None,
                 endpoint: str = "https://s.jina.ai/", timeout: tuple[int, int] = (5, 45)):
        self._api_key = api_key or os.environ.get("JINA_API_KEY")
        self._url = endpoint
        self._timeout = timeout

    def _sync(self, query: str, max_results: int) -> list[dict]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        resp = requests.get(self._url + "?q=" + quote(query), headers=headers, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data", data) if isinstance(data, dict) else data
        items: list[dict] = []
        for r in (rows or [])[:max_results]:
            if not isinstance(r, dict):
                continue
            items.append({
                "url": r.get("url", ""),
                "title": r.get("title", "") or "No Title",
                "snippets": r.get("description") or (r.get("content") or "")[:300],
            })
        return items


class AlphaXivBackend(SearchBackend):
    """Public alphaXiv paper search via the alphaxiv SDK (keyless, native async)."""

    name = "alphaxiv"

    async def search(self, query: str, max_results: int) -> list[dict]:
        from . import alphaxiv as ax  # respect monkeypatching of ax._load_axv
        AlphaXivClient, AlphaXivError = ax._load_axv()
        try:
            async with AlphaXivClient() as axv:
                papers = await axv.search.papers_rich(query)
        except AlphaXivError as exc:
            raise RuntimeError(f"alphaXiv search error: {exc}") from exc
        return ax.papers_to_items(papers)


class ExaMcpBackend(SearchBackend):
    """Exa search via its hosted **MCP** server (``https://mcp.exa.ai/mcp``).

    Uses the MCP streamable-HTTP client (the same ``mcp`` package the Arbor MCP
    server depends on) to call Exa's ``web_search_exa`` tool. The hosted server
    is **keyless** for basic use; pass an Exa API key (sent as ``x-api-key``) to
    raise limits. Needs the optional ``mcp`` dependency
    (``pip install 'arbor-agent[mcp]'``).
    """

    name = "exa-mcp"
    DEFAULT_URL = "https://mcp.exa.ai/mcp"

    def __init__(self, *, api_key: str | None = None, url: str | None = None,
                 tool: str = "web_search_exa", read_timeout: int = 30):
        self._api_key = api_key
        self._url = url or self.DEFAULT_URL
        self._tool = tool
        self._read_timeout = read_timeout

    async def search(self, query: str, max_results: int) -> list[dict]:
        return self._parse(await self._call_tool(query, max_results))

    async def _call_tool(self, query: str, max_results: int) -> str:
        try:
            from datetime import timedelta

            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError as exc:  # pragma: no cover - exercised via env
            raise RuntimeError(
                "the exa-mcp backend needs the 'mcp' package — install it with "
                "pip install 'arbor-agent[mcp]'"
            ) from exc

        headers = {"x-api-key": self._api_key} if self._api_key else {}
        async with streamablehttp_client(self._url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    self._tool,
                    {"query": query, "numResults": max_results},
                    read_timeout_seconds=timedelta(seconds=self._read_timeout),
                )
        parts = [
            getattr(c, "text", "")
            for c in (getattr(result, "content", None) or [])
            if getattr(c, "text", "")
        ]
        return "\n".join(parts)

    @classmethod
    def _parse(cls, text: str) -> list[dict]:
        """Map Exa MCP's output into search items.

        The hosted ``web_search_exa`` returns a plain-text block (``Title:`` /
        ``URL:`` / ``Published:`` / ``Author:`` / ``Highlights:`` per result);
        some configs may return JSON. Try JSON first, then the text format.
        """
        text = (text or "").strip()
        if not text:
            return []
        items = cls._parse_json(text)
        if items:
            return items
        return cls._parse_text(text)

    @staticmethod
    def _parse_json(text: str) -> list[dict]:
        import json

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        if isinstance(data, dict):
            rows = data.get("results") or data.get("data") or data.get("hits") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        items: list[dict] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            body = r.get("text") or r.get("snippet") or r.get("summary") or ""
            if not body and isinstance(r.get("highlights"), list):
                body = " … ".join(str(h) for h in r["highlights"])
            prefix = " · ".join(
                p for p in (r.get("author") or "", r.get("publishedDate") or "") if p
            )
            items.append({
                "url": r.get("url") or r.get("id") or "",
                "title": r.get("title") or "No Title",
                "snippets": _snippet(prefix, body),
            })
        return items

    @staticmethod
    def _parse_text(text: str) -> list[dict]:
        """Parse the ``Title:/URL:/Published:/Author:/Highlights:`` text block."""
        items: list[dict] = []
        cur: dict | None = None
        hl: list[str] = []
        in_hl = False

        def flush() -> None:
            if cur and cur.get("url"):
                body = " ".join(s.strip() for s in hl if s.strip())
                meta = [
                    v for v in (cur.get("author", ""), cur.get("published", ""))
                    if v and v != "N/A"
                ]
                items.append({
                    "url": cur["url"],
                    "title": cur.get("title") or "No Title",
                    "snippets": _snippet(" · ".join(meta), body),
                })

        for line in text.splitlines():
            s = line.strip()
            if s.startswith("Title:"):
                flush()
                cur, hl, in_hl = {"title": s[6:].strip()}, [], False
            elif cur is None:
                continue
            elif s.startswith("URL:"):
                cur["url"] = s[4:].strip()
                in_hl = False
            elif s.startswith("Published:"):
                cur["published"] = s[10:].strip()
                in_hl = False
            elif s.startswith("Author:"):
                cur["author"] = s[7:].strip()
                in_hl = False
            elif s.startswith("Highlights:"):
                in_hl = True
            elif in_hl:
                hl.append(line)
        flush()
        return items


# ── Resolution from a (duck-typed) SearchConfig ────────────────────────────

def _exa_key(sc: Any) -> str | None:
    return getattr(sc, "exa_api_key", None) or os.environ.get("EXA_API_KEY")


def _serper_key(sc: Any) -> str | None:
    return getattr(sc, "serper_api_key", None) or os.environ.get("SERPER_API_KEY")


def _serpbase_key(sc: Any) -> str | None:
    return getattr(sc, "serpbase_api_key", None) or os.environ.get("SERPBASE_API_KEY")


def _serply_key(sc: Any) -> str | None:
    return getattr(sc, "serply_api_key", None) or os.environ.get("SERPLY_API_KEY")


def resolve_backend_names(sc: Any) -> list[str]:
    """Ordered, de-duplicated list of usable backend names for ``sc``.

    Honors the explicit ``backends`` list; falls back to the legacy
    ``builtin_backend`` / ``web_search_endpoint`` fields when it is empty.
    Drops any backend whose credentials/endpoint are missing.
    """
    raw = [str(n).lower() for n in (getattr(sc, "backends", None) or [])]
    if not raw:
        if getattr(sc, "builtin_backend", "none") == "alphaxiv":
            raw.append("alphaxiv")
        if getattr(sc, "web_search_endpoint", None):
            raw.append("endpoint")

    usable: list[str] = []
    for n in raw:
        if n not in _KNOWN:
            continue
        if n == "endpoint" and not getattr(sc, "web_search_endpoint", None):
            continue
        if n == "serper" and not _serper_key(sc):
            continue
        if n == "serpbase" and not _serpbase_key(sc):
            continue
        if n == "serply" and not _serply_key(sc):
            continue
        if n == "exa" and not _exa_key(sc):
            continue
        # exa-mcp is keyless (the hosted server works without a key); an
        # optional Exa key just raises limits.
        usable.append(n)
    return list(dict.fromkeys(usable))


def build_search_backends(sc: Any) -> list[SearchBackend]:
    """Instantiate the usable backends for ``sc``, in order."""
    out: list[SearchBackend] = []
    for n in resolve_backend_names(sc):
        if n == "alphaxiv":
            out.append(AlphaXivBackend())
        elif n == "jina":
            out.append(JinaSearchBackend(api_key=getattr(sc, "jina_api_key", None)))
        elif n == "serper":
            out.append(SerperBackend(api_key=_serper_key(sc)))
        elif n == "serpbase":
            key = _serpbase_key(sc)
            if key is not None:  # pragma: no cover - resolve_backend_names guards
                out.append(SerpBaseBackend(api_key=key))
        elif n == "serply":
            key = _serply_key(sc)
            if key is not None:  # pragma: no cover - resolve_backend_names guards
                out.append(SerplyBackend(api_key=key))
        elif n == "exa":
            out.append(ExaBackend(api_key=_exa_key(sc)))
        elif n == "exa-mcp":
            out.append(ExaMcpBackend(
                api_key=_exa_key(sc), url=getattr(sc, "exa_mcp_url", None)
            ))
        elif n == "endpoint":
            out.append(EndpointBackend(
                endpoint_url=sc.web_search_endpoint,
                provider=getattr(sc, "web_search_provider", "google"),
                api_key=getattr(sc, "web_search_api_key", None),
            ))
    return out
