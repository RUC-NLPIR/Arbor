---
name: aros-source-research
source_ids:
  - source-1
  - source-2
input: ResearchQuestion
output: SourcePacket
tools:
  - Source.read
  - Source.search
---

## Purpose

Build an auditable evidence packet for a research question without turning search
results into a scientific decision.

## Inputs

Read one `ResearchQuestion`, including its `question_ref`, `scope`, and
`decision_context`. Keep the question and its decision boundary unchanged.

## Method

1. Prefer primary sources; use secondary sources only to locate or contextualize
   primary evidence, and label them as secondary.
2. Query multiple independent sources and preserve the exact query log, including
   every query formulation and retrieval result.
3. Retain dead ends and contradictions, including unsuccessful queries and evidence
   that disagrees with another source.
4. For every source used, bind its opaque source id, retrieval time, content
   reference, and SHA-256 content hash; keep citations traceable to those bindings.
5. Treat novelty findings as evidence only, never as a scientific verdict; report
   what the search did and did not establish.
6. State search, access, coverage, and source-quality limitations explicitly.

## Output

- Artifact: Return exactly one `SourcePacket`.
- Required fields: `query`, `question_ref`, `sources`, `retrieved_at`, `content_refs`,
  `content_sha256s`, `limitations`.
- Lineage: Copy input `question_ref` unchanged to output `question_ref`.
- Evidence binding: Bind every factual statement to a cited content reference or mark
  it as unresolved.

## Completion

- Complete only when a bound `SourcePacket` preserves the input `question_ref`
  unchanged, the exact query log, retained dead ends and contradictions, content
  bindings, and explicit limitations needed for later inspection.

## Forbidden

- Do not download experimental data.
- Do not fabricate citations.
- Do not write to external systems or perform any external write.
- Do not make scientific acceptance decisions.
- Do not issue scientific verdicts.
- Do not execute experiments; experimental execution is outside this procedure.
