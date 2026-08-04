# AROS Principal Loop Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build the provider-neutral AROS core for K/Q/I files, bounded ResearchAttentionPacket, explicit assimilation, mechanical TransitionAudit, temporary-index Git CAS, and clearly labelled cooperative local checkpoints.

**Architecture:** Pure readers first expose semantic files and existing Task/Run/Eval observations without mutation. One Attention service and one TransitionAudit service consume those readers. Checkpoint uses an injected AdmissionGateway, a temporary Git index, and compare-and-swap of the canonical ref; the local human-direct gateway is explicitly cooperative and cannot satisfy mediated commissioning.

**Tech Stack:** Python 3.10+, strict JSON, Markdown/YAML navigation frontmatter, Git plumbing, Typer, existing AROS Task/Run/Eval services, pytest, Ruff.

---

## Authority and constraints

- Highest target specification: AR_OS_AGENT_PRINCIPAL_DESIGN_BOOK_v1_0_zh.md.
- Approved proposed implementation design: docs/superpowers/specs/2026-08-04-aros-principal-research-loop-design.md at commit 1c33237.
- This plan implements only the Arbor-side core. The OpenCode ProContract admission and real mediated commissioning are separate dependent plans.
- Never run uv. Use /workspace/Arbor/.venv/bin/python and /workspace/Arbor/.venv/bin/ruff.
- Never import legacy Coordinator, semantic pipeline, auto_git, or src/core/git_artifacts.py.
- Do not use the dirty aros-m4-hardening worktree.
- Every new production file stays under src/aros; no pyproject package change is needed.
- Before every commit, run the focused tests, Ruff on touched Python files, and git diff --check.

## File boundaries

Create:

- src/aros/research_files.py — pure K/Q/I/frontmatter/section/EvidenceLink parsing.
- src/aros/observations.py — pure Task collection, Run final, and Eval receipt catalog.
- src/aros/attention.py — the sole ResearchAttentionPacket builder and text renderer.
- src/aros/transitions.py — four-field proposal, operational-intent factory, and read-only audit.
- src/aros/checkpoint.py — AdmissionGateway seam, temporary index, commit-tree, CAS, recovery.
- src/aros/checkpoint_bridge.py — pinned JSON-lines prepare/finalize subprocess boundary for a host broker.
- src/aros/transition_index.py — HEAD-bound disposable assimilation cache and explicit rebuild.
- src/aros/research_tool.py — the single native Principal Research tool.
- tests/test_aros_research_files.py
- tests/test_aros_observations.py
- tests/test_aros_attention.py
- tests/test_aros_transitions.py
- tests/test_aros_checkpoint.py
- tests/test_aros_checkpoint_bridge.py
- tests/test_aros_transition_index.py
- tests/test_aros_research_tool.py
- tests/test_aros_transition_cli.py
- tests/test_aros_checkpoint_cli.py

Modify:

- src/aros/workspace.py — templates and boot delegation to Attention.
- src/aros/tasks.py — expose a side-effect-free collection reader.
- src/aros/runs.py — expose strict manifest/final readers already used internally.
- src/aros/eval.py — expose strict receipt plus Eval→Run lineage reader.
- src/aros/worktrees.py — expose the existing pinned Git runner with explicit environment/stdin.
- src/aros/principal.py — replace InspectTool with ResearchTool.
- src/aros/task_tool.py, run_tool.py, eval_tool.py — request service-generated operational intents without carrying admission authority in runners.
- src/aros/runner.py — emit deterministic final-record intent only.
- src/cli/commands/aros_cmd.py — boot JSON, transition audit, checkpoint, rebuild-index routes.
- tests/test_aros_workspace.py
- tests/test_aros_tasks.py
- tests/test_aros_runs.py
- tests/test_aros_eval.py
- tests/test_aros_principal.py
- tests/test_aros_cli.py

## Fixed public contracts

Use these names and fields exactly.

~~~python
@dataclass(frozen=True)
class EvidenceLink:
    observation_ref: str
    relation: Literal["supports", "challenges", "bounds", "context"]
    scope: str


@dataclass(frozen=True)
class EvidenceLinkOccurrence:
    path: str
    anchor: str
    ordinal: int
    link: EvidenceLink
    canonical_sha256: str


@dataclass(frozen=True)
class SemanticDocument:
    path: str
    identifier: str | None
    frontmatter: Mapping[str, object]
    sections: Mapping[str, str]
    evidence_links: tuple[EvidenceLinkOccurrence, ...]
    warnings: tuple[str, ...]
~~~

~~~python
@dataclass(frozen=True)
class ObservationRecord:
    ref: str
    kind: Literal["task_return", "run_final", "measurement", "eval_outcome"]
    record_sha256: str
    versioned_paths: tuple[str, ...]
    candidate_commit: str | None
    measurement_state: str | None
    payload: Mapping[str, object]


class ObservationCatalog:
    def __init__(self, root: Path):
        self.root = root

    def resolve(self, observation_ref: str) -> ObservationRecord:
        return resolve_observation(self.root, observation_ref)

    def enumerate_terminal(self) -> tuple[ObservationRecord, ...]:
        return enumerate_terminal_observations(self.root)
~~~

~~~python
@dataclass(frozen=True)
class AttentionAuthorityContext:
    authority: Mapping[str, object]
    remaining_budget: Mapping[str, object]
    institutional_obligations: tuple[Mapping[str, object], ...]


@dataclass(frozen=True)
class Assimilation:
    observation_ref: str
    affected_paths: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class TransitionProposal:
    schema_version: int
    base_commit: str
    workspace_paths: tuple[str, ...]
    assimilations: tuple[Assimilation, ...]
~~~

~~~python
class AdmissionGateway(Protocol):
    def admit_transition(
        self,
        *,
        candidate_subject_sha256: str,
        audit_payload_sha256: str,
        audit_testimony: Mapping[str, object],
    ) -> bytes:
        ...

    def revalidate_transition(self, admission_receipt: bytes) -> bytes:
        ...
~~~

AdmissionGateway contains host authority. Actor, contract, lease, budget, evaluator policy, and canonical ref never appear in model-controlled tool arguments.

~~~python
@dataclass(frozen=True)
class PreparedCheckpoint:
    transition_id: str
    prepared_ref: str
    candidate_subject_sha256: str
    audit_payload_sha256: str
    audit_testimony: Mapping[str, object]


class CheckpointService:
    def prepare(self, proposal_ref: str, message: str) -> PreparedCheckpoint:
        return self._prepare_locked(proposal_ref, message)

    def finalize(
        self,
        prepared_ref: str,
        admission_receipt: bytes,
        finalize_fence: bytes,
    ) -> dict[str, object]:
        return self._finalize_locked(prepared_ref, admission_receipt, finalize_fence)

    def checkpoint(self, proposal_ref: str, message: str) -> dict[str, object]:
        prepared = self.prepare(proposal_ref, message)
        receipt = self.gateway.admit_transition(
            candidate_subject_sha256=prepared.candidate_subject_sha256,
            audit_payload_sha256=prepared.audit_payload_sha256,
            audit_testimony=prepared.audit_testimony,
        )
        fence = self.gateway.revalidate_transition(receipt)
        return self.finalize(prepared.prepared_ref, receipt, fence)
~~~

CheckpointService is constructed with a candidate workspace root and a broker-owned canonical RepositoryBinding. They may be the same repository in cooperative mode. In mediated mode they are distinct full repositories sharing the audited base commit; the candidate has no canonical remote or credential. ResearchAttentionService receives the same pair: canonical Git supplies admitted semantic/history state, while candidate supplies runtime observations and explicitly separated dirty pending state.

## Task 1: Parse semantic research files

**Files:**

- Create: src/aros/research_files.py
- Create: tests/test_aros_research_files.py
- Modify: src/aros/workspace.py
- Modify: tests/test_aros_workspace.py

- [ ] **Step 1: Write RED tests for exact navigation and EvidenceLinks**

Add tests with these exact names:

~~~python
def test_question_frontmatter_id_must_match_path(tmp_path):
    path = tmp_path / "questions/Q-0001/question.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nid: Q-9999\n---\n# Question\n", encoding="utf-8")
    with pytest.raises(ResearchFileError, match="identifier"):
        read_semantic_document(tmp_path, "questions/Q-0001/question.md")


def test_evidence_link_accepts_exact_three_field_json_line(tmp_path):
    claim = _write_claim(
        tmp_path,
        '{"observation_ref":"eval/evaluations/EVAL-' + "a" * 64 +
        '/receipt.json","relation":"supports","scope":"seed 7"}',
    )
    document = read_semantic_document(tmp_path, claim)
    assert document.evidence_links[0].link.relation == "supports"
    assert document.evidence_links[0].anchor == "Evidence links"


@pytest.mark.parametrize(
    "line",
    [
        '{"observation_ref":"runs/RUN-a/final.json","relation":"supports","scope":"x","extra":1}',
        '{"observation_ref":"runs/RUN-a/final.json","relation":"proves","scope":"x"}',
        '{"observation_ref":"runs/RUN-a/final.json","observation_ref":"x","relation":"supports","scope":"x"}',
    ],
)
def test_evidence_link_rejects_duplicate_unknown_or_invalid_relation(tmp_path, line):
    claim = _write_claim(tmp_path, line)
    with pytest.raises(ResearchFileError):
        read_semantic_document(tmp_path, claim)
~~~

Also add:

- test_missing_recommended_sections_are_warnings
- test_semantic_reader_rejects_symlink_non_utf8_and_escape
- test_frontier_focus_is_optional_and_does_not_hide_other_questions

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_research_files.py
~~~

Expected: collection fails because arbor.aros.research_files does not exist.

- [ ] **Step 3: Implement the pure parser**

Implement:

~~~python
def read_semantic_document(root: Path, relative: str) -> SemanticDocument:
    path = _ordinary_contained_file(root, relative)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ResearchFileError(f"semantic file is not UTF-8: {relative}") from error
    frontmatter, body = _split_frontmatter(text)
    identifier = _validate_navigation_identity(relative, frontmatter)
    sections = _sections(body)
    links = _evidence_links(relative, sections)
    warnings = tuple(
        f"missing recommended section: {heading}"
        for heading in _recommended_sections(relative)
        if heading not in sections
    )
    return SemanticDocument(
        path=relative,
        identifier=identifier,
        frontmatter=frontmatter,
        sections=sections,
        evidence_links=links,
        warnings=warnings,
    )
~~~

Use a local SafeLoader subclass that rejects duplicate YAML keys. Parse each nonblank line under Evidence links and Counterevidence with store._strict_json_loads, require exactly observation_ref/relation/scope, and derive canonical_sha256 with store.json_sha256. Do not interpret JSON-looking prose in any other heading.

- [ ] **Step 4: Extend init templates without inventing research content**

Create directories and only these navigation/template files:

~~~text
questions/FRONTIER.md
model/CURRENT.md
knowledge/claims/
ideas/
memory/decisions/
transitions/
~~~

FRONTIER contains an empty optional focus pointer and explanatory comments. CURRENT contains a heading and no model claim. Do not create Q-0001, C-0001, or I-0001.

- [ ] **Step 5: Verify GREEN and regressions**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_research_files.py tests/test_aros_workspace.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/research_files.py src/aros/workspace.py \
  tests/test_aros_research_files.py tests/test_aros_workspace.py
git diff --check
~~~

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

~~~bash
git add src/aros/research_files.py src/aros/workspace.py \
  tests/test_aros_research_files.py tests/test_aros_workspace.py
git commit -m "feat(aros): parse principal research files"
~~~

## Task 2: Expose pure observation lineage

**Files:**

- Create: src/aros/observations.py
- Create: tests/test_aros_observations.py
- Modify: src/aros/tasks.py
- Modify: src/aros/runs.py
- Modify: src/aros/eval.py
- Modify: tests/test_aros_tasks.py
- Modify: tests/test_aros_runs.py
- Modify: tests/test_aros_eval.py

- [ ] **Step 1: Write side-effect and lineage RED tests**

Add:

~~~python
def test_task_collection_reader_is_side_effect_free(tmp_path, monkeypatch):
    task_id, expected = _committed_collection(tmp_path)
    before = _tree_snapshot(tmp_path)
    monkeypatch.setattr(tasks_module, "atomic_write_json", _unexpected_write)
    observed = read_validated_task_collection(tmp_path, task_id)
    assert observed == expected
    assert _tree_snapshot(tmp_path) == before


def test_eval_linked_run_is_not_a_second_observation(tmp_path):
    receipt = _real_terminal_eval_receipt(tmp_path)
    catalog = ObservationCatalog(tmp_path)
    refs = [record.ref for record in catalog.enumerate_terminal()]
    assert receipt["eval_id"] in refs[0]
    assert f"runs/{receipt['run_id']}/final.json" not in refs


def test_task_and_measurement_joint_closure_requires_same_candidate_commit(tmp_path):
    task = _collection(tmp_path, child_commit="1" * 40)
    measurement = _measurement(tmp_path, candidate_commit="2" * 40)
    with pytest.raises(ObservationError, match="candidate_commit"):
        validate_task_measurement_lineage(task, measurement)
~~~

Also add:

- test_run_final_reader_returns_canonical_record_hash
- test_eval_receipt_requires_full_run_lineage
- test_invalid_or_lost_eval_cannot_resolve_as_measurement
- test_observation_resolve_rejects_runtime_and_path_escape

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_observations.py
~~~

Expected: import failure for observations or missing pure readers.

- [ ] **Step 3: Extract readers without changing service behavior**

Add module-level readers:

~~~python
def read_validated_task_collection(
    root: Path,
    task_id: str,
    *,
    reader: Callable[[Path], object] = read_json_strict_no_repair,
) -> dict[str, object]:
    return _validate_collected_record(root, task_id, reader)


def read_validated_run_manifest(root: Path, run_id: str) -> dict[str, object]:
    return RunService(root)._load_manifest(run_id, reader=read_json_strict_no_repair)


def read_validated_eval_receipt(root: Path, eval_id: str) -> dict[str, object]:
    return EvalService(root).read_validated_receipt(eval_id)
~~~

The Task reader must not construct TaskService, acquire a lock, reconcile status, probe permissions, or write. Extract current validation code into a pure helper and keep TaskService._load_collected delegating to it.

- [ ] **Step 4: Implement ObservationCatalog dispatch**

Resolve only canonical versioned refs:

~~~python
def resolve(self, observation_ref: str) -> ObservationRecord:
    if _TASK_COLLECTION.fullmatch(observation_ref):
        return self._task(observation_ref)
    if _EVAL_RECEIPT.fullmatch(observation_ref):
        return self._eval(observation_ref)
    if _RUN_FINAL.fullmatch(observation_ref):
        return self._run(observation_ref)
    raise ObservationError(f"unsupported observation ref: {observation_ref}")
~~~

For an Eval receipt, include the linked Run manifest/final in versioned_paths but suppress that Run final from enumerate_terminal. kind is measurement only for valid or underpowered; invalid_eval/not_available is eval_outcome.

- [ ] **Step 5: Verify GREEN and existing service suites**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_observations.py tests/test_aros_tasks.py \
  tests/test_aros_runs.py tests/test_aros_eval.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/observations.py src/aros/tasks.py src/aros/runs.py src/aros/eval.py \
  tests/test_aros_observations.py
git diff --check
~~~

Expected: all exit 0; no existing receipt bytes change.

- [ ] **Step 6: Commit**

~~~bash
git add src/aros/observations.py src/aros/tasks.py src/aros/runs.py src/aros/eval.py \
  tests/test_aros_observations.py tests/test_aros_tasks.py tests/test_aros_runs.py tests/test_aros_eval.py
git commit -m "refactor(aros): expose pure observation lineage"
~~~

## Task 3: Build one bounded ResearchAttentionPacket

**Files:**

- Create: src/aros/attention.py
- Create: tests/test_aros_attention.py
- Modify: src/aros/workspace.py
- Modify: tests/test_aros_workspace.py

- [ ] **Step 1: Write exact-shape and non-mutation RED tests**

~~~python
EXPECTED_KEYS = {
    "schema_version",
    "snapshot",
    "active_question",
    "current_uncertainty",
    "recent_evidence_delta",
    "hypotheses",
    "pending_measurements",
    "unassimilated_returns",
    "current_obligations",
    "remaining_budget",
    "blocked_reasons",
    "authority",
    "warnings",
    "omitted",
}


def test_attention_packet_has_exact_top_level_shape(initialized_workspace):
    packet = ResearchAttentionService(initialized_workspace).build()
    assert set(packet) == EXPECTED_KEYS


def test_attention_packet_is_deterministic_and_not_persisted(initialized_workspace):
    before = _tree_snapshot(initialized_workspace)
    first = ResearchAttentionService(initialized_workspace).build()
    second = ResearchAttentionService(initialized_workspace).build()
    assert first == second
    assert _tree_snapshot(initialized_workspace) == before
~~~

Also add:

- test_attention_separates_head_from_dirty_pending_edits
- test_attention_quotes_question_sections_without_summarizing
- test_attention_deduplicates_eval_linked_run
- test_attention_terminal_record_appears_only_in_unassimilated
- test_attention_lost_without_receipt_appears_only_in_pending_measurements
- test_attention_missing_authority_is_unavailable_not_invented
- test_attention_json_and_text_share_one_object
- test_attention_bounds_multibyte_text_and_reports_omissions
- test_attention_exposes_current_and_rival_hypothesis_refs
- test_attention_separates_scientific_and_institutional_obligations
- test_attention_reports_budget_available_unavailable_and_exhausted
- test_attention_preserves_blocker_owning_layers
- test_attention_focus_question_may_be_null
- test_attention_combines_canonical_head_with_candidate_pending_state

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_attention.py
~~~

Expected: import failure for attention.

- [ ] **Step 3: Implement packet building with one ownership path**

~~~python
class ResearchAttentionService:
    def __init__(
        self,
        candidate_root: str | Path,
        *,
        canonical_repository: RepositoryBinding | None = None,
    ):
        self.candidate_root = Path(candidate_root).resolve()
        self.canonical_repository = canonical_repository

    def build(
        self,
        *,
        max_chars: int = 8_000,
        context: AttentionAuthorityContext | None = None,
    ) -> dict[str, object]:
        semantic = self._semantic_attention()
        operational = self._operational_attention()
        packet = self._assemble(semantic, operational, context)
        return _bound_packet(packet, max_chars)

    def render_text(self, packet: Mapping[str, object]) -> str:
        return _render_packet(packet)
~~~

No generation timestamp. Every excerpt carries path, heading, and SHA-256. Lists have stable ordering and caps. Missing authority uses explicit unavailable state. Until Task 6 builds a trusted transition index, terminal observations remain conservatively unassimilated and warnings contains index_incomplete.

In mediated mode semantic excerpts and admitted transition ancestry are read from canonical Git blobs at the canonical ref. Candidate working files are never presented as admitted meaning; their path/hash/diff appears under snapshot pending state. Task/Run/Eval runtime is read from candidate_root.

- [ ] **Step 4: Make boot delegate to the packet**

Change boot_workspace to call build once and render that same object. Add boot_packet(root, max_chars) for CLI JSON. Remove the old independent Run-only section assembly; keep status_workspace for compatibility.

- [ ] **Step 5: Verify GREEN**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_attention.py tests/test_aros_workspace.py tests/test_aros_cli.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/attention.py src/aros/workspace.py \
  tests/test_aros_attention.py tests/test_aros_workspace.py
git diff --check
~~~

- [ ] **Step 6: Commit**

~~~bash
git add src/aros/attention.py src/aros/workspace.py \
  tests/test_aros_attention.py tests/test_aros_workspace.py tests/test_aros_cli.py
git commit -m "feat(aros): build bounded research attention"
~~~

## Task 4: Audit explicit four-field transitions

**Files:**

- Create: src/aros/transitions.py
- Create: tests/test_aros_transitions.py

- [ ] **Step 1: Write strict proposal and audit RED tests**

~~~python
def test_proposal_requires_exact_four_fields_and_directory_identity(repo):
    proposal = _proposal(repo)
    proposal["actor"] = "principal"
    _write_proposal(repo, "T-0001", proposal)
    with pytest.raises(TransitionError, match="fields"):
        TransitionAuditService(repo, canonical_ref="refs/heads/main").audit(
            "transitions/T-0001/proposal.json"
        )


def test_audit_binds_evidence_link_to_same_assimilation(repo):
    measurement = _measurement(repo)
    claim = _claim(repo, observation_ref="runs/RUN-unrelated/final.json")
    proposal = _assimilation(repo, measurement, [claim])
    audit = TransitionAuditService(repo, canonical_ref="refs/heads/main").audit(proposal)
    assert audit["mechanically_valid"] is False
    assert {issue["code"] for issue in audit["issues"]} == {"evidence_ref_mismatch"}
~~~

Also add:

- test_audit_rejects_stale_base_symlink_runtime_and_undeclared_paths
- test_audit_requires_changed_semantic_rationale_path
- test_audit_derives_exact_new_observation_closure
- test_audit_task_measurement_pair_requires_equal_candidate_commit
- test_audit_contains_no_scientific_verdict
- test_audit_writes_nothing_and_is_byte_deterministic
- test_candidate_subject_hash_binds_every_payload_field
- test_missing_recommended_heading_is_warning_not_denial

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_transitions.py
~~~

- [ ] **Step 3: Implement exact proposal parsing**

~~~python
_PROPOSAL_FIELDS = {
    "schema_version",
    "base_commit",
    "workspace_paths",
    "assimilations",
}


def load_transition_proposal(root: Path, proposal_ref: str) -> TransitionProposal:
    transition_id = _transition_id_from_ref(proposal_ref)
    raw = read_json_strict_no_repair(root / proposal_ref)
    if type(raw) is not dict or set(raw) != _PROPOSAL_FIELDS:
        raise TransitionError("transition proposal has invalid fields")
    return _parse_proposal(transition_id, raw)
~~~

Implement deterministic issue records with severity/code/ref/detail. Only error severity makes mechanically_valid false.

- [ ] **Step 4: Implement audit payload and subject hashing**

The payload excludes audit_payload_sha256 and candidate_subject_sha256. Compute:

~~~python
audit_payload_sha256 = json_sha256(payload)
candidate_subject_sha256 = json_sha256(
    {
        "schema_version": 1,
        "transition_id": transition_id,
        "base_commit": proposal.base_commit,
        "workspace": sorted(
            (item["path"], item["owner"], item["blob_oid"])
            for item in path_receipts
        ),
        "observation_closure": sorted(
            (item["path"], item["blob_oid"])
            for item in closure
        ),
        "proposal_blob_sha256": proposal_blob_sha256,
        "audit_payload_sha256": audit_payload_sha256,
    }
)
~~~

The full receipts remain in the audit payload and are therefore already bound by audit_payload_sha256; do not hash a second ad-hoc receipt shape into the subject. Audit must never call file_lock, status/list methods that reconcile, create_json, or any writer.

- [ ] **Step 5: Add deterministic operational-intent factory**

~~~python
def build_operational_proposal(
    *,
    base_commit: str,
    workspace_paths: Sequence[str],
    record_sha256: str,
) -> tuple[str, dict[str, object]]:
    transition_id = f"T-OPS-{base_commit[:12]}-{record_sha256[:12]}"
    return transition_id, {
        "schema_version": 1,
        "base_commit": base_commit,
        "workspace_paths": sorted(set(workspace_paths)),
        "assimilations": [],
    }
~~~

- [ ] **Step 6: Verify GREEN and commit**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_transitions.py
/workspace/Arbor/.venv/bin/ruff check src/aros/transitions.py tests/test_aros_transitions.py
git diff --check
git add src/aros/transitions.py tests/test_aros_transitions.py
git commit -m "feat(aros): audit explicit research transitions"
~~~

## Task 5: Checkpoint with a temporary index and Git CAS

**Files:**

- Create: src/aros/checkpoint.py
- Create: src/aros/checkpoint_bridge.py
- Create: tests/test_aros_checkpoint.py
- Create: tests/test_aros_checkpoint_bridge.py
- Modify: src/aros/worktrees.py

- [ ] **Step 1: Write temporary-index and fault-injection RED tests**

Create a RecordingAdmissionGateway that returns fixed test-only bytes and records the exact subject. Add:

- test_checkpoint_never_uses_user_index_for_candidate_tree
- test_checkpoint_commit_contains_exact_audited_paths_and_receipts
- test_checkpoint_preserves_unrelated_staged_and_unstaged_work
- test_checkpoint_rejects_overlapping_index_or_worktree_drift
- test_checkpoint_cas_failure_leaves_canonical_ref_unchanged
- test_checkpoint_recovery_repairs_only_admitted_index_paths
- test_checkpoint_faults_at_every_durable_point_are_old_or_complete
- test_checkpoint_barrier_writes_marker_then_waits_for_broker_ack
- test_denial_is_runtime_only_and_never_staged
- test_final_tree_is_audited_tree_plus_one_admission
- test_prepare_and_finalize_recheck_the_same_subject
- test_bridge_emits_only_one_admission_request_and_accepts_receipt_on_stdin
- test_bridge_attention_injects_host_authority_into_the_single_packet_builder
- test_distinct_candidate_imports_exact_task_commit_objects_without_updating_a_ref
- test_cas_atomically_publishes_immutable_observation_refs_with_canonical_ref
- test_post_cas_projects_canonical_commit_to_candidate_without_losing_unrelated_dirt
- test_reconcile_finishes_projection_after_post_cas_process_death
- test_task_observation_ref_keeps_return_commit_reachable
- test_eval_observation_ref_keeps_candidate_commit_reachable
- test_allow_then_expired_or_revised_fence_never_reaches_cas
- test_procontract_canonical_json_golden_vector_matches_typescript

Use a parameterized fault hook at after_audit, after_tree, after_allow, after_commit_object, after_cas, and after_index_repair.

The Python golden vector must encode:

~~~text
{"a":"é","nested":{"a":[true,null],"b":2},"z":1}
~~~

as hex 7b2261223a22c3a9222c226e6573746564223a7b2261223a5b747275652c6e756c6c5d2c2262223a327d2c227a223a317d with SHA-256 3d4ef4cab1709da1a1628556cd21d27c5c1c6478d92a03fda97ee98f1236cf44. ProContract camelCase and human-direct snake_case receipts have separate strict decoders.

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_checkpoint.py
~~~

- [ ] **Step 3: Expose the pinned Git runner**

Add a public helper in worktrees.py:

~~~python
def run_git(
    repository: RepositoryBinding,
    *args: str,
    input_bytes: bytes | None = None,
    index_file: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return _git_result(
        repository,
        *args,
        input_bytes=input_bytes,
        index_file=index_file,
    )
~~~

Extend the existing _git_result rather than reconstructing Git invocation. Preserve --no-replace-objects, every _BASE_CONFIGS entry, binary capture, ten-second timeout, and sanitized _git_environment. Add only input_bytes and a validated index_file; index_file must be an exact plain path under the AROS runtime checkpoint directory and is inserted as GIT_INDEX_FILE into the already sanitized environment. No generic extra_environment parameter is allowed.

- [ ] **Step 4: Implement durable candidate preparation**

Implement through commit-tree preparation only:

~~~text
lock
read base/ref/worktree/index snapshot
read-only audit
create temporary index and read-tree base
import exact validated candidate/Task commit objects with fetch --no-tags --no-write-fetch-head when absent
hash exact in-memory bytes and update-index --cacheinfo
materialize audit.json create-once
write candidate tree
persist create-once PreparedCheckpoint
~~~

prepare ends after the audited candidate tree and create-once prepared record; it does not call admission or create a commit object. checkpoint continues through the injected gateway for cooperative use.

- [ ] **Step 5: Verify and commit candidate preparation**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_checkpoint.py -k "prepare or temporary_index or exact_audited"
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/checkpoint.py src/aros/worktrees.py tests/test_aros_checkpoint.py
git diff --check
git add src/aros/checkpoint.py src/aros/worktrees.py tests/test_aros_checkpoint.py
git commit -m "feat(aros): prepare audited checkpoint candidates"
~~~

- [ ] **Step 6: Implement finalize fence and canonical ref transaction**

Continue in this exact order:

~~~text
call AdmissionGateway
materialize admission.json create-once
write final tree = candidate + admission only
commit-tree -p base
rehash bytes/index/ref
call AdmissionGateway.revalidate_transition without another budget charge
validate short-lived finalize fence against receipt/subject/current time
update-ref --stdin transaction: update canonical new old, create exact immutable observation refs
~~~

Use update <canonical> <new> <old> for an existing canonical ref, not create. Observation refs are create-only and part of the same transaction.

- [ ] **Step 7: Verify and commit canonical finalize**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_checkpoint.py -k "finalize or cas or observation_ref or fence"
/workspace/Arbor/.venv/bin/ruff check src/aros/checkpoint.py tests/test_aros_checkpoint.py
git diff --check
git add src/aros/checkpoint.py tests/test_aros_checkpoint.py
git commit -m "feat(aros): finalize checkpoints with git ref cas"
~~~

- [ ] **Step 8: Implement candidate projection, bridge, barriers, and reconcile**

Continue after canonical CAS:

~~~text
import canonical commit into candidate and CAS candidate HEAD/ref
repair only admitted candidate index/worktree paths
write operational event
~~~

CheckpointService.checkpoint accepts only proposal_ref and message. The gateway is constructor-injected. prepare persists a create-once prepared record containing the exact snapshot/tree/message hashes but does not call admission or move a ref. finalize accepts receipt and finalize-fence bytes, verifies their subject/audit/receipt hashes and expiry, rechecks every candidate snapshot, and performs the CAS. The host obtains the fence only through a no-charge revalidation of current contract revision, binding, lease, session, prompt, and attempt.

For distinct repositories, object import may leave unreachable objects on denial/crash but may not create or update any ref. The successful update-ref transaction uses update <canonical> <new> <old> and creates deterministic immutable refs under refs/aros/observations/. A Task ref points to return commit R so B-C-R remains reachable; a separate Eval ref points to candidate commit C when needed. Names and targets are derived from audited observation kind, stable ID, record hash, and validated commit and are included in audit testimony. A conflicting or pre-existing different observation ref aborts the whole transaction.

After canonical CAS, projection imports the canonical commit into candidate without a persistent remote, CASes candidate HEAD/ref from base to new, and resets only admitted index entries. Working bytes already equal admitted blobs; unrelated staged/unstaged bytes remain. If projection is interrupted, canonical admission stands, Attention reports projection_pending, and reconcile completes projection before another mutating operation.

CheckpointBarrier is constructor-injected and defaults to no-op. At each named durable point it fsyncs a create-once marker and waits on a broker-owned control FD. The model cannot select barriers. Unit tests acknowledge the FD; live commissioning observes the marker and sends SIGKILL instead of acknowledgement.

checkpoint_bridge.py is an internal JSON-lines process boundary with three commands:

~~~text
attention -> read exact AttentionAuthorityContext JSON on stdin and return one packet
prepare  -> one PreparedCheckpoint JSON response
finalize -> read exact AdmissionReceipt and FinalizeFence bytes from stdin and return commit result
~~~

It accepts candidate/canonical roots only from broker-owned process arguments, never from model tool input. Attention decodes authority/budget/obligations from broker-only stdin and passes them to ResearchAttentionService; neither TypeScript nor the text renderer patches packet fields. It runs under python -I with a pinned source bootstrap in the mediated plan.

If canonical HEAD already contains the exact transition subject, return the existing commit, finish candidate projection, and repair only admitted index paths/event. If canonical HEAD differs and does not contain it, report stale_base. Never auto-rebase or call Task/Run/Eval.

- [ ] **Step 9: Verify GREEN and commit bridge/recovery**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_checkpoint.py tests/test_aros_checkpoint_bridge.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/checkpoint.py src/aros/checkpoint_bridge.py src/aros/worktrees.py \
  tests/test_aros_checkpoint.py tests/test_aros_checkpoint_bridge.py
git diff --check
git add src/aros/checkpoint.py src/aros/checkpoint_bridge.py src/aros/worktrees.py \
  tests/test_aros_checkpoint.py tests/test_aros_checkpoint_bridge.py
git commit -m "feat(aros): recover checkpoint broker projections"
~~~

## Task 6: Derive assimilation state from admitted Git history

**Files:**

- Create: src/aros/transition_index.py
- Create: tests/test_aros_transition_index.py
- Modify: src/aros/attention.py
- Modify: tests/test_aros_attention.py

- [ ] **Step 1: Write ancestry/cache RED tests**

Add:

- test_only_admitted_ancestral_assimilation_clears_observation
- test_naked_citation_manual_proposal_and_denial_do_not_clear
- test_deleted_stale_or_malformed_cache_redisplays_pending
- test_boot_validation_is_capped_at_256_records
- test_rebuild_index_reconstructs_from_git
- test_recent_evidence_delta_comes_from_latest_transition_with_links
- test_operational_only_transition_does_not_replace_recent_evidence_delta
- test_index_validates_admission_canonical_hash_subject_audit_tree_and_ancestry
- test_index_rejects_valid_receipt_copied_into_an_unrelated_commit

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_transition_index.py tests/test_aros_attention.py
~~~

- [ ] **Step 3: Implement conservative cache semantics**

The cache contains schema_version, head, validated_through, assimilation map, and latest_evidence_transition. Before an assimilation clears anything, validate the receipt codec/canonical hash, subject/audit hashes, proposal/audit/admission blobs in the same commit tree, exact commit ancestry, and immutable observation refs. Boot validates its HEAD binding plus at most 256 newest records. Missing/invalid/incomplete cache returns no cleared observations, index_incomplete warning, and bounded pending pointers. Only explicit rebuild scans all history.

- [ ] **Step 4: Wire the sole packet builder**

Attention calls TransitionIndex once. Terminal records appear only in unassimilated_returns; active/lost-without-record appear only in pending_measurements.

- [ ] **Step 5: Verify GREEN and commit**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_transition_index.py tests/test_aros_attention.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/transition_index.py src/aros/attention.py \
  tests/test_aros_transition_index.py tests/test_aros_attention.py
git diff --check
git add src/aros/transition_index.py src/aros/attention.py \
  tests/test_aros_transition_index.py tests/test_aros_attention.py
git commit -m "feat(aros): derive assimilation state from git"
~~~

## Task 7: Add explicitly cooperative human-direct checkpoints

**Files:**

- Modify: src/aros/checkpoint.py
- Create: tests/test_aros_checkpoint_cli.py
- Modify: src/cli/commands/aros_cmd.py

- [ ] **Step 1: Write RED tests for non-confusable authority**

~~~python
def test_checkpoint_requires_gateway_or_explicit_human_direct(repo):
    result = runner.invoke(aros_app, ["checkpoint", "--cwd", str(repo), "--proposal", PROPOSAL, "--message", "x"])
    assert result.exit_code == 2
    assert "admission" in result.output


def test_human_direct_receipt_is_unambiguously_cooperative(repo):
    result = _checkpoint(repo, "--cooperative-human-direct")
    receipt = _admission_at_head(repo)
    assert receipt["enforcement_class"] == "cooperative"
    assert receipt["issuer"] == "human-direct"
~~~

Also test that the Research tool schema cannot select this route and add test_receipt_codec_never_confuses_human_snake_case_with_procontract_camel_case.

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_checkpoint_cli.py
~~~

- [ ] **Step 3: Implement the separate CLI-only gateway**

HumanDirectGateway is constructed only by the CLI flag. Its exact codec uses schema_version, receipt_kind="human_direct", decision, candidate_subject_sha256, audit_payload_sha256, enforcement_class="cooperative", issuer="human-direct", issued_at, and receipt_sha256. ProContract receipts use their separately versioned camelCase codec. TransitionIndex dispatches on receipt_kind versus ProContract schemaVersion and rejects mixed fields. Human direct never claims ProContract ownership, protected authority, or mediated acceptance. Checkpoint logic remains shared.

- [ ] **Step 4: Verify GREEN and commit**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_checkpoint_cli.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/checkpoint.py src/cli/commands/aros_cmd.py tests/test_aros_checkpoint_cli.py
git diff --check
git add src/aros/checkpoint.py src/cli/commands/aros_cmd.py tests/test_aros_checkpoint_cli.py
git commit -m "feat(aros): label cooperative human checkpoints"
~~~

## Task 8: Expose Research through native CLI and Principal

**Files:**

- Create: src/aros/research_tool.py
- Create: tests/test_aros_research_tool.py
- Create: tests/test_aros_transition_cli.py
- Modify: src/aros/principal.py
- Modify: src/cli/commands/aros_cmd.py
- Modify: tests/test_aros_principal.py
- Modify: tests/test_aros_cli.py

- [ ] **Step 1: Write RED schema and routing tests**

Add:

- test_research_tool_schema_exposes_only_attention_audit_checkpoint
- test_research_tool_has_no_actor_contract_lease_budget_ref_or_human_route_arguments
- test_principal_uses_research_tool_instead_of_separate_inspect
- test_boot_json_emits_exact_attention_packet
- test_transition_audit_cli_is_read_only
- test_audit_rebuild_index_cli_calls_full_rebuild

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_research_tool.py tests/test_aros_transition_cli.py \
  tests/test_aros_cli.py tests/test_aros_principal.py
~~~

- [ ] **Step 3: Implement the one tool**

Use oneOf action schemas. attention has optional max_chars; transition_audit requires proposal_ref; checkpoint requires proposal_ref and message. AdmissionGateway and AttentionAuthorityContext are constructor-injected, never arguments.

~~~python
class ResearchTool(Tool):
    name = "Research"

    async def execute(self, **kwargs: Any) -> str:
        action = kwargs["action"]
        if action == "attention":
            return json.dumps(self.attention.build(max_chars=kwargs.get("max_chars", 8_000)), ensure_ascii=False)
        if action == "transition_audit":
            return json.dumps(self.audit.audit(kwargs["proposal_ref"]), ensure_ascii=False)
        return json.dumps(
            self.checkpoint.checkpoint(kwargs["proposal_ref"], kwargs["message"]),
            ensure_ascii=False,
        )
~~~

- [ ] **Step 4: Add CLI groups**

Add transition Typer group with audit, top-level checkpoint, and audit group with --rebuild-index. boot --json prints the packet; text boot calls the same renderer.

- [ ] **Step 5: Verify GREEN and commit**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_research_tool.py tests/test_aros_transition_cli.py \
  tests/test_aros_checkpoint_cli.py tests/test_aros_cli.py tests/test_aros_principal.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/research_tool.py src/aros/principal.py src/cli/commands/aros_cmd.py \
  tests/test_aros_research_tool.py tests/test_aros_transition_cli.py
git diff --check
git add src/aros/research_tool.py src/aros/principal.py src/cli/commands/aros_cmd.py \
  tests/test_aros_research_tool.py tests/test_aros_transition_cli.py \
  tests/test_aros_cli.py tests/test_aros_principal.py
git commit -m "feat(aros): expose principal research system call"
~~~

## Task 9A: Admit Task service records at safe seams

**Files:**

- Modify: src/aros/tasks.py
- Modify: src/aros/task_tool.py
- Modify: tests/test_aros_tasks.py
- Modify: tests/test_aros_transitions.py

- [ ] **Step 1: Write Task operational-intent RED tests**

Add:

- test_task_create_returns_empty_assimilation_operational_intent
- test_task_create_callback_admits_brief_before_start
- test_task_collect_intent_never_assimilates_return
- test_task_operational_intent_retry_binds_new_base_and_is_idempotent
- test_task_service_never_receives_admission_credential

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_tasks.py tests/test_aros_transitions.py
~~~

- [ ] **Step 3: Return in-memory intents and admit in the foreground tool**

TaskService returns a pure OperationalIntent envelope alongside the unchanged brief/collection record; it does not materialize proposal.json or call Git. TaskTool's injected callback materializes the base-bound proposal and completes operational checkpoint before returning from create, so the existing clean committed-parent start gate remains valid. collect may return admission_required or synchronously admit through the same callback. Without a callback, records remain current cooperative behavior and the result exposes admission_required.

- [ ] **Step 4: Verify and commit**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_tasks.py tests/test_aros_transitions.py tests/test_aros_checkpoint.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/tasks.py src/aros/task_tool.py \
  tests/test_aros_tasks.py tests/test_aros_transitions.py
git diff --check
git add src/aros/tasks.py src/aros/task_tool.py \
  tests/test_aros_tasks.py tests/test_aros_transitions.py
git commit -m "feat(aros): admit task service records"
~~~

## Task 9B: Admit Run and Eval records without blocking execution

**Files:**

- Modify: src/aros/runs.py
- Modify: src/aros/runner.py
- Modify: src/aros/eval.py
- Modify: src/aros/run_tool.py
- Modify: src/aros/eval_tool.py
- Modify: tests/test_aros_runs.py
- Modify: tests/test_aros_eval.py
- Modify: tests/test_aros_transitions.py

- [ ] **Step 1: Write Run/Eval seam RED tests**

Add:

- test_run_prepare_does_not_materialize_transition_dirt_before_start
- test_eval_prepare_bundle_can_start_with_only_validated_run_artifact_dirt
- test_run_terminal_event_carries_in_memory_operational_intent_data
- test_eval_receipt_intent_includes_exact_run_lineage
- test_operational_checkpoint_never_clears_unassimilated_return
- test_runner_never_receives_admission_gateway_credential_or_proposal_writer

- [ ] **Step 2: Run and verify RED**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_runs.py tests/test_aros_eval.py tests/test_aros_transitions.py
~~~

- [ ] **Step 3: Keep internal Eval prepare→start clean**

RunService and EvalService build OperationalIntent values in memory. EvalService.run may not materialize a transition between prepare_bundle and Run start. The foreground Eval tool materializes/admit intents only after the linked Run has safely started or the terminal receipt returns. runner.py writes only its existing service-owned final/event facts; a host reconstructs deterministic intent from those records after wake-up.

- [ ] **Step 4: Verify and commit**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_runs.py tests/test_aros_eval.py \
  tests/test_aros_transitions.py tests/test_aros_checkpoint.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/runs.py src/aros/runner.py src/aros/eval.py \
  src/aros/run_tool.py src/aros/eval_tool.py
git diff --check
git add src/aros/runs.py src/aros/runner.py src/aros/eval.py \
  src/aros/run_tool.py src/aros/eval_tool.py \
  tests/test_aros_runs.py tests/test_aros_eval.py tests/test_aros_transitions.py
git commit -m "feat(aros): admit run and eval records at safe seams"
~~~

## Task 10: Core commissioning and full regression

**Files:**

- Create: docs/analysis/aros-principal-loop-core-smoke.md
- Modify: docs/aros/README.md
- Modify: memory/NOW.md

- [ ] **Step 1: Run focused AROS suite**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_research_files.py tests/test_aros_observations.py \
  tests/test_aros_attention.py tests/test_aros_transitions.py \
  tests/test_aros_checkpoint.py tests/test_aros_transition_index.py \
  tests/test_aros_research_tool.py tests/test_aros_transition_cli.py \
  tests/test_aros_checkpoint_cli.py
~~~

Expected: all pass.

- [ ] **Step 2: Run architecture/public gates**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_architecture_boundary.py tests/test_aros_public_entry.py \
  tests/test_document_registry.py
/workspace/Arbor/.venv/bin/ruff check src/aros tests/test_aros_*.py
git diff --check
~~~

Expected: all pass and no legacy path growth.

- [ ] **Step 3: Run the complete repository suite**

~~~bash
/workspace/Arbor/.venv/bin/python -m pytest -q
~~~

Expected: all existing and new tests pass. Record exact pass/skip count and duration.

- [ ] **Step 4: Run a real cooperative restart smoke**

In a fresh temporary Git repo:

1. aros init.
2. create one real standalone Run/Eval receipt using current services.
3. edit one Claim with a strict EvidenceLink.
4. author two minimal assimilations when a Task return is also present.
5. checkpoint with --cooperative-human-direct.
6. remove .aros indexes and the first Principal runtime.
7. run aros boot --json in a fresh process.

Record exact commands, commits, receipt hashes, packet size, and explicit enforcement_class=cooperative. Do not call this mediated or protected.

- [ ] **Step 5: Write evidence and current capability docs**

Document what is proven and explicitly list what remains for the dependent OpenCode plan: ProContract-owned AdmissionReceipt, host fence/budget, candidate authority domain, context-epoch injection, and mediated E2E.

- [ ] **Step 6: Commit**

~~~bash
git add docs/analysis/aros-principal-loop-core-smoke.md docs/aros/README.md memory/NOW.md
git commit -m "docs(aros): commission principal loop core"
~~~

## Core plan completion gate

This plan is complete only when:

- every focused/full command above passes from a clean worktree;
- a transcript-free restart derives the exact bounded packet;
- naked citations/manual proposals/ACKs do not clear observations;
- real Task/Run/Eval readers remain side-effect free;
- checkpoint fault injection proves old HEAD or one complete commit;
- cooperative mode is labelled everywhere;
- no result is presented as mediated ProContract authority.

Do not merge this branch or mark the overall AROS goal complete. Proceed to the OpenCode ProContract admission plan.
