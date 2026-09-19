# Search & External Knowledge

Real research starts from what's already known. Arbor can pull in external
knowledge — literature and the open web — in **two independent lanes**:

| Lane | When | What it does | Lands on | Default |
| --- | --- | --- | --- | --- |
| **Grounded ideation** | *during* IDEATE | the coordinator searches to **inform a new idea** | `node.grounding` | **off** |
| **Novelty audit** | *after* an experiment | a SearchAgent checks **prior art** for a proven idea | `node.related_work` | off |

The two lanes run **independent searches and never share fetched text** — so the
same page can't both inspire an idea and certify it as novel. Both are **off by
default**, which keeps benchmark runs fair: the system can't crib a finished
idea or result off the web. Turn them on for real research, where reading
related work first is the whole point.

All of this is configured under the `search:` block — see
[Configuration](configuration.md#search) for the full field list.

---

## Grounded ideation

Enable it with:

```yaml
search:
  enabled: true
  grounded_ideation: true
  backends: [alphaxiv, jina]   # keyless: papers + general web
```

The coordinator then gets a **`ResearchSearch`** tool it can call while forming
ideas. It is an *optional* input — ideas can still come from experiment results
or the model's own reasoning — used when external knowledge would actually help.
Set an `intent` to shape the search:

| intent | use it for |
| --- | --- |
| `related_work` | you have a draft idea — find prior work and assess overlap / gaps |
| `survey` | organize how a field/problem is currently solved (approaches + trade-offs) |
| `lookup` | a specific fact: a method detail, a dataset, a benchmark number |
| `explore` | open-ended scan of a direction for unexplored angles |

The tool runs in an **isolated context** (verbose search results and page text
never enter the coordinator's window) and returns a compact digest: a summary,
findings, and numbered sources. Sources that were never actually opened are
dropped, so citations stay honest.

When a returned source genuinely shaped an idea, the coordinator records it on
the node's **`grounding`** field — visible in the Idea Tree and the report,
separate from the novelty audit's `related_work`.

---

## Novelty audit

The audit surveys **prior art for an idea that already proved out**, so you know
whether a win is also a contribution before merging it to trunk.

```yaml
search:
  enabled: true
  auto_search_on_add: true     # check every new idea before running it
```

With `auto_search_on_add: true`, every idea added to the tree gets a
pre-experiment novelty check whose verdict lands on the node's `related_work`
field (advisory — it never blocks a run). A dedicated SearchAgent does its own
fresh search, so it can't be fooled by whatever grounded ideation read earlier.

For a one-off check outside a run:

```bash
arbor idea-check "your hypothesis in one sentence"
```

See [`arbor idea-check`](cli.md#arbor-idea-check) for options.

---

## Search backends

`search.backends` is an **ordered list**; results from every backend are merged
and de-duplicated, so you can combine sources (e.g. papers *and* general web,
the same paper from two backends merges to one).

| backend | needs a key? | covers |
| --- | --- | --- |
| `alphaxiv` | no | arXiv / alphaXiv papers (Python ≥ 3.12) |
| `jina` | no (optional `JINA_API_KEY` raises limits) | general web (s.jina.ai) |
| `serper` | `SERPER_API_KEY` | Google results (serper.dev) |
| `serpbase` | `SERPBASE_API_KEY` | Google SERP (serpbase.dev, POST + X-API-Key) |
| `serply` | `SERPLY_API_KEY` | Google Scholar papers ([serply.io](https://serply.io), GET + X-Api-Key) |
| `exa` | `EXA_API_KEY` | neural web search (exa.ai REST) |
| `exa-mcp` | no (optional `EXA_API_KEY` raises limits) | Exa via its hosted MCP server |
| `endpoint` | optional | self-hosted `web_search_endpoint` (BrowseComp-style) |

A backend whose key is missing is **silently skipped**, so a list like
`[alphaxiv, jina, serper]` degrades gracefully to the keyless two when no
`SERPER_API_KEY` is set. The fully keyless default is
**`backends: [alphaxiv, jina]`** — papers + general web, zero setup.

Keys go in the config file (`serper_api_key` / `serpbase_api_key` /
`serply_api_key` / `exa_api_key` / `jina_api_key`) or the matching env vars
(`SERPER_API_KEY`, `SERPBASE_API_KEY`, `SERPLY_API_KEY`, `EXA_API_KEY`,
`JINA_API_KEY`).

!!! note "Two papers backends"
    `alphaxiv` and `serply` both cover papers but not the same corpus:
    alphaXiv is arXiv preprints, while Serply queries Google Scholar, which
    also indexes venue proceedings and journals (ACL Anthology, AAAI,
    NeurIPS) that never appear on arXiv. Listing both —
    `backends: [alphaxiv, serply]` — merges and de-duplicates them, which
    widens prior-art coverage for the novelty audit. Note that Scholar's
    `description` is the byline ("Authors - Venue, Year"), not an abstract.

!!! note "Exa via MCP"
    The `exa-mcp` backend calls Exa's hosted MCP server
    (`https://mcp.exa.ai/mcp`; override the URL with `exa_mcp_url`). The hosted
    server is **keyless** for basic use — set `exa_api_key` only to raise limits
    (sent as the `x-api-key` header). It needs the MCP client:
    `pip install 'arbor-agent[mcp]'`. The plain `exa` backend hits the same
    provider over its REST API (which *does* need a key) — pick whichever fits.

---

## Visiting pages

Reading a page is keyless too. `search.visit_backend: auto` (the default):

- alphaXiv paper URLs → the alphaXiv SDK (full text), and
- any other URL → the **Jina reader** (`r.jina.ai`, clean markdown, no key),
  falling back to a raw `requests` fetch. **PDFs are read too** — the Jina
  reader extracts PDF text, and the raw fallback parses PDFs via `pypdf`.

So no browse endpoint or API key is needed to open a page. Force a single
fetcher with `visit_backend: jina | requests | alphaxiv | endpoint`.

**Reading depth (full text vs. abstract).** Page text is truncated to a token
budget. The novelty-audit lane uses `visit_max_content_tokens` (default 2048 —
abstracts are enough to judge prior art). The grounded-ideation lane reads for
depth and uses the larger `research_visit_tokens` (default 6000) so a paper's
**method / results sections survive truncation**, not just the abstract. Raise
either for deeper reads (at higher token cost).


---

## Backward compatibility

Older configs keep working unchanged. When `backends` is empty, Arbor maps the
legacy fields automatically: `builtin_backend: alphaxiv` → `[alphaxiv]`, and a
set `web_search_endpoint` → `[endpoint]` (with `web_browse_endpoint` used for
visits).

## What's active?

When a run starts, the coordinator logs the resolved configuration once, e.g.:

```
search enabled — backends: alphaxiv, jina | visit: auto | grounded_ideation: on
```

so you can confirm which backends are live and whether grounded ideation is on.

## See also

- [Configuration](configuration.md#search) — the full `search:` field list.
- [`arbor idea-check`](cli.md#arbor-idea-check) — one-off novelty check.
