from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import py_compile
import re
import shutil
import signal
import subprocess
import time
from dataclasses import FrozenInstanceError
from pathlib import Path, PurePosixPath
from types import MappingProxyType

import pytest


ROOT = Path(__file__).resolve().parent.parent
PROGRAM_ROOT = ROOT / "commissioning/research_program"
SOURCES_PATH = PROGRAM_ROOT / "SOURCES.json"
CONTRACTS_PATH = PROGRAM_ROOT / "contracts/procedure_contracts.json"
PROCEDURES_ROOT = PROGRAM_ROOT / "procedures"
UPSTREAM_PRODUCT_NAMES = ("claude", "gemini")
SOURCE_RECORD_SUFFIXES = {".json", ".jsonl", ".yaml", ".yml", ".md"}
SOURCE_RECORD_EXEMPTIONS = {
    "SOURCES.json",
    "procedures/aros-source-research.md",
}
RUNTIME_SOURCE_SUFFIXES = {".json", ".md", ".py"}
RUNTIME_ARTIFACT_PARTS = {"__pycache__", "build"}
APPROVED_SOURCE_RECORD = {
    "schema_version": 1,
    "sources": [
        {
            "id": "source-1",
            "repository": "/workspace/Auto-claude-code-research-in-sleep",
            "commit": "df729a3f942e4a97646d212eb8aee1144ab5e31b",
            "license": "MIT",
            "selected_paths": [
                "skills/research-lit/SKILL.md",
                "skills/novelty-check/SKILL.md",
                "skills/citation-audit/SKILL.md",
                "skills/idea-creator/SKILL.md",
                "skills/research-refine/SKILL.md",
                "skills/experiment-plan/SKILL.md",
                "skills/ablation-planner/SKILL.md",
                "skills/analyze-results/SKILL.md",
                "skills/research-wiki/SKILL.md",
                "skills/research-review/SKILL.md",
                "skills/experiment-audit/SKILL.md",
                "skills/integrity-forensics/SKILL.md",
                "skills/result-to-claim/SKILL.md",
                "skills/claims-drafting/SKILL.md",
                "skills/shared-references/external-cadence.md",
                "skills/shared-references/reviewer-independence.md",
                "mcp-servers/claude-review/server.py",
                "mcp-servers/gemini-review/server.py",
                "mcp-servers/manual-review/server.py",
            ],
            "adaptation": (
                "Distill scientific procedures, durable recovery, cadence, and fresh "
                "review; remove scoring, paper production, remote execution, and "
                "duplicate orchestration."
            ),
        },
        {
            "id": "source-2",
            "repository": (
                "/workspace/Arbor/.worktree/aros-long-running-research-program-design"
            ),
            "commit": "e9c58c998767dd87bdea99a727533819850ac281",
            "license": "Apache-2.0",
            "selected_paths": [
                "skills/arbor-agent-setup-intake/SKILL.md",
                "skills/arbor-agent-ideate/SKILL.md",
                "skills/arbor-agent-executor/SKILL.md",
                "skills/arbor-agent-search/SKILL.md",
                "skills/arbor-agent-resume-report/SKILL.md",
                "skills/arbor-agent-tools/SKILL.md",
                "src/mcp/server.py",
                "src/mcp/session_ops.py",
            ],
            "adaptation": (
                "Distill mechanism framing, deterministic tool boundaries, search, and "
                "durable handoff; remove tree authority, scalar evaluation, merge gates, "
                "and duplicate session state."
            ),
        },
    ],
}
EXPECTED_ALLOWED_TOOLS = (
    "Source.read",
    "Source.search",
    "Task.create",
    "Task.start",
    "Task.status",
    "Task.collect",
    "Run.request",
    "Run.status",
    "Eval.run",
    "Receipt.read",
    "Research.observe",
    "Research.checkpoint",
    "Research.petition",
    "Git.read",
)
EXPECTED_ARTIFACTS = {
    "ResearchQuestion": ("question_ref", "scope", "decision_context"),
    "SourcePacket": (
        "query",
        "question_ref",
        "sources",
        "retrieved_at",
        "content_refs",
        "content_sha256s",
        "limitations",
    ),
    "RivalMechanismSet": (
        "root_question_ref",
        "mechanisms",
        "predictions",
        "falsifiers",
        "conflicts",
        "remaining_uncertainty",
    ),
    "ExperimentProposal": (
        "mechanism_refs",
        "decision_uncertainty",
        "prediction",
        "falsifier",
        "controls",
        "run_request",
        "expected_information_gain",
        "cost_bound",
    ),
    "RunEvidence": (
        "run_ref",
        "eval_refs",
        "raw_refs",
        "process_state",
        "budget_used",
        "rival_mechanism_set_ref",
        "mechanism_refs",
        "experiment_proposal_ref",
        "prediction_ref",
        "falsifier_ref",
        "preregistration_ref",
    ),
    "ObservationUpdate": (
        "evidence_refs",
        "classifications",
        "strengthened",
        "weakened",
        "eliminated",
        "counterexamples",
        "negative_results",
        "remaining_uncertainty",
        "next_action_rationale",
    ),
    "Preregistration": (
        "mechanism_hypothesis",
        "key_predictions",
        "falsifiers",
        "controls",
        "primary_comparisons",
        "transfer_prediction",
        "stopping_rules",
        "evaluator_version",
        "candidate_commit",
        "data_manifest_refs",
        "environment_sha256",
        "output_schema_sha256",
        "analysis_boundaries",
        "rerun_rules",
        "experiment_proposal_ref",
        "mechanism_refs",
        "prediction_ref",
        "falsifier_ref",
    ),
    "FrozenEvidencePacket": (
        "task_brief_ref",
        "preregistration_ref",
        "candidate_commit",
        "source_refs",
        "raw_refs",
        "reproduction_ref",
        "root_question_ref",
        "claim_draft_ref",
        "researcher_model_id",
        "researcher_model_family",
        "researcher_session_refs",
        "researcher_worktree_ref",
        "packet_sha256",
        "review_session_receipt_ref",
        "reviewer_session_id",
    ),
    "ReviewerReport": (
        "reproduction_refs",
        "alternative_explanations",
        "leakage_findings",
        "statistical_findings",
        "scope_objections",
        "fatal_objections",
        "unresolved_objections",
        "reviewer_model_id",
        "reviewer_model_family",
        "independence_evidence_ref",
        "packet_sha256",
        "claim_draft_ref",
        "candidate_commit",
        "reviewer_session_id",
    ),
    "AdjudicatedEvidence": (
        "claim_draft_ref",
        "evidence_refs",
        "review_ref",
        "principal_response_ref",
        "root_question_ref",
        "candidate_commit",
        "preregistration_ref",
        "reproduction_ref",
        "principal_actor_ref",
        "principal_checkpoint_ref",
        "disposition",
        "principal_decision_receipt_ref",
        "authority_class",
        "principal_authority_ref",
        "checkpoint_reservation_ref",
    ),
    "ClaimPackage": (
        "claim",
        "scope",
        "evidence_refs",
        "counterevidence",
        "reproduction_commands",
        "limitations",
        "remaining_uncertainty",
        "review_objections",
        "disposition",
        "root_question_ref",
        "candidate_commit",
        "preregistration_ref",
        "review_ref",
        "principal_response_ref",
        "reproduction_ref",
        "environment_ref",
        "checkpoint_ref",
        "principal_decision_receipt_ref",
        "authority_class",
        "principal_authority_ref",
    ),
}
EXPECTED_PROCEDURES = {
    "aros-source-research": (
        "ResearchQuestion",
        "SourcePacket",
        ("Source.read", "Source.search"),
    ),
    "aros-rival-mechanisms": (
        "SourcePacket",
        "RivalMechanismSet",
        ("Git.read", "Receipt.read", "Research.observe", "Research.petition"),
    ),
    "aros-experiment-design": (
        "RivalMechanismSet",
        "ExperimentProposal",
        ("Receipt.read", "Research.observe", "Research.petition"),
    ),
    "aros-evidence-update": (
        "RunEvidence",
        "ObservationUpdate",
        (
            "Run.status",
            "Receipt.read",
            "Git.read",
            "Research.observe",
            "Research.checkpoint",
        ),
    ),
    "aros-independent-review": (
        "FrozenEvidencePacket",
        "ReviewerReport",
        (
            "Source.read",
            "Run.request",
            "Run.status",
            "Eval.run",
            "Receipt.read",
            "Git.read",
            "Research.petition",
        ),
    ),
    "aros-claim-package": (
        "AdjudicatedEvidence",
        "ClaimPackage",
        ("Source.read", "Receipt.read", "Git.read", "Research.checkpoint"),
    ),
}
APPROVED_PROCEDURE_SHA256 = {
    "aros-claim-package": "b6f661a42c2e18aca8cf0a1a2a49956bf04c7c166ad6f24f9aaee0e00c39737e",
    "aros-evidence-update": "2d28d7003eea2b11efafcfce8c96fef292fbc9a7a3a54ba31771952b875d7776",
    "aros-experiment-design": "a40ff958ebe3fd87ed88b869ade75063f09d6e96969761911ae447057a27727c",
    "aros-independent-review": "7eac1ab5f835a0a630b796103a4b767db35a1a0cc3d6ad51cf83c1625a548e06",
    "aros-rival-mechanisms": "d38f66fe7631900769b5d7a37595e1522c5275e01fe5b50e56e609fea54cd989",
    "aros-source-research": "2e9aa3126854944234ad7e99550918fc91f228b1da780bbd87550681184b9dec",
}
EXPECTED_PROCEDURE_HEADINGS = (
    "Purpose",
    "Inputs",
    "Method",
    "Output",
    "Completion",
    "Forbidden",
)
EXPECTED_SOURCE_METHOD_RULES = (
    (
        "Prefer primary sources; use secondary sources only to locate or "
        "contextualize primary evidence, and label them as secondary."
    ),
    (
        "Query multiple independent sources and preserve the exact query log, "
        "including every query formulation and retrieval result."
    ),
    (
        "Retain dead ends and contradictions, including unsuccessful queries and "
        "evidence that disagrees with another source."
    ),
    (
        "For every source used, bind its opaque source id, retrieval time, content "
        "reference, and SHA-256 content hash; keep citations traceable to those "
        "bindings."
    ),
    (
        "Treat novelty findings as evidence only, never as a scientific verdict; "
        "report what the search did and did not establish."
    ),
    "State search, access, coverage, and source-quality limitations explicitly.",
)
EXPECTED_SOURCE_FORBIDDEN_RULES = (
    "Do not download experimental data.",
    "Do not fabricate citations.",
    "Do not write to external systems or perform any external write.",
    "Do not make scientific acceptance decisions.",
    "Do not issue scientific verdicts.",
    "Do not execute experiments; experimental execution is outside this procedure.",
)
EXPECTED_SOURCE_COMPLETION_RULES = (
    (
        "Complete only when a bound `SourcePacket` preserves the input "
        "`question_ref` unchanged, the exact query log, retained dead ends and "
        "contradictions, content bindings, and explicit limitations needed for "
        "later inspection."
    ),
)
EXPECTED_RIVAL_METHOD_RULES = (
    (
        "Produce at least two independently formed falsifiable causal mechanisms; "
        "derive each alternative on its own terms before comparing it with the "
        "others."
    ),
    (
        "Apply this priority: mechanism compression before literature novelty, and "
        "literature novelty before impact."
    ),
    (
        "For each mechanism, state its prediction, distinguishing observation, "
        "falsifier, scope, and conflicts with bound evidence or other mechanisms."
    ),
    (
        "Record explicit remaining uncertainty, including uncertainty shared by "
        "every rival, after comparing the alternatives against the same evidence."
    ),
    (
        "Identify observations that could discriminate between rivals; this "
        "procedure does not choose an experiment yet."
    ),
)
EXPECTED_RIVAL_FORBIDDEN_RULES = (
    "Do not rank mechanisms by pilot score or use pilot-score ranking.",
    "Do not select a top winner.",
    "Do not retain unfalsifiable mechanisms.",
    "Do not choose an experiment yet.",
    "Do not call any `Source.*` tool directly.",
)
EXPECTED_RIVAL_COMPLETION_RULES = (
    "Complete only with at least two surviving rivals.",
    (
        "Every surviving rival must have at least one discriminating observation "
        "and a stated falsifier."
    ),
    "If fewer than two rivals survive, do not emit a `RivalMechanismSet`.",
    (
        "Call `Research.observe` with the missing distinguishing evidence, then call "
        "`Research.petition` to request a new `SourcePacket`."
    ),
    "Exit incomplete after those calls; never complete this procedure.",
)
EXPECTED_EXPERIMENT_INPUT_RULES = (
    "Artifact: Read exactly one `RivalMechanismSet`.",
    (
        "Required fields: `root_question_ref`, `mechanisms`, `predictions`, "
        "`falsifiers`, `conflicts`, `remaining_uncertainty`."
    ),
    (
        "Evidence binding: Use `Receipt.read` to inspect the bound evidence for "
        "the surviving rivals before designing a proposal."
    ),
)
EXPECTED_EXPERIMENT_METHOD_RULES = (
    (
        "Begin from first principles: compress each rival into the smallest "
        "causal mechanism that explains its bound evidence before proposing an "
        "intervention."
    ),
    (
        "Identify one explicit `decision_uncertainty` that separates the "
        "surviving rivals or determines that they require revision."
    ),
    (
        "For each candidate, state one discriminating prediction, its falsifier, "
        "controls, primary comparisons, transfer test, stopping rules, rerun "
        "plan, cache-isolation plan, and exact evaluator binding in the proposed "
        "`run_request`."
    ),
    (
        "Apply the gates lexicographically and in this exact order: essentiality "
        "first, falsifiability second, and decision relevance third. Reject every "
        "candidate that fails any gate before comparing expected information "
        "gain, cost, or concurrency."
    ),
    (
        "Among candidates that pass all three gates, maximize expected "
        "information gain per cost, with every estimate bound to cited evidence "
        "and uncertainty."
    ),
    (
        "Consider concurrency only after all three gates pass and only when "
        "parallel proposals improve coverage; concurrency must not change the "
        "lexicographic choice."
    ),
)
EXPECTED_EXPERIMENT_COMPLETION_RULES = (
    (
        "Complete only with one proposal that passes all three gates, maximizes "
        "expected information gain per cost among the passing candidates, and "
        "contains every required design binding."
    ),
    "If no candidate passes every gate, do not emit an `ExperimentProposal`.",
    (
        "Call `Research.observe` with the unresolved gate failure and missing "
        "evidence, then call `Research.petition` to request the new evidence "
        "needed to form a valid proposal."
    ),
    "Exit incomplete after those calls; never complete this procedure.",
)
EXPECTED_EXPERIMENT_FORBIDDEN_RULES = (
    (
        "Do not execute an experiment or emit an executable command; return only "
        "an `ExperimentProposal`."
    ),
    (
        "Do not call `Run.request`, `Run.status`, `Eval.run`, any `Task.*` tool, "
        "or any direct process tool."
    ),
    (
        "Do not use a shell, subprocess, SSH, remote execution, job queue, upload, "
        "or notification service."
    ),
    (
        "Do not rank proposals by pilot score, aggregate score, or a score "
        "threshold."
    ),
    "Do not use a fixed experiment count or fixed-round stopping rule.",
    (
        "Do not select the top positive result, prefer positive outcomes, or "
        "choose a winner from pilot results."
    ),
)
EXPECTED_EVIDENCE_INPUT_RULES = (
    "Artifact: Read exactly one `RunEvidence`.",
    (
        "Required fields: `run_ref`, `eval_refs`, `raw_refs`, `process_state`, "
        "`budget_used`, `rival_mechanism_set_ref`, `mechanism_refs`, "
        "`experiment_proposal_ref`, `prediction_ref`, `falsifier_ref`, "
        "`preregistration_ref`."
    ),
    (
        "Immutability: Treat `run_ref`, every `eval_ref`, and every `raw_ref` as "
        "an immutable exact reference."
    ),
    (
        "Lineage: Treat `rival_mechanism_set_ref`, `mechanism_refs`, "
        "`experiment_proposal_ref`, `prediction_ref`, and `falsifier_ref` as "
        "required non-null lineage, and treat `preregistration_ref` as a required "
        "field whose value may be null; do not infer omitted lineage."
    ),
)
EXPECTED_EVIDENCE_CLASSIFICATION_FIELDS = (
    "evidence_ref",
    "operational_state",
    "scientific_relations",
    "relation_targets",
    "confirmation_status",
    "confirmation_deviations",
)
EXPECTED_CONFIRMATION_MATCH_FIELDS = (
    "candidate_commit",
    "data_manifest_refs",
    "environment_sha256",
    "evaluator_version",
    "output_schema_sha256",
    "controls",
    "primary_comparisons",
    "analysis_boundaries",
    "stopping_rules",
    "rerun_rules",
)
EXPECTED_CONFIRMATION_MATCH_RULES = (
    (
        "When `preregistration_ref` is non-null, for `candidate_commit`, require "
        "the executed receipt value to exactly equal the `Preregistration` value."
    ),
    (
        "When `preregistration_ref` is non-null, for `data_manifest_refs`, require "
        "the executed receipt references and order to exactly equal the "
        "`Preregistration` value."
    ),
    (
        "When `preregistration_ref` is non-null, for `environment_sha256`, require "
        "the executed receipt value to exactly equal the `Preregistration` value."
    ),
    (
        "When `preregistration_ref` is non-null, for `evaluator_version`, require "
        "the executed receipt value to exactly equal the `Preregistration` value."
    ),
    (
        "When `preregistration_ref` is non-null, for `output_schema_sha256`, "
        "require the executed receipt value to exactly equal the `Preregistration` "
        "value."
    ),
    (
        "When `preregistration_ref` is non-null, for `controls`, require the "
        "executed receipt control definitions and specifications to exactly equal "
        "the `Preregistration` value."
    ),
    (
        "When `preregistration_ref` is non-null, for `primary_comparisons`, "
        "require the executed receipt comparison definitions, metric "
        "specifications, control specifications, and analysis specifications to "
        "exactly equal the `Preregistration` value; never compare observed outcome "
        "values."
    ),
    (
        "When `preregistration_ref` is non-null, for `analysis_boundaries`, require "
        "the executed analysis boundaries to exactly equal the `Preregistration` "
        "value."
    ),
    (
        "When `preregistration_ref` is non-null, for `stopping_rules`, require the "
        "executed stopping behavior to exactly equal the `Preregistration` value."
    ),
    (
        "When `preregistration_ref` is non-null, for `rerun_rules`, require the "
        "executed rerun behavior to exactly equal the `Preregistration` value."
    ),
)
EXPECTED_SCIENTIFIC_CONFIRMATION_FIELDS = (
    "experiment_proposal_ref",
    "mechanism_refs",
    "prediction_ref",
    "falsifier_ref",
)
EXPECTED_SCIENTIFIC_CONFIRMATION_RULES = (
    (
        "When `preregistration_ref` is non-null, require the input "
        "`experiment_proposal_ref` to exactly equal the `Preregistration` "
        "`experiment_proposal_ref`."
    ),
    (
        "When `preregistration_ref` is non-null, require the input "
        "`mechanism_refs` and `Preregistration` `mechanism_refs` to have the same "
        "duplicate-free ordered sequence and the same set, and require every "
        "mechanism to belong to the current `RivalMechanismSet` read from "
        "`rival_mechanism_set_ref`."
    ),
    (
        "When `preregistration_ref` is non-null, require the input "
        "`prediction_ref` to exactly equal the `Preregistration` `prediction_ref`."
    ),
    (
        "When `preregistration_ref` is non-null, require the input "
        "`falsifier_ref` to exactly equal the `Preregistration` `falsifier_ref`."
    ),
)
EXPECTED_EVIDENCE_METHOD_RULES = (
    (
        "Read the exact immutable receipts identified by `run_ref` and "
        "`eval_refs` and the exact immutable raw references identified by "
        "`raw_refs`; bind each read to its reference before interpretation."
    ),
    (
        "Use `Git.read` to read the exact `RivalMechanismSet`, "
        "`ExperimentProposal`, prediction, and falsifier named by the non-null "
        "lineage fields before classification; when `preregistration_ref` is "
        "non-null, also read that exact `Preregistration`; reject missing or "
        "inconsistent non-null lineage."
    ),
    (
        "Require input `mechanism_refs` to belong to the referenced "
        "`RivalMechanismSet` and to match the `ExperimentProposal` "
        "`mechanism_refs`; require `prediction_ref` to identify that proposal's "
        "prediction and `falsifier_ref` to identify that proposal's falsifier."
    ),
    (
        "Emit exactly one `classifications` entry for every evidence item; each "
        "entry is interpreted only within the verified proposal and prediction "
        "lineage."
    ),
    (
        "Set each `evidence_ref` to exactly the input `run_ref`, one of the input "
        "`eval_refs`, or one of the input `raw_refs`; emit no classification for "
        "any other reference."
    ),
    (
        "For every evidence item, record exactly one `operational_state`: "
        "`unavailable`, `failed`, or `executed`; determine it only from the exact "
        "process, transport, evaluator, and measurement receipts."
    ),
    (
        "Set every classification's `confirmation_status` to exactly "
        "`confirmatory` or `non_confirmatory`."
    ),
    (
        "Only an item whose `operational_state` is `executed` may carry "
        "`scientific_relations`; assign zero or more of `supports`, `weakens`, "
        "`eliminates`, `counterexample`, and `negative_result` from the scientific "
        "evidence."
    ),
    (
        "Allow one executed item to carry both `negative_result` and "
        "`counterexample` when both apply; scientific relations are not mutually "
        "exclusive."
    ),
    (
        "An item whose `operational_state` is `unavailable` or `failed` must have "
        "empty `scientific_relations` and must never be recorded as a "
        "`negative_result`; set its `confirmation_status` to `non_confirmatory`."
    ),
    (
        "For every `supports`, `weakens`, `eliminates`, or `counterexample` "
        "relation, require every `relation_targets` value to belong to the input "
        "`mechanism_refs`; do not infer or name an arbitrary rival."
    ),
    (
        "Connect every executed classification's `evidence_ref` to the input "
        "`experiment_proposal_ref`, `prediction_ref`, and `falsifier_ref` before "
        "applying any scientific relation."
    ),
    (
        "When the input `preregistration_ref` is null, set every "
        "`confirmation_status` to `non_confirmatory` and "
        "`confirmation_deviations` to exactly [`missing_preregistration`]; do not "
        "apply methods 14 through 23 or compare expected values. Executed evidence "
        "may still carry `supports`, `weakens`, `eliminates`, `counterexample`, or "
        "`negative_result`."
    ),
    *EXPECTED_CONFIRMATION_MATCH_RULES,
    *EXPECTED_SCIENTIFIC_CONFIRMATION_RULES,
    (
        "When `preregistration_ref` is non-null, for any missing or deviating "
        "confirmation binding, set `confirmation_status` to `non_confirmatory` and "
        "append a `confirmation_deviations` item containing the field name, "
        "expected preregistered value, observed receipt value or a missing marker, "
        "and `evidence_ref`; record every mismatch."
    ),
    (
        "Set `confirmation_status` to `confirmatory` only for executed evidence "
        "when `preregistration_ref` is non-null, the proposal, mechanism, "
        "prediction, falsifier, and current-rival-set bindings match exactly, "
        "every execution-envelope binding matches exactly, and "
        "`confirmation_deviations` is empty; null never yields `confirmatory`."
    ),
    (
        "Map `supports` to `strengthened`, `weakens` to `weakened`, and "
        "`eliminates` to `eliminated`; attach the exact evidence reference to "
        "every update."
    ),
    (
        "Preserve every negative result, counterexample, preregistration "
        "deviation, protocol deviation, and unexpected condition with its exact "
        "evidence reference."
    ),
    (
        "If the next action changes, cite the specific prior observation that "
        "caused the change in `next_action_rationale`; otherwise state why the "
        "current action remains justified."
    ),
    (
        "Record the most important remaining uncertainty, the next-action "
        "rationale, and the exact input `budget_used` without discarding achieved "
        "evidence."
    ),
)
EXPECTED_EVIDENCE_COMPLETION_RULES = (
    (
        "Complete only after every declared receipt, raw reference, and non-null "
        "lineage artifact has been read and every evidence item has exactly one "
        "valid classification in one `ObservationUpdate`."
    ),
    (
        "If required rival, proposal, prediction, or falsifier lineage is missing or "
        "inconsistent, do not emit an `ObservationUpdate`; a null "
        "`preregistration_ref` is allowed and does not block completion."
    ),
    "Call `Research.observe` with the complete `ObservationUpdate`.",
    (
        "Call `Research.checkpoint` with the observation update and its exact "
        "immutable evidence references."
    ),
    (
        "Exit immediately after the successful checkpoint; do not continue the "
        "research session."
    ),
)
EXPECTED_EVIDENCE_FORBIDDEN_RULES = (
    (
        "Do not attach a scientific relation, including `negative_result`, to "
        "evidence whose `operational_state` is `unavailable` or `failed`."
    ),
    (
        "Do not place a value outside the input `mechanism_refs` in "
        "`relation_targets` or update an arbitrary rival."
    ),
    (
        "Do not set `confirmation_status` to `confirmatory` when "
        "`preregistration_ref` is null, the evidence is not executed, any required "
        "receipt lineage value is missing or deviates, or "
        "`confirmation_deviations` is nonempty."
    ),
    (
        "Do not call `Eval.run`, create an evaluation, or execute an evaluator; "
        "read only existing evaluation receipts."
    ),
    (
        "Do not request, retry, restart, rerun, or resubmit an experiment, and do "
        "not call `Run.request`."
    ),
    (
        "Do not overwrite or mutate a receipt, raw reference, prior observation, "
        "negative result, counterexample, or deviation."
    ),
    (
        "Do not use a shell, subprocess, SSH, remote execution, job queue, upload, "
        "or notification service."
    ),
    (
        "Do not use a score, score threshold, fixed-round rule, or preference for "
        "the top positive result to interpret evidence or decide whether to stop."
    ),
    "Do not admit, accept, or reject a `Claim`; Claim admission is outside this procedure.",
)

EXPECTED_REVIEW_INPUT_RULES = (
    "Artifact: Read exactly one `FrozenEvidencePacket`.",
    (
        "Required fields: `task_brief_ref`, `preregistration_ref`, "
        "`candidate_commit`, `source_refs`, `raw_refs`, `reproduction_ref`, "
        "`root_question_ref`, `claim_draft_ref`, `researcher_model_id`, "
        "`researcher_model_family`, `researcher_session_refs`, "
        "`researcher_worktree_ref`, `packet_sha256`, `review_session_receipt_ref`, "
        "`reviewer_session_id`."
    ),
    (
        "Context isolation: Start a fresh Reviewer model with empty message history, "
        "no Researcher transcript, no prior review conversation, and only the frozen "
        "packet as initial context."
    ),
    (
        "Read-only boundary: Treat the packet, exact `candidate_commit`, Claim draft, "
        "preregistration, sources, raw evidence, reproduction package, Researcher session "
        "references, and Researcher worktree reference as immutable."
    ),
)
EXPECTED_REVIEW_METHOD_RULES = (
    (
        "Require `packet_sha256` to equal SHA-256 of the UTF-8 bytes of canonical JSON "
        "for the `FrozenEvidencePacket` excluding `packet_sha256`, encoded with "
        "`sort_keys=True`, `separators=(\",\", \":\")`, and `ensure_ascii=False`; compute "
        "and compare it before dereferencing or reviewing any packet content."
    ),
    (
        "Use `Receipt.read` to read exact `review_session_receipt_ref` before any Reviewer "
        "analysis; require it to be immutable, host-issued before the Reviewer started, "
        "and to contain `reviewer_model_id`, `reviewer_model_family`, "
        "`researcher_model_id`, `researcher_model_family`, `initial_message_count`, "
        "`supplied_packet_sha256`, `reviewer_session_id`, `nonce`, `created_at`, and "
        "`issuer`."
    ),
    (
        "Read the exact TaskBrief, root question, Claim draft, preregistration, candidate "
        "commit, sources, raw evidence, and reproduction package named by the packet; "
        "require one coherent question, Claim, candidate, preregistration, source, raw, "
        "and reproduction lineage across every exact reference."
    ),
    (
        "Read and bind `researcher_model_id`, `researcher_model_family`, every "
        "`researcher_session_refs` entry, and `researcher_worktree_ref`; reject a missing, "
        "unknown, unverifiable, cross-lineage, or review-receipt-mismatched Researcher "
        "identity or execution reference."
    ),
    (
        "Require the review session receipt `supplied_packet_sha256` to equal input "
        "`packet_sha256`, `initial_message_count` to be the plain integer 0, receipt "
        "`reviewer_session_id` to equal both input `reviewer_session_id` and the active "
        "host Reviewer session or thread, `nonce` to be unique and unused, its Reviewer "
        "identity to equal the active Reviewer, its Researcher identity to equal the packet, "
        "and both model families to be known and different."
    ),
    (
        "Set `independence_evidence_ref` exactly to input `review_session_receipt_ref`; "
        "bind its exact Reviewer and Researcher identities to every new Reviewer Run and "
        "Eval receipt, and do not self-issue or replace the host receipt."
    ),
    (
        "Independently rebuild the exact `candidate_commit` in a clean isolated Reviewer "
        "Run without editing the candidate or using `researcher_worktree_ref` or any "
        "Researcher-built workspace."
    ),
    (
        "Independently rerun every primary evidence path from the exact "
        "`reproduction_ref` through a new Reviewer `Run.request`, follow each with "
        "`Run.status`, and never reuse a candidate, Researcher, or prior Reviewer Run as "
        "the independent reproduction."
    ),
    (
        "Run the specified evaluations through new Reviewer `Eval.run` calls and compare "
        "both raw outputs and parsed results with the frozen raw evidence; record every "
        "match, mismatch, and unavailable comparison."
    ),
    (
        "Record exact packet, Claim-draft, candidate, source, raw-evidence, reproduction, "
        "Run, Eval, and receipt references in `reproduction_refs`, together with exact "
        "Researcher and Reviewer identities and `independence_evidence_ref`."
    ),
    (
        "Develop and test plausible alternative explanations, and attempt at least one "
        "counterexample that could distinguish the reported mechanism from those "
        "alternatives."
    ),
    (
        "Audit leakage and contamination, warmup and startup effects, data identity and "
        "partitioning, cache isolation and cache state, statistical assumptions and "
        "uncertainty, hard-constraint compliance, and claimed scope."
    ),
    (
        "Place every material issue in the corresponding report field, mark objections "
        "that defeat the Claim as `fatal_objections`, retain all other open issues in "
        "`unresolved_objections`, and do not convert the report into a canonical verdict."
    ),
)
EXPECTED_REVIEW_COMPLETION_RULES = (
    (
        "Complete only after the exact canonical packet hash, host-issued review session "
        "receipt, unique active `reviewer_session_id`, unused nonce, zero-message fresh "
        "context, and one coherent lineage are verified, cross-family independence is "
        "proven, every frozen reference is read, every primary evidence path has an "
        "independent Reviewer reproduction attempt, every required audit is performed, "
        "and all `ReviewerReport` fields are populated."
    ),
    (
        "If the packet hash fails, any required reference is missing, or question, Claim, "
        "candidate, preregistration, source, raw, reproduction, identity, session, or "
        "worktree references are spliced across lineages, do not emit a `ReviewerReport`; "
        "call `Research.petition` with the exact lineage blocker and exit incomplete."
    ),
    (
        "If `review_session_receipt_ref` is missing or unreadable, the receipt is not "
        "host-issued before Reviewer start, `supplied_packet_sha256` mismatches, "
        "`initial_message_count` is not the plain integer 0, receipt, input, and active "
        "`reviewer_session_id` values do not all match, the receipt or nonce belongs to an "
        "old session or has been replayed, either identity mismatches, the families are "
        "equal, unknown, or unverifiable, or `independence_evidence_ref` cannot equal that "
        "receipt, do not emit a `ReviewerReport`; call `Research.petition` with the exact "
        "independence blocker and exit incomplete."
    ),
    (
        "If Reviewer transport is unavailable, a required new Reviewer Run or Eval "
        "cannot be requested or observed, or the fresh model identity cannot be bound, "
        "do not emit a `ReviewerReport`; call `Research.petition` with the exact transport "
        "blocker and exit incomplete, which blocks Claim admission."
    ),
    (
        "A completed reproduction that disagrees with the candidate is scientific "
        "counterevidence, not transport failure; emit it in the report and mark a fatal "
        "objection when it defeats the Claim."
    ),
    (
        "Return the completed objection report and exit the fresh Reviewer session "
        "without admitting or editing a Claim."
    ),
)
EXPECTED_REVIEW_FORBIDDEN_RULES = (
    (
        "Do not read or request a Researcher transcript, reuse message history, continue "
        "a Researcher session, or substitute self-review for the fresh independent review."
    ),
    (
        "Do not edit the candidate, exact `candidate_commit`, frozen packet, Claim draft, "
        "preregistration, evidence, or reproduction package."
    ),
    (
        "Do not admit, accept, narrow, or reject a Claim, and do not present the report "
        "as a canonical scientific verdict."
    ),
    (
        "Do not reuse a candidate, Researcher, or prior review Run as an independent "
        "Reviewer reproduction."
    ),
    (
        "Do not claim independence from different model identifiers alone, accept the "
        "same or an unknown model family, fabricate `independence_evidence_ref`, or accept "
        "spliced packet references."
    ),
    (
        "Do not issue, synthesize, alter, or substitute `review_session_receipt_ref`; it "
        "must be the pre-existing host receipt read with `Receipt.read`, and output "
        "`independence_evidence_ref` must equal it."
    ),
    (
        "Do not reuse a review session receipt, `reviewer_session_id`, or nonce from an old "
        "session, and do not echo a session identifier that differs from the active host "
        "Reviewer session or thread."
    ),
    (
        "Do not use a shell, subprocess, SSH, direct remote execution, job queue, upload, "
        "or notification service; experimental work must use the declared AROS Run and "
        "Eval tools."
    ),
    (
        "Do not use a score, ranking, pass threshold, or aggregate quality number to "
        "replace evidence or objections."
    ),
    "Do not produce a paper, rebuttal, poster, slide deck, publication, or submission.",
)
EXPECTED_CLAIM_INPUT_RULES = (
    "Artifact: Read exactly one `AdjudicatedEvidence`.",
    (
        "Required fields: `claim_draft_ref`, `evidence_refs`, `review_ref`, "
        "`principal_response_ref`, `root_question_ref`, `candidate_commit`, "
        "`preregistration_ref`, `reproduction_ref`, `principal_actor_ref`, "
        "`principal_checkpoint_ref`, `disposition`, `principal_decision_receipt_ref`, "
        "`authority_class`, `principal_authority_ref`, `checkpoint_reservation_ref`."
    ),
    (
        "Exact reads: Read the exact Claim draft, every evidence reference, Reviewer "
        "report, and Principal response before constructing the package; bind every "
        "value to its immutable reference."
    ),
    (
        "Authority: Derive adjudication authority only from exact "
        "`principal_authority_ref` and `principal_decision_receipt_ref` values read with "
        "`Receipt.read` from the canonical AROS receipt store; do not infer actor authority, "
        "enforcement, acceptance, narrowing, rejection, or objection resolution from names, "
        "labels, inline payloads, or paths."
    ),
)
EXPECTED_CLAIM_METHOD_RULES = (
    (
        "Read the exact Claim draft, every evidence reference, Reviewer report, Principal "
        "response, Principal actor record, Principal checkpoint, Principal authority "
        "receipt, Principal decision receipt, checkpoint reservation, preregistration, and "
        "reproduction package before packaging."
    ),
    (
        "Require `root_question_ref`, `candidate_commit`, `preregistration_ref`, "
        "`reproduction_ref`, `claim_draft_ref`, evidence lineage, and review lineage to "
        "match exactly across every input and every referenced artifact; reject missing "
        "or cross-lineage references."
    ),
    (
        "Use `Receipt.read` to read exact `principal_authority_ref` from the canonical AROS "
        "receipt store; require the immutable authority receipt to bind exactly `issuer`, "
        "`actor`, `enforcement_class`, and `authority_context_sha256`."
    ),
    (
        "Use `Receipt.read` to read exact `principal_decision_receipt_ref` from the canonical "
        "AROS receipt store; require the canonical immutable receipt returned by "
        "`Receipt.read`, not an inline or unbound payload, to bind exactly `issuer`, "
        "`actor`, `principal_response_ref`, `principal_checkpoint_ref`, `disposition`, "
        "`enforcement_class`, and `authority_context_sha256`."
    ),
    (
        "Require decision-receipt `issuer` and `actor` to equal the issuer and actor "
        "authorized by the Principal authority receipt, decision-receipt `actor` to equal "
        "input `principal_actor_ref`, its response, checkpoint, and disposition references "
        "to equal the corresponding inputs, its checkpoint to bind the same response and "
        "lineage, its `authority_context_sha256` and `enforcement_class` to equal the "
        "authority receipt, and input `authority_class` to equal that "
        "`enforcement_class`, which must be exactly `cooperative` or `protected`."
    ),
    (
        "Use `Receipt.read` to read exact `checkpoint_reservation_ref` from the canonical "
        "AROS receipt store; require the immutable host-issued reservation to bind "
        "exactly `planned_checkpoint_ref`, `idempotency_key`, `claim_package_path`, "
        "`claim_package_hash_protocol`, `principal_decision_receipt_ref`, "
        "`principal_authority_ref`, `reservation_nonce`, and `status`."
    ),
    (
        "Require reservation `status` to be exactly `pending` or `consumed`, Principal "
        "decision and authority references to equal the verified input references, and its "
        "package path and hash protocol to identify the exact future `ClaimPackage`; when "
        "status is `pending`, require `planned_checkpoint_ref` and `idempotency_key` to be "
        "unique and unused."
    ),
    (
        "Require input `disposition` and the Principal response disposition to match and "
        "to equal exactly one of `accept`, `narrow`, or `reject`; copy the disposition, "
        "rationale, and evidence references without changing them."
    ),
    (
        "Enumerate every material Reviewer objection, including every fatal and unresolved "
        "objection, and map it one-to-one to an explicit Principal response."
    ),
    (
        "Require the Principal response to answer every material objection with exactly "
        "one disposition: `accept`, `narrow`, or `reject`; copy each answer, rationale, "
        "evidence reference, and scope effect without changing them."
    ),
    (
        "For disposition `accept` or `narrow`, construct `claim` and `scope` only from the "
        "adjudicated wording and boundaries; never broaden the admitted result or repair "
        "it with new policy."
    ),
    (
        "For disposition `reject`, construct only a rejected adjudication package that "
        "records the rejected Claim draft and reasons; do not describe it as an admitted, "
        "supported, or scientific negative Claim."
    ),
    (
        "Describe a scientific negative Claim only when executed evidence explicitly "
        "records a negative result and the Principal disposition `accept` or `narrow` "
        "admits that scoped negative Claim; rejection alone is never scientific evidence."
    ),
    (
        "Construct `evidence_refs` and `counterevidence` from the exact adjudicated evidence "
        "and review references, preserving contrary observations, counterexamples, and "
        "executed negative results."
    ),
    (
        "Copy exact, bounded reproduction commands from `reproduction_ref` into "
        "`reproduction_commands`, derive `environment_ref` from matching preregistration, "
        "reproduction, and evidence receipts, and do not invent or execute a command."
    ),
    (
        "State limitations and `remaining_uncertainty` at the adjudicated scope, including "
        "unresolved nonfatal objections and evidence that could change the conclusion."
    ),
    (
        "Populate `review_objections` with every material objection, its exact "
        "`review_ref`, its Principal disposition and `principal_response_ref`, and the "
        "resulting scope effect."
    ),
    (
        "Before checkpointing, write the complete `ClaimPackage` at the reserved "
        "`claim_package_path`, set `checkpoint_ref` exactly to reserved "
        "`planned_checkpoint_ref`, and serialize and hash it with the reserved "
        "`claim_package_hash_protocol`."
    ),
)
EXPECTED_CLAIM_COMPLETION_RULES = (
    (
        "Complete only after every exact input reference and cross-artifact lineage is "
        "verified, canonical Principal authority and decision receipts exactly bind and "
        "authorize issuer, actor, response, checkpoint, disposition, authority context, "
        "and enforcement class, the checkpoint reservation is canonical and matching with "
        "known status, every material objection has an explicit Principal disposition, no "
        "fatal objection remains unresolved, and all `ClaimPackage` fields are populated."
    ),
    (
        "If any required reference is missing or cross-lineage, "
        "`principal_authority_ref` or `principal_decision_receipt_ref` is missing, unreadable, "
        "noncanonical, inline, or unbound, either receipt has an unknown field, decision "
        "issuer or actor is not authorized by the authority receipt, actor, response, "
        "checkpoint, disposition, authority context, or enforcement class mismatches, the "
        "checkpoint does not bind the same response and lineage, any material objection is "
        "unanswered, or any fatal objection remains unresolved, do not emit or checkpoint a "
        "`ClaimPackage`; return incomplete for Principal adjudication."
    ),
    (
        "If `checkpoint_reservation_ref` is missing, unreadable, noncanonical, or expired, "
        "its status is neither `pending` nor `consumed`, its decision or authority reference "
        "mismatches, or its package path or hash protocol does not match the exact future "
        "`ClaimPackage`, do not emit or checkpoint a `ClaimPackage`; return incomplete for "
        "a new host-issued reservation."
    ),
    (
        "If reservation status is `consumed`, use `Receipt.read` to read the canonical "
        "checkpoint receipt and status; require `checkpoint_reservation_ref`, "
        "`idempotency_key`, reserved `checkpoint_ref`, `claim_package_path`, "
        "`claim_package_sha256` computed under the reserved hash protocol, "
        "`principal_decision_receipt_ref`, `principal_authority_ref`, and terminal "
        "`complete` status to match exactly, then treat the checkpoint as already complete, "
        "return the existing `checkpoint_ref`, and do not call `Research.checkpoint` or "
        "request a new reservation."
    ),
    (
        "If reservation status is `consumed` but any reservation id, idempotency key, "
        "reserved checkpoint reference, ClaimPackage path or hash, Principal decision or "
        "authority reference, or terminal status mismatches, block as a replay conflict; "
        "do not return a checkpoint reference, call `Research.checkpoint`, or request a new "
        "reservation."
    ),
    (
        "For disposition `accept` or `narrow`, emit only the scoped admitted Claim that "
        "the verified Principal response authorizes."
    ),
    (
        "For disposition `reject`, emit a rejected adjudication package and set "
        "`disposition` to `reject`; do not emit an admitted or supported Claim and do not "
        "convert rejection into a scientific negative result."
    ),
    (
        "Only when reservation status is `pending`, after writing the complete package with "
        "reserved `checkpoint_ref`, call `Research.checkpoint` exactly once using the "
        "reservation `idempotency_key`, exact package path and hash, lineage and evidence "
        "references, `review_ref`, `principal_response_ref`, `principal_authority_ref`, "
        "`principal_decision_receipt_ref`, and preserved `authority_class`; complete only if "
        "the returned `checkpoint_ref` equals reserved `planned_checkpoint_ref` exactly."
    ),
    (
        "If the response to the single pending checkpoint call is lost, do not call "
        "`Research.checkpoint` again and do not request a new reservation; exit incomplete, "
        "then in the next session reread canonical reservation status and follow the exact "
        "`consumed` recovery rule."
    ),
    "Exit immediately after the successful checkpoint; do not continue the research session.",
)
EXPECTED_CLAIM_FORBIDDEN_RULES = (
    (
        "Do not admit, accept, narrow, or reject a Claim; only the Principal may perform "
        "Claim admission or adjudication."
    ),
    (
        "Do not repair policy, invent a Principal disposition, resolve an objection, or "
        "alter adjudicated wording or scope."
    ),
    (
        "Do not accept a missing, unreadable, noncanonical, altered, or mismatched "
        "`principal_authority_ref` or `principal_decision_receipt_ref`, and do not combine "
        "references from different questions, candidates, preregistrations, reproductions, "
        "reviews, responses, actors, checkpoints, authority contexts, or enforcement "
        "classes."
    ),
    (
        "Do not accept an inline or unbound authority or decision receipt payload, read a "
        "receipt from a path or noncanonical store, or treat any value except the exact "
        "`Receipt.read` result as a canonical AROS receipt."
    ),
    (
        "Do not call cooperative authority protected, infer protected enforcement from a "
        "Principal label, or change `authority_class`; preserve the exact decision-receipt "
        "`enforcement_class`."
    ),
    (
        "Do not copy `principal_checkpoint_ref` into output `checkpoint_ref`, invent or "
        "alter a checkpoint reference or idempotency key, or checkpoint without the exact "
        "host-issued `checkpoint_reservation_ref`."
    ),
    (
        "Do not call `Research.checkpoint` or request a new reservation for an exactly "
        "matching consumed checkpoint, and do not treat a consumed mismatch as successful "
        "recovery or accept a returned checkpoint reference that differs from reserved "
        "`planned_checkpoint_ref`."
    ),
    (
        "Do not launder disposition `reject` into an admitted, supported, or scientific "
        "negative Claim; rejection is an adjudication outcome, not scientific evidence."
    ),
    (
        "Do not call a result scientifically negative unless exact executed evidence "
        "records the negative result and the verified Principal response admits that "
        "scoped negative Claim with disposition `accept` or `narrow`."
    ),
    (
        "Do not perform new science, create evidence, search for supporting evidence, or "
        "reinterpret the exact evidence beyond the Principal response."
    ),
    (
        "Do not run or request an experiment or evaluation, and do not use a shell, "
        "subprocess, SSH, direct remote execution, job queue, upload, or notification "
        "service."
    ),
    (
        "Do not omit counterevidence, a negative result, limitation, remaining uncertainty, "
        "or material Reviewer objection."
    ),
    (
        "Do not use a score, ranking, pass threshold, or aggregate quality number as an "
        "admission rule or package field."
    ),
    "Do not produce a paper, rebuttal, poster, slide deck, publication, or submission.",
)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise AssertionError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _load_source_record() -> dict[str, object]:
    value = json.loads(
        SOURCES_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_unique_json_object,
    )
    assert isinstance(value, dict)
    return value


def _parse_procedure_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---"
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise AssertionError("procedure frontmatter is not closed") from error

    metadata: dict[str, object] = {}
    active_list: list[str] | None = None
    for line in lines[1:closing]:
        field = re.fullmatch(r"([a-z_]+):(.*)", line)
        if field is not None:
            key, raw_value = field.groups()
            assert key not in metadata, f"duplicate frontmatter key: {key}"
            value = raw_value.strip()
            if value:
                metadata[key] = value
                active_list = None
            else:
                active_list = []
                metadata[key] = active_list
            continue

        item = re.fullmatch(r"  - ([A-Za-z0-9.-]+)", line)
        assert item is not None and active_list is not None, (
            f"invalid frontmatter line: {line!r}"
        )
        active_list.append(item.group(1))

    assert list(metadata) == ["name", "source_ids", "input", "output", "tools"]
    return metadata, "\n".join(lines[closing + 1 :])


def _procedure_section(body: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\n(?P<section>.*?)(?=^## |\Z)", body
    )
    assert match is not None, f"missing procedure section: {heading}"
    return match.group("section")


def _procedure_rules(section: str, *, numbered: bool) -> tuple[str, ...]:
    marker = r"([0-9]+)\. (.+)" if numbered else r"- (.+)"
    rules: list[str] = []
    current: list[str] | None = None
    ordinals: list[int] = []

    for line in section.strip().splitlines():
        match = re.fullmatch(marker, line)
        if match is not None:
            if current is not None:
                rules.append(" ".join(current))
            if numbered:
                ordinals.append(int(match.group(1)))
                current = [match.group(2)]
            else:
                current = [match.group(1)]
            continue

        assert current is not None and line.startswith("  "), (
            f"invalid normative rule line: {line!r}"
        )
        current.append(line.strip())

    assert current is not None, "normative rule list must not be empty"
    rules.append(" ".join(current))
    if numbered:
        assert ordinals == list(range(1, len(rules) + 1))
    return tuple(rules)


def _assert_procedure_rules(
    body: str,
    heading: str,
    expected: tuple[str, ...],
    *,
    numbered: bool,
) -> None:
    section = _procedure_section(body, heading)
    assert _procedure_rules(section, numbered=numbered) == expected


def _required_fields(rule: str) -> tuple[str, ...]:
    assert rule.startswith("Required fields: ") and rule.endswith(".")
    return tuple(re.findall(r"`([A-Za-z0-9_]+)`", rule))


def _assert_approved_source_record(record: dict[str, object]) -> None:
    assert record == APPROVED_SOURCE_RECORD


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _assert_commit_object(repository: Path, commit: object) -> None:
    assert isinstance(commit, str)
    assert re.fullmatch(r"[0-9a-f]{40}", commit)
    assert _git(repository, "rev-parse", commit) == commit
    assert _git(repository, "cat-file", "-t", commit) == "commit", (
        "source object must be a commit"
    )


def _is_source_or_provenance_record(program_root: Path, path: Path) -> bool:
    relative = path.relative_to(program_root)
    if path.suffix.casefold() not in SOURCE_RECORD_SUFFIXES:
        return False
    if relative.as_posix() in SOURCE_RECORD_EXEMPTIONS:
        return False
    candidates = (*relative.parts[:-1], path.stem)
    for candidate in candidates:
        tokens = {
            token for token in re.split(r"[^a-z0-9]+", candidate.casefold()) if token
        }
        if tokens & {"source", "sources", "provenance"}:
            return True
    return False


def _assert_sole_source_or_provenance_record(
    program_root: Path, sources_path: Path
) -> None:
    assert sources_path.relative_to(program_root).as_posix() == "SOURCES.json"
    assert sources_path.is_file()
    records = {
        path
        for path in program_root.rglob("*")
        if path.is_file() and _is_source_or_provenance_record(program_root, path)
    }
    assert not records, f"unexpected source or provenance record: {records}"


def _assert_no_upstream_product_names(program_root: Path, sources_path: Path) -> None:
    for path in program_root.rglob("*"):
        relative = path.relative_to(program_root)
        if not path.is_file() or path.suffix.casefold() not in RUNTIME_SOURCE_SUFFIXES:
            continue
        folded_parts = tuple(part.casefold() for part in relative.parts)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if any(part in RUNTIME_ARTIFACT_PARTS for part in folded_parts):
            continue
        relative_name = relative.as_posix().casefold()
        for upstream_name in UPSTREAM_PRODUCT_NAMES:
            assert upstream_name not in relative_name, (
                f"upstream product name {upstream_name!r} in path {path}"
            )
        if path != sources_path:
            content = path.read_bytes().lower()
            for upstream_name in UPSTREAM_PRODUCT_NAMES:
                assert upstream_name.encode() not in content, (
                    f"upstream product name {upstream_name!r} in {path}"
                )


def test_source_schema_versions_are_exact_int_one() -> None:
    record = _load_source_record()
    assert set(record) == {"schema_version", "sources"}
    assert type(record["schema_version"]) is int
    assert record["schema_version"] == 1

    package = importlib.import_module("commissioning.research_program")
    assert type(package.SCHEMA_VERSION) is int
    assert package.SCHEMA_VERSION == 1


def test_source_record_binds_exact_sources_and_adaptations() -> None:
    record = _load_source_record()
    _assert_approved_source_record(record)
    sources = record["sources"]
    assert isinstance(sources, list)

    for source in sources:
        assert isinstance(source, dict)
        adaptation = source["adaptation"]
        assert isinstance(adaptation, str)
        assert adaptation == adaptation.strip()
        assert 1 <= len(adaptation) <= 256


def test_source_repositories_commits_and_selected_paths_are_real() -> None:
    sources = _load_source_record()["sources"]
    assert isinstance(sources, list)

    for source in sources:
        repository_value = source["repository"]
        assert isinstance(repository_value, str)
        repository = Path(repository_value)
        assert repository.is_absolute()
        assert repository.is_dir()
        assert _git(repository, "rev-parse", "--is-inside-work-tree") == "true"
        git_directory = Path(
            _git(repository, "rev-parse", "--path-format=absolute", "--git-dir")
        )
        assert git_directory.is_absolute()
        assert git_directory.is_dir()

        commit = source["commit"]
        _assert_commit_object(repository, commit)

        selected_paths = source["selected_paths"]
        assert isinstance(selected_paths, list)
        assert selected_paths
        assert all(isinstance(path, str) for path in selected_paths)
        assert len(selected_paths) == len(set(selected_paths))
        for selected_path in selected_paths:
            path = PurePosixPath(selected_path)
            assert selected_path == path.as_posix()
            assert not path.is_absolute()
            assert path.parts
            assert all(part not in {"", ".", ".."} for part in path.parts)
            assert "\\" not in selected_path
            assert "\x00" not in selected_path
            assert (
                _git(repository, "cat-file", "-t", f"{commit}:{selected_path}")
                == "blob"
            )


def test_sources_json_is_the_sole_source_or_provenance_record() -> None:
    _assert_sole_source_or_provenance_record(PROGRAM_ROOT, SOURCES_PATH)


def test_source_runtime_names_do_not_reuse_upstream_product_names() -> None:
    _assert_no_upstream_product_names(PROGRAM_ROOT, SOURCES_PATH)


def test_source_record_scan_allows_exact_scientific_source_research(
    tmp_path: Path,
) -> None:
    program_root = tmp_path / "research_program"
    sources_path = program_root / "SOURCES.json"
    procedure = program_root / "procedures/aros-source-research.md"
    procedure.parent.mkdir(parents=True)
    sources_path.write_text("{}\n", encoding="utf-8")
    procedure.write_text(
        "# Source research\n\nScientific source and provenance analysis.\n",
        encoding="utf-8",
    )

    _assert_sole_source_or_provenance_record(program_root, sources_path)


@pytest.mark.parametrize(
    "relative_path",
    [
        "provenance.json",
        "source-record.json",
        "provenance/record.json",
        "source-record/record.json",
        "duplicate/SOURCES.json",
        "source.json",
        "SOURCES-v2.JSONL",
        "source/record.YAML",
        "Provenance-notes.YML",
        "PROVENANCE.MD",
        "research-provenance.json",
        "upstream-source-record.json",
        "nested/record-sources.yaml",
    ],
)
def test_source_record_scan_rejects_second_record_location(
    tmp_path: Path, relative_path: str
) -> None:
    program_root = tmp_path / "research_program"
    sources_path = program_root / "SOURCES.json"
    second_record = program_root / relative_path
    second_record.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text("{}\n", encoding="utf-8")
    second_record.write_text("{}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match="source or provenance record"):
        _assert_sole_source_or_provenance_record(program_root, sources_path)


def test_source_record_scan_ignores_scientific_content(tmp_path: Path) -> None:
    program_root = tmp_path / "research_program"
    sources_path = program_root / "SOURCES.json"
    notes = program_root / "procedures/resource-analysis.md"
    notes.parent.mkdir(parents=True)
    sources_path.write_text("{}\n", encoding="utf-8")
    notes.write_text(
        "Analyze scientific sources and provenance evidence.\n", encoding="utf-8"
    )

    _assert_sole_source_or_provenance_record(program_root, sources_path)


@pytest.mark.parametrize("upstream_name", UPSTREAM_PRODUCT_NAMES)
@pytest.mark.parametrize("placement", ["filename", "content"])
def test_source_runtime_name_scan_rejects_filename_and_content(
    tmp_path: Path, upstream_name: str, placement: str
) -> None:
    program_root = tmp_path / "research_program"
    program_root.mkdir()
    sources_path = program_root / "SOURCES.json"
    sources_path.write_text("{}\n", encoding="utf-8")
    if placement == "filename":
        bad_path = program_root / f"adapter-{upstream_name.upper()}.py"
        bad_path.write_text("pass\n", encoding="utf-8")
    else:
        bad_path = program_root / "adapter.py"
        bad_path.write_text(f"UPSTREAM = {upstream_name.upper()!r}\n", encoding="utf-8")

    with pytest.raises(AssertionError, match=upstream_name):
        _assert_no_upstream_product_names(program_root, sources_path)


def test_source_runtime_name_scan_allows_sources_json_bytes(tmp_path: Path) -> None:
    program_root = tmp_path / "research_program"
    program_root.mkdir()
    sources_path = program_root / "SOURCES.json"
    sources_path.write_text("ClAuDe and GeMiNi\n", encoding="utf-8")

    _assert_no_upstream_product_names(program_root, sources_path)


@pytest.mark.parametrize(
    "relative_path",
    ["__pycache__/cache.pyc", ".hidden.py", "build/generated.py"],
)
def test_source_runtime_name_scan_ignores_artifacts(
    tmp_path: Path, relative_path: str
) -> None:
    program_root = tmp_path / "research_program"
    sources_path = program_root / "SOURCES.json"
    artifact = program_root / relative_path
    artifact.parent.mkdir(parents=True, exist_ok=True)
    sources_path.write_text("{}\n", encoding="utf-8")
    artifact.write_bytes(b"CLAUDE and GEMINI\n")

    _assert_no_upstream_product_names(program_root, sources_path)


def test_source_approved_record_rejects_value_drift() -> None:
    record = _load_source_record()
    sources = record["sources"]
    assert isinstance(sources, list)
    sources[0]["repository"] = "/unapproved/repository"

    with pytest.raises(AssertionError):
        _assert_approved_source_record(record)


def test_source_commit_check_rejects_tree_oid() -> None:
    tree_oid = _git(ROOT, "rev-parse", "HEAD^{tree}")
    assert re.fullmatch(r"[0-9a-f]{40}", tree_oid)

    with pytest.raises(AssertionError, match="commit"):
        _assert_commit_object(ROOT, tree_oid)


def _contract_module():
    if not CONTRACTS_PATH.is_file():
        pytest.skip("canonical contract file is not implemented")
    return importlib.import_module("commissioning.research_program.validate")


def _contract_candidate(tmp_path: Path) -> tuple[object, dict[str, object], Path]:
    module = _contract_module()
    value = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    path = tmp_path / "procedure_contracts.json"
    return module, value, path


def _write_contract_candidate(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, allow_nan=False, separators=(",", ":")),
        encoding="utf-8",
    )


def test_contract_file_exists() -> None:
    assert CONTRACTS_PATH.is_file()


def test_contract_set_has_exact_canonical_values() -> None:
    module = _contract_module()
    contracts = module.load_contracts(CONTRACTS_PATH)

    assert type(contracts.schema_version) is int
    assert contracts.schema_version == 1
    assert contracts.allowed_tools == EXPECTED_ALLOWED_TOOLS
    assert dict(contracts.artifacts) == EXPECTED_ARTIFACTS
    assert tuple(contracts.procedures) == tuple(EXPECTED_PROCEDURES)
    for name, (input_name, output_name, tools) in EXPECTED_PROCEDURES.items():
        procedure = contracts.procedures[name]
        assert isinstance(procedure, module.ProcedureContract)
        assert procedure.input == input_name
        assert procedure.output == output_name
        assert procedure.tools == tools


@pytest.mark.parametrize(
    ("artifact", "field"),
    [
        ("FrozenEvidencePacket", "root_question_ref"),
        ("FrozenEvidencePacket", "claim_draft_ref"),
        ("FrozenEvidencePacket", "candidate_commit"),
        ("FrozenEvidencePacket", "researcher_model_id"),
        ("FrozenEvidencePacket", "researcher_model_family"),
        ("FrozenEvidencePacket", "researcher_session_refs"),
        ("FrozenEvidencePacket", "researcher_worktree_ref"),
        ("FrozenEvidencePacket", "packet_sha256"),
        ("FrozenEvidencePacket", "review_session_receipt_ref"),
        ("FrozenEvidencePacket", "reviewer_session_id"),
        ("ReviewerReport", "reviewer_model_id"),
        ("ReviewerReport", "reviewer_model_family"),
        ("ReviewerReport", "independence_evidence_ref"),
        ("ReviewerReport", "packet_sha256"),
        ("ReviewerReport", "claim_draft_ref"),
        ("ReviewerReport", "candidate_commit"),
        ("ReviewerReport", "reviewer_session_id"),
        ("AdjudicatedEvidence", "root_question_ref"),
        ("AdjudicatedEvidence", "candidate_commit"),
        ("AdjudicatedEvidence", "preregistration_ref"),
        ("AdjudicatedEvidence", "reproduction_ref"),
        ("AdjudicatedEvidence", "principal_actor_ref"),
        ("AdjudicatedEvidence", "principal_checkpoint_ref"),
        ("AdjudicatedEvidence", "disposition"),
        ("AdjudicatedEvidence", "principal_decision_receipt_ref"),
        ("AdjudicatedEvidence", "authority_class"),
        ("AdjudicatedEvidence", "principal_authority_ref"),
        ("AdjudicatedEvidence", "checkpoint_reservation_ref"),
        ("ClaimPackage", "disposition"),
        ("ClaimPackage", "root_question_ref"),
        ("ClaimPackage", "candidate_commit"),
        ("ClaimPackage", "preregistration_ref"),
        ("ClaimPackage", "review_ref"),
        ("ClaimPackage", "principal_response_ref"),
        ("ClaimPackage", "reproduction_ref"),
        ("ClaimPackage", "environment_ref"),
        ("ClaimPackage", "checkpoint_ref"),
        ("ClaimPackage", "principal_decision_receipt_ref"),
        ("ClaimPackage", "authority_class"),
        ("ClaimPackage", "principal_authority_ref"),
    ],
)
def test_contract_rejects_missing_review_claim_lineage_and_authority_fields(
    tmp_path: Path, artifact: str, field: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    artifacts = value["artifacts"]
    assert isinstance(artifacts, dict)
    fields = artifacts[artifact]
    assert isinstance(fields, list)
    assert field in fields
    fields.remove(field)
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="canonical list"):
        module.load_contracts(path)


def test_contract_names_candidate_and_reproduction_lineage_unambiguously() -> None:
    packet_fields = EXPECTED_ARTIFACTS["FrozenEvidencePacket"]
    claim_fields = EXPECTED_ARTIFACTS["ClaimPackage"]

    assert "commit" not in packet_fields
    assert "candidate_commit" in packet_fields
    assert "reproduction_ref" in packet_fields
    assert "reproduction_ref" in EXPECTED_ARTIFACTS["AdjudicatedEvidence"]
    assert "reproduction_ref" in claim_fields
    assert "checkpoint_ref" in claim_fields


def test_frozen_evidence_packet_hash_is_exact_canonical_utf8_and_detects_tamper() -> None:
    module = _contract_module()
    assert hasattr(module, "frozen_evidence_packet_sha256")
    packet = {
        "z_ref": "最后",
        "a_ref": "α",
        "packet_sha256": "stale",
    }
    original = dict(packet)

    assert module.frozen_evidence_packet_sha256(packet) == (
        "85742e4e59b03e3f1635d4cecbf4c5dcef840428cf99d819573fa8aa60e4d95b"
    )
    reordered = dict(reversed(tuple(packet.items())))
    assert module.frozen_evidence_packet_sha256(reordered) == (
        "85742e4e59b03e3f1635d4cecbf4c5dcef840428cf99d819573fa8aa60e4d95b"
    )
    changed_hash = {**packet, "packet_sha256": "ignored replacement"}
    assert module.frozen_evidence_packet_sha256(changed_hash) == (
        "85742e4e59b03e3f1635d4cecbf4c5dcef840428cf99d819573fa8aa60e4d95b"
    )
    tampered = {**packet, "a_ref": "β"}
    assert module.frozen_evidence_packet_sha256(tampered) != (
        "85742e4e59b03e3f1635d4cecbf4c5dcef840428cf99d819573fa8aa60e4d95b"
    )
    assert packet == original


def test_evidence_update_cross_contract_lineage_is_explicit_and_update_only() -> None:
    value = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    artifacts = value["artifacts"]
    procedures = value["procedures"]
    assert isinstance(artifacts, dict)
    assert isinstance(procedures, dict)

    assert tuple(artifacts["RunEvidence"]) == EXPECTED_ARTIFACTS["RunEvidence"]
    assert tuple(artifacts["ObservationUpdate"]) == EXPECTED_ARTIFACTS[
        "ObservationUpdate"
    ]
    assert tuple(artifacts["Preregistration"]) == EXPECTED_ARTIFACTS[
        "Preregistration"
    ]
    assert "mechanisms" in artifacts["RivalMechanismSet"]
    assert "mechanism_refs" in artifacts["ExperimentProposal"]
    assert "prediction" in artifacts["ExperimentProposal"]
    assert "key_predictions" in artifacts["Preregistration"]
    assert procedures["aros-evidence-update"]["tools"] == [
        "Run.status",
        "Receipt.read",
        "Git.read",
        "Research.observe",
        "Research.checkpoint",
    ]


def test_evidence_update_contract_rejects_eval_run_tool_bypass(
    tmp_path: Path,
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    procedures = value["procedures"]
    assert isinstance(procedures, dict)
    procedure = procedures["aros-evidence-update"]
    assert isinstance(procedure, dict)
    canonical_tools = list(EXPECTED_PROCEDURES["aros-evidence-update"][2])
    assert procedure["tools"] == canonical_tools
    procedure["tools"] = [*canonical_tools, "Eval.run"]
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="canonical contract"):
        module.load_contracts(path)


def test_contract_json_has_exact_container_shapes() -> None:
    _contract_module()
    value = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))

    assert list(value) == ["schema_version", "allowed_tools", "artifacts", "procedures"]
    assert isinstance(value["allowed_tools"], list)
    assert list(value["artifacts"]) == list(EXPECTED_ARTIFACTS)
    for name, required_fields in value["artifacts"].items():
        assert isinstance(required_fields, list), name
        assert required_fields == list(EXPECTED_ARTIFACTS[name])
    assert list(value["procedures"]) == list(EXPECTED_PROCEDURES)
    for procedure in value["procedures"].values():
        assert list(procedure) == ["input", "output", "tools"]
        assert isinstance(procedure["tools"], list)


def test_contract_results_are_recursively_immutable() -> None:
    module = _contract_module()
    contracts = module.load_contracts(CONTRACTS_PATH)
    procedure = contracts.procedures["aros-source-research"]

    with pytest.raises(FrozenInstanceError):
        contracts.schema_version = 2
    with pytest.raises(FrozenInstanceError):
        procedure.input = "OtherArtifact"
    with pytest.raises(TypeError):
        contracts.artifacts["ResearchQuestion"] = ()
    with pytest.raises(TypeError):
        contracts.procedures["new-procedure"] = procedure
    with pytest.raises(TypeError):
        contracts.artifacts["ResearchQuestion"][0] = "other"


def test_contract_dataclasses_are_slotted_and_forced_mutation_is_isolated() -> None:
    module = _contract_module()
    contracts = module.load_contracts(CONTRACTS_PATH)
    independent = module.load_contracts(CONTRACTS_PATH)
    procedure = contracts.procedures["aros-source-research"]

    assert not hasattr(contracts, "__dict__")
    assert not hasattr(procedure, "__dict__")
    assert isinstance(contracts.artifacts, MappingProxyType)
    assert isinstance(contracts.procedures, MappingProxyType)
    assert all(type(fields) is tuple for fields in contracts.artifacts.values())
    assert all(
        type(item.tools) is tuple for item in contracts.procedures.values()
    )

    for target, field, replacement in (
        (contracts, "artifacts", {"Injected": ["mutable"]}),
        (procedure, "tools", ["Injected.tool"]),
    ):
        try:
            object.__setattr__(target, field, replacement)
        except (AttributeError, TypeError):
            pass

    fresh = module.load_contracts(CONTRACTS_PATH)
    for untouched in (independent, fresh):
        assert dict(untouched.artifacts) == EXPECTED_ARTIFACTS
        assert untouched.procedures["aros-source-research"].tools == (
            "Source.read",
            "Source.search",
        )


def test_contract_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    module = _contract_module()
    raw = CONTRACTS_PATH.read_text(encoding="utf-8")
    duplicate = raw.replace(
        '"schema_version": 1,',
        '"schema_version": 1,\n  "schema_version": 1,',
        1,
    )
    path = tmp_path / "duplicate.json"
    path.write_text(duplicate, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        module.load_contracts(path)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_contract_loader_rejects_non_finite_json(
    tmp_path: Path, constant: str
) -> None:
    module = _contract_module()
    raw = CONTRACTS_PATH.read_text(encoding="utf-8")
    candidate = raw.replace(
        '"schema_version": 1,',
        f'"schema_version": 1,\n  "finite_probe": {constant},',
        1,
    )
    path = tmp_path / "non-finite.json"
    path.write_text(candidate, encoding="utf-8")

    with pytest.raises(ValueError, match="finite"):
        module.load_contracts(path)


@pytest.mark.parametrize(
    "forbidden",
    [
        "score",
        "ranking",
        "pass",
        "reward",
        "objective",
        "aggregate",
        "acceptance_score",
    ],
)
def test_contract_loader_recursively_rejects_forbidden_field_names(
    tmp_path: Path, forbidden: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    procedures = value["procedures"]
    assert isinstance(procedures, dict)
    source_research = procedures["aros-source-research"]
    assert isinstance(source_research, dict)
    source_research["nested"] = {"deeper": {forbidden: 0}}
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match=forbidden):
        module.load_contracts(path)


def test_contract_loader_requires_plain_schema_integer(tmp_path: Path) -> None:
    module, value, path = _contract_candidate(tmp_path)
    value["schema_version"] = True
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="schema_version"):
        module.load_contracts(path)


@pytest.mark.parametrize("section", ["top", "artifact", "procedure"])
def test_contract_loader_rejects_unknown_fields(
    tmp_path: Path, section: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    if section == "top":
        value["unknown"] = None
    elif section == "artifact":
        artifacts = value["artifacts"]
        assert isinstance(artifacts, dict)
        artifacts["UnknownArtifact"] = ["field"]
    else:
        procedures = value["procedures"]
        assert isinstance(procedures, dict)
        procedure = procedures["aros-source-research"]
        assert isinstance(procedure, dict)
        procedure["unknown"] = None
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="unknown"):
        module.load_contracts(path)


@pytest.mark.parametrize("section", ["allowed_tools", "artifact", "procedure_tools"])
def test_contract_loader_requires_exact_duplicate_free_lists(
    tmp_path: Path, section: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    if section == "allowed_tools":
        values = value["allowed_tools"]
    elif section == "artifact":
        artifacts = value["artifacts"]
        assert isinstance(artifacts, dict)
        values = artifacts["ResearchQuestion"]
    else:
        procedures = value["procedures"]
        assert isinstance(procedures, dict)
        procedure = procedures["aros-source-research"]
        assert isinstance(procedure, dict)
        values = procedure["tools"]
    assert isinstance(values, list)
    values.append(values[0])
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="duplicate"):
        module.load_contracts(path)


@pytest.mark.parametrize("reference", ["input", "output", "tool"])
def test_contract_loader_rejects_unknown_references(
    tmp_path: Path, reference: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    procedures = value["procedures"]
    assert isinstance(procedures, dict)
    procedure = procedures["aros-source-research"]
    assert isinstance(procedure, dict)
    if reference == "input":
        procedure["input"] = "UnknownArtifact"
    elif reference == "output":
        procedure["output"] = "UnknownArtifact"
    else:
        tools = procedure["tools"]
        assert isinstance(tools, list)
        tools[0] = "Unknown.tool"
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="unknown"):
        module.load_contracts(path)


@pytest.mark.parametrize("section", ["allowed_tools", "artifact", "procedure_tools"])
def test_contract_loader_rejects_non_list_collections(
    tmp_path: Path, section: str
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    if section == "allowed_tools":
        value["allowed_tools"] = "Source.read"
    elif section == "artifact":
        artifacts = value["artifacts"]
        assert isinstance(artifacts, dict)
        artifacts["ResearchQuestion"] = "question_ref"
    else:
        procedures = value["procedures"]
        assert isinstance(procedures, dict)
        procedure = procedures["aros-source-research"]
        assert isinstance(procedure, dict)
        procedure["tools"] = "Source.read"
    _write_contract_candidate(path, value)

    with pytest.raises(ValueError, match="list"):
        module.load_contracts(path)


def test_contract_loader_rejects_symlink_non_utf8_and_oversize(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    linked = tmp_path / "linked.json"
    linked.symlink_to(CONTRACTS_PATH)
    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b" " * (128 * 1024 + 1))

    with pytest.raises(ValueError, match="regular file"):
        module.load_contracts(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        module.load_contracts(linked)
    with pytest.raises(ValueError, match="UTF-8"):
        module.load_contracts(invalid_utf8)
    with pytest.raises(ValueError, match="128 KiB"):
        module.load_contracts(oversize)


def test_contract_loader_binds_single_read_to_lstat_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    _write_contract_candidate(path, value)
    replacement = path.read_bytes()
    real_open = os.open

    def replacing_open(candidate: object, flags: int) -> int:
        path.unlink()
        path.write_bytes(replacement)
        return real_open(candidate, flags)

    monkeypatch.setattr(module.os, "open", replacing_open)

    with pytest.raises(ValueError, match="identity"):
        module.load_contracts(path)


@pytest.mark.parametrize(
    "name",
    ["aros-source-research", "aros-rival-mechanisms"],
)
def test_wave_one_procedure_frontmatter_matches_central_contract(name: str) -> None:
    metadata, _ = _parse_procedure_frontmatter(PROCEDURES_ROOT / f"{name}.md")
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    contract = contracts["procedures"][name]

    assert metadata == {
        "name": name,
        "source_ids": ["source-1", "source-2"],
        "input": contract["input"],
        "output": contract["output"],
        "tools": contract["tools"],
    }


@pytest.mark.parametrize(
    "name",
    ["aros-source-research", "aros-rival-mechanisms"],
)
def test_wave_one_procedures_have_exact_sections_and_opaque_provenance(
    name: str,
) -> None:
    path = PROCEDURES_ROOT / f"{name}.md"
    metadata, body = _parse_procedure_frontmatter(path)
    raw = path.read_bytes().lower()

    assert tuple(re.findall(r"(?m)^## ([^\n]+)$", body)) == (
        EXPECTED_PROCEDURE_HEADINGS
    )
    assert all(body.count(f"## {heading}\n") == 1 for heading in EXPECTED_PROCEDURE_HEADINGS)
    assert all(
        isinstance(source_id, str)
        and re.fullmatch(r"source-[0-9]+", source_id) is not None
        for source_id in metadata["source_ids"]
    )
    assert not any(name.encode() in raw for name in UPSTREAM_PRODUCT_NAMES)
    assert b"repository:" not in raw
    assert b"commit:" not in raw
    assert b"license:" not in raw
    assert b"/workspace/" not in raw


def test_rival_procedure_has_exact_incomplete_branch_authority() -> None:
    metadata, _ = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )

    assert metadata["tools"] == [
        "Git.read",
        "Receipt.read",
        "Research.observe",
        "Research.petition",
    ]
    assert all(
        isinstance(tool, str) and not tool.startswith("Source.")
        for tool in metadata["tools"]
    )


def test_source_procedure_has_exact_normative_method_rules() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-source-research.md"
    )

    _assert_procedure_rules(
        body,
        "Method",
        EXPECTED_SOURCE_METHOD_RULES,
        numbered=True,
    )


def test_source_procedure_has_exact_output_fields_and_question_lineage() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-source-research.md"
    )
    output = _procedure_rules(_procedure_section(body, "Output"), numbered=False)

    assert len(output) == 4
    assert output[0] == "Artifact: Return exactly one `SourcePacket`."
    assert _required_fields(output[1]) == EXPECTED_ARTIFACTS["SourcePacket"]
    assert output[2] == (
        "Lineage: Copy input `question_ref` unchanged to output `question_ref`."
    )
    assert output[3] == (
        "Evidence binding: Bind every factual statement to a cited content "
        "reference or mark it as unresolved."
    )
    _assert_procedure_rules(
        body,
        "Completion",
        EXPECTED_SOURCE_COMPLETION_RULES,
        numbered=False,
    )


def test_source_procedure_forbids_direct_actions_and_scientific_verdicts() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-source-research.md"
    )

    _assert_procedure_rules(
        body,
        "Forbidden",
        EXPECTED_SOURCE_FORBIDDEN_RULES,
        numbered=False,
    )


def test_source_method_rejects_opposite_primary_source_polarity() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-source-research.md"
    )
    mutated = body.replace(
        "1. Prefer primary sources;",
        "1. Do not prefer primary sources;",
        1,
    )
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Method",
            EXPECTED_SOURCE_METHOD_RULES,
            numbered=True,
        )


def test_rival_procedure_forms_independent_falsifiable_causal_alternatives() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )

    inputs = _procedure_rules(_procedure_section(body, "Inputs"), numbered=False)
    assert len(inputs) == 3
    assert inputs[0] == "Artifact: Read exactly one `SourcePacket`."
    assert _required_fields(inputs[1]) == EXPECTED_ARTIFACTS["SourcePacket"]
    assert inputs[2] == (
        "Lineage: Treat input `question_ref` as the immutable root question "
        "reference."
    )
    _assert_procedure_rules(
        body,
        "Method",
        EXPECTED_RIVAL_METHOD_RULES,
        numbered=True,
    )


def test_rival_procedure_has_exact_output_fields_and_question_lineage() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )
    output = _procedure_rules(_procedure_section(body, "Output"), numbered=False)

    assert len(output) == 4
    assert output[0] == "Artifact: Return exactly one `RivalMechanismSet`."
    assert _required_fields(output[1]) == EXPECTED_ARTIFACTS["RivalMechanismSet"]
    assert output[2] == (
        "Lineage: Set `root_question_ref` exactly to input `question_ref`."
    )
    assert output[3] == (
        "Evidence binding: Preserve the evidence reference supporting or "
        "challenging every mechanism."
    )


def test_rival_completion_requires_two_surviving_discriminable_rivals() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )

    _assert_procedure_rules(
        body,
        "Completion",
        EXPECTED_RIVAL_COMPLETION_RULES,
        numbered=False,
    )
    completion = _procedure_section(body, "Completion")
    assert "return unresolved" not in completion.lower()
    assert completion.index("`Research.observe`") < completion.index(
        "`Research.petition`"
    )


def test_rival_procedure_rejects_score_winners_and_unfalsifiable_rivals() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )

    _assert_procedure_rules(
        body,
        "Forbidden",
        EXPECTED_RIVAL_FORBIDDEN_RULES,
        numbered=False,
    )


def test_rival_forbidden_rules_reject_opposite_ranking_polarity() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-rival-mechanisms.md"
    )
    mutated = body.replace(
        "- Do not rank mechanisms by pilot score or use pilot-score ranking.",
        "- May rank by pilot score.",
        1,
    )
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Forbidden",
            EXPECTED_RIVAL_FORBIDDEN_RULES,
            numbered=False,
        )


@pytest.mark.parametrize(
    "name",
    ["aros-experiment-design", "aros-evidence-update"],
)
def test_experiment_design_and_evidence_update_frontmatter_and_headings_match_contract(
    name: str,
) -> None:
    path = PROCEDURES_ROOT / f"{name}.md"
    metadata, body = _parse_procedure_frontmatter(path)
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    contract = contracts["procedures"][name]
    raw = path.read_bytes().lower()

    assert metadata == {
        "name": name,
        "source_ids": ["source-1", "source-2"],
        "input": contract["input"],
        "output": contract["output"],
        "tools": contract["tools"],
    }
    assert tuple(re.findall(r"(?m)^## ([^\n]+)$", body)) == (
        EXPECTED_PROCEDURE_HEADINGS
    )
    assert all(body.count(f"## {heading}\n") == 1 for heading in EXPECTED_PROCEDURE_HEADINGS)
    assert not any(upstream.encode() in raw for upstream in UPSTREAM_PRODUCT_NAMES)
    assert b"repository:" not in raw
    assert b"commit:" not in raw
    assert b"license:" not in raw
    assert b"/workspace/" not in raw


def test_experiment_design_reads_rivals_and_applies_exact_lexicographic_method() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-experiment-design.md"
    )

    _assert_procedure_rules(
        body,
        "Inputs",
        EXPECTED_EXPERIMENT_INPUT_RULES,
        numbered=False,
    )
    assert _required_fields(EXPECTED_EXPERIMENT_INPUT_RULES[1]) == EXPECTED_ARTIFACTS[
        "RivalMechanismSet"
    ]
    _assert_procedure_rules(
        body,
        "Method",
        EXPECTED_EXPERIMENT_METHOD_RULES,
        numbered=True,
    )


def test_experiment_design_returns_exact_proposal_and_petitions_if_none_valid() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-experiment-design.md"
    )
    output = _procedure_rules(_procedure_section(body, "Output"), numbered=False)

    assert len(output) == 4
    assert output[0] == "Artifact: Return exactly one `ExperimentProposal`."
    assert _required_fields(output[1]) == EXPECTED_ARTIFACTS["ExperimentProposal"]
    assert output[2] == (
        "Lineage: Restrict `mechanism_refs` to mechanisms in the input "
        "`RivalMechanismSet` and preserve their evidence bindings."
    )
    assert output[3] == (
        "Authority: Treat `run_request` as a future AROS request descriptor, not "
        "an execution command or authorization."
    )
    _assert_procedure_rules(
        body,
        "Completion",
        EXPECTED_EXPERIMENT_COMPLETION_RULES,
        numbered=False,
    )


def test_experiment_design_forbids_direct_actions_scores_and_fixed_rounds() -> None:
    metadata, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-experiment-design.md"
    )

    assert metadata["tools"] == [
        "Receipt.read",
        "Research.observe",
        "Research.petition",
    ]
    _assert_procedure_rules(
        body,
        "Forbidden",
        EXPECTED_EXPERIMENT_FORBIDDEN_RULES,
        numbered=False,
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "4. Apply the gates lexicographically and in this exact order: "
            "essentiality first,",
            "4. Apply the gates lexicographically and in this exact order: "
            "falsifiability first,",
        ),
        (
            "falsifiability second, and decision relevance third. Reject every "
            "candidate",
            "falsifiability second, and decision relevance third. Retain every "
            "candidate",
        ),
        (
            "5. Among candidates that pass all three gates, maximize expected "
            "information gain",
            "5. Among candidates that pass all three gates, minimize expected "
            "information gain",
        ),
        (
            "6. Consider concurrency only after all three gates pass",
            "6. Consider concurrency before all three gates pass",
        ),
    ],
)
def test_experiment_design_method_rejects_order_and_polarity_mutations(
    old: str, new: str
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-experiment-design.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Method",
            EXPECTED_EXPERIMENT_METHOD_RULES,
            numbered=True,
        )


def test_evidence_update_reads_exact_evidence_and_classifies_independent_axes() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )

    _assert_procedure_rules(
        body,
        "Inputs",
        EXPECTED_EVIDENCE_INPUT_RULES,
        numbered=False,
    )
    assert _required_fields(EXPECTED_EVIDENCE_INPUT_RULES[1]) == EXPECTED_ARTIFACTS[
        "RunEvidence"
    ]
    _assert_procedure_rules(
        body,
        "Method",
        EXPECTED_EVIDENCE_METHOD_RULES,
        numbered=True,
    )
    method = _procedure_rules(_procedure_section(body, "Method"), numbered=True)
    method_text = "\n".join(method)
    assert "`supports`" in method_text
    assert "assign zero or more" in method_text
    assert "both `negative_result` and `counterexample`" in method_text
    assert "empty `scientific_relations`" in method_text
    assert "`experiment_proposal_ref`, `prediction_ref`, and `falsifier_ref`" in (
        method_text
    )
    assert "`preregistration_ref`" in method_text
    assert not any(
        rule.startswith("Classify each evidence item as exactly one of")
        for rule in method
    )


def test_evidence_update_returns_exact_fields_then_checkpoints_and_exits() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    output = _procedure_rules(_procedure_section(body, "Output"), numbered=False)

    assert len(output) == 6
    assert output[0] == "Artifact: Return exactly one `ObservationUpdate`."
    assert _required_fields(output[1]) == EXPECTED_ARTIFACTS["ObservationUpdate"]
    assert output[2] == (
        "Classification entries: Every `classifications` item contains exactly "
        "`evidence_ref`, `operational_state`, `scientific_relations`, and "
        "`relation_targets`, `confirmation_status`, and "
        "`confirmation_deviations`."
    )
    assert tuple(re.findall(r"`([a-z_]+)`", output[2]))[1:] == (
        EXPECTED_EVIDENCE_CLASSIFICATION_FIELDS
    )
    assert output[3] == (
        "Confirmation deviations: `confirmation_deviations` is empty only when "
        "`confirmation_status` is `confirmatory`; when `preregistration_ref` is "
        "null it is exactly [`missing_preregistration`], and otherwise for "
        "`non_confirmatory`, list every missing or deviating preregistration "
        "binding with its field name, expected value, observed value or missing "
        "marker, and `evidence_ref`."
    )
    assert output[4] == (
        "Evidence binding: Cite an exact input receipt or raw reference for every "
        "strengthened, weakened, eliminated, counterexample, and negative-result "
        "entry; preserve the same reference in both lists when one executed item "
        "has both relations."
    )
    assert output[5] == (
        "Budget accounting: Include the exact input `budget_used` in "
        "`next_action_rationale`; do not add an output field outside the central "
        "contract."
    )
    _assert_procedure_rules(
        body,
        "Completion",
        EXPECTED_EVIDENCE_COMPLETION_RULES,
        numbered=False,
    )


def test_evidence_update_allows_null_preregistration_for_exploratory_relations() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    inputs = _procedure_rules(_procedure_section(body, "Inputs"), numbered=False)
    method = _procedure_rules(_procedure_section(body, "Method"), numbered=True)
    completion = _procedure_rules(
        _procedure_section(body, "Completion"), numbered=False
    )
    method_text = "\n".join(method)

    assert "required field whose value may be null" in inputs[3]
    assert "`confirmation_status` to `non_confirmatory`" in method_text
    assert "Executed evidence may still carry `supports`, `weakens`, `eliminates`" in (
        method_text
    )
    assert "exactly [`missing_preregistration`]" in method_text
    assert "do not apply methods 14 through 23 or compare expected values" in (
        method_text
    )
    assert "null never yields `confirmatory`" in method_text
    assert "does not block completion" in completion[1]


@pytest.mark.parametrize(
    ("field", "expected_rule"),
    tuple(zip(EXPECTED_CONFIRMATION_MATCH_FIELDS, EXPECTED_CONFIRMATION_MATCH_RULES)),
    ids=EXPECTED_CONFIRMATION_MATCH_FIELDS,
)
def test_evidence_update_each_preregistration_mismatch_is_nonconfirmatory(
    field: str, expected_rule: str
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    method = _procedure_rules(_procedure_section(body, "Method"), numbered=True)

    assert expected_rule in method
    mismatch_rule = next(
        rule
        for rule in method
        if "for any missing or deviating confirmation binding" in rule
    )
    assert "`non_confirmatory`" in mismatch_rule
    assert "`confirmation_deviations`" in mismatch_rule
    assert "record every mismatch" in mismatch_rule

    ordinal = EXPECTED_EVIDENCE_METHOD_RULES.index(expected_rule) + 1
    assert 14 <= ordinal <= 23
    old = (
        f"{ordinal}. When `preregistration_ref` is non-null, for `{field}`, require"
    )
    new = f"{ordinal}. Without a preregistration, for `{field}`, permit mismatch and"
    mutated = body.replace(old, new, 1)
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Method",
            EXPECTED_EVIDENCE_METHOD_RULES,
            numbered=True,
        )


@pytest.mark.parametrize(
    ("field", "expected_rule"),
    tuple(
        zip(
            EXPECTED_SCIENTIFIC_CONFIRMATION_FIELDS,
            EXPECTED_SCIENTIFIC_CONFIRMATION_RULES,
        )
    ),
    ids=EXPECTED_SCIENTIFIC_CONFIRMATION_FIELDS,
)
def test_evidence_update_rejects_unrelated_preregistered_scientific_lineage(
    field: str, expected_rule: str
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    method = _procedure_rules(_procedure_section(body, "Method"), numbered=True)

    assert expected_rule in method
    ordinal = EXPECTED_EVIDENCE_METHOD_RULES.index(expected_rule) + 1
    old = (
        f"{ordinal}. When `preregistration_ref` is non-null, require the input"
    )
    new = f"{ordinal}. Accept an unrelated preregistration for the input"
    mutated = body.replace(old, new, 1)
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Method",
            EXPECTED_EVIDENCE_METHOD_RULES,
            numbered=True,
        )

    if field == "mechanism_refs":
        assert "same duplicate-free ordered sequence and the same set" in expected_rule
        assert "current `RivalMechanismSet`" in expected_rule


def test_evidence_update_primary_comparison_match_excludes_observed_outcomes() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    method = _procedure_rules(_procedure_section(body, "Method"), numbered=True)
    rule = EXPECTED_CONFIRMATION_MATCH_RULES[
        EXPECTED_CONFIRMATION_MATCH_FIELDS.index("primary_comparisons")
    ]

    assert rule in method
    assert "comparison definitions" in rule
    assert "metric specifications" in rule
    assert "control specifications" in rule
    assert "analysis specifications" in rule
    assert "never compare observed outcome values" in rule
    mutated = body.replace(
        "never compare observed outcome values",
        "compare observed outcome values",
        1,
    )
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Method",
            EXPECTED_EVIDENCE_METHOD_RULES,
            numbered=True,
        )


def test_evidence_update_forbids_retry_overwrite_scores_and_claim_admission() -> None:
    metadata, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )

    assert metadata["tools"] == [
        "Run.status",
        "Receipt.read",
        "Git.read",
        "Research.observe",
        "Research.checkpoint",
    ]
    _assert_procedure_rules(
        body,
        "Forbidden",
        EXPECTED_EVIDENCE_FORBIDDEN_RULES,
        numbered=False,
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "Only an item whose `operational_state` is `executed` may carry",
            "An item whose `operational_state` is `unavailable` may carry",
        ),
        (
            "Allow one executed item to carry both `negative_result` and",
            "Forbid one executed item from carrying both `negative_result` and",
        ),
        (
            "10. An item whose `operational_state` is `unavailable` or `failed` "
            "must have empty",
            "10. An item whose `operational_state` is `unavailable` or `failed` "
            "may carry relations",
        ),
        ("Preserve every negative result", "Discard every negative result"),
        (
            "cite the specific prior observation that caused the",
            "do not cite the specific prior observation that caused the",
        ),
    ],
)
def test_evidence_update_method_rejects_opposite_evidence_polarity(
    old: str, new: str
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Method",
            EXPECTED_EVIDENCE_METHOD_RULES,
            numbered=True,
        )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "reject missing or inconsistent non-null lineage",
            "allow missing or inconsistent non-null lineage",
        ),
        (
            "require every `relation_targets` value to belong to the input "
            "`mechanism_refs`",
            "permit `relation_targets` outside the input `mechanism_refs`",
        ),
        (
            "Set `confirmation_status` to `confirmatory` only for executed "
            "evidence when",
            "Set `confirmation_status` to `confirmatory` even for unexecuted "
            "evidence when",
        ),
    ],
)
def test_evidence_update_rejects_missing_lineage_and_unrelated_targets(
    old: str, new: str
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Method",
            EXPECTED_EVIDENCE_METHOD_RULES,
            numbered=True,
        )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Inputs",
            "may be null; do not infer omitted lineage",
            "must be non-null; do not infer omitted lineage",
            False,
        ),
        (
            "Method",
            "Executed evidence may still carry",
            "Executed evidence must not carry",
            True,
        ),
        (
            "Method",
            "do not apply methods 14 through 23 or compare",
            "apply methods 14 through 23 and compare",
            True,
        ),
        (
            "Completion",
            "a null `preregistration_ref` is allowed and",
            "a non-null `preregistration_ref` is required and",
            False,
        ),
    ],
)
def test_evidence_update_rejects_non_nullable_preregistration_mutations(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body

    expected = {
        "Inputs": EXPECTED_EVIDENCE_INPUT_RULES,
        "Method": EXPECTED_EVIDENCE_METHOD_RULES,
        "Completion": EXPECTED_EVIDENCE_COMPLETION_RULES,
    }[heading]
    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


def test_evidence_update_forbidden_rules_reject_eval_run_tool_bypass() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-evidence-update.md"
    )
    mutated = body.replace(
        "- Do not call `Eval.run`, create an evaluation, or execute an evaluator;",
        "- Call `Eval.run` to create and execute an evaluation;",
        1,
    )
    assert mutated != body

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            "Forbidden",
            EXPECTED_EVIDENCE_FORBIDDEN_RULES,
            numbered=False,
        )


@pytest.mark.parametrize(
    "name",
    ["aros-independent-review", "aros-claim-package"],
)
def test_independent_review_and_claim_package_frontmatter_and_headings_match_contract(
    name: str,
) -> None:
    path = PROCEDURES_ROOT / f"{name}.md"
    metadata, body = _parse_procedure_frontmatter(path)
    contracts = json.loads(CONTRACTS_PATH.read_text(encoding="utf-8"))
    contract = contracts["procedures"][name]
    raw = path.read_bytes().lower()

    assert metadata == {
        "name": name,
        "source_ids": ["source-1", "source-2"],
        "input": contract["input"],
        "output": contract["output"],
        "tools": contract["tools"],
    }
    assert tuple(re.findall(r"(?m)^## ([^\n]+)$", body)) == (
        EXPECTED_PROCEDURE_HEADINGS
    )
    assert all(
        body.count(f"## {heading}\n") == 1
        for heading in EXPECTED_PROCEDURE_HEADINGS
    )
    assert not any(upstream.encode() in raw for upstream in UPSTREAM_PRODUCT_NAMES)
    assert b"repository:" not in raw
    assert b"commit:" not in raw
    assert b"license:" not in raw
    assert b"/workspace/" not in raw


def test_independent_review_uses_fresh_context_and_reproduces_primary_evidence() -> None:
    metadata, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-independent-review.md"
    )

    assert metadata["tools"] == [
        "Source.read",
        "Run.request",
        "Run.status",
        "Eval.run",
        "Receipt.read",
        "Git.read",
        "Research.petition",
    ]
    _assert_procedure_rules(
        body,
        "Inputs",
        EXPECTED_REVIEW_INPUT_RULES,
        numbered=False,
    )
    assert _required_fields(EXPECTED_REVIEW_INPUT_RULES[1]) == EXPECTED_ARTIFACTS[
        "FrozenEvidencePacket"
    ]
    _assert_procedure_rules(
        body,
        "Method",
        EXPECTED_REVIEW_METHOD_RULES,
        numbered=True,
    )


def test_independent_review_returns_all_report_fields_and_blocks_on_transport() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-independent-review.md"
    )
    output = _procedure_rules(_procedure_section(body, "Output"), numbered=False)

    assert len(output) == 6
    assert output[0] == "Artifact: Return exactly one `ReviewerReport`."
    assert _required_fields(output[1]) == EXPECTED_ARTIFACTS["ReviewerReport"]
    assert output[2] == (
        "Independence binding: Set `reviewer_model_id` and `reviewer_model_family` "
        "exactly from the host-issued review session receipt, and set "
        "`independence_evidence_ref` exactly to input `review_session_receipt_ref`; copy "
        "input `reviewer_session_id` unchanged only after it equals the receipt and active "
        "host Reviewer session or thread."
    )
    assert output[3] == (
        "Lineage binding: Copy input `packet_sha256`, `claim_draft_ref`, and "
        "`candidate_commit` unchanged into the report."
    )
    assert output[4] == (
        "Reproduction binding: In `reproduction_refs`, bind exact candidate, source, "
        "raw-evidence, reproduction, Reviewer Run, Eval, and receipt references with "
        "the exact Researcher identity, Reviewer identity, and independence evidence."
    )
    assert output[5] == (
        "Objection mapping: Put alternative explanations, leakage findings, statistical "
        "findings, scope objections, fatal objections, and unresolved objections only in "
        "their corresponding required fields; retain empty fields explicitly."
    )
    _assert_procedure_rules(
        body,
        "Completion",
        EXPECTED_REVIEW_COMPLETION_RULES,
        numbered=False,
    )
    _assert_procedure_rules(
        body,
        "Forbidden",
        EXPECTED_REVIEW_FORBIDDEN_RULES,
        numbered=False,
    )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Inputs",
            "Start a fresh Reviewer model with empty message history",
            "Continue the Researcher model with its message history",
            False,
        ),
        (
            "Inputs",
            "Researcher transcript, no prior review conversation",
            "Researcher transcript and the prior review conversation",
            False,
        ),
        (
            "Method",
            "Independently rebuild the exact `candidate_commit`",
            "Edit and rebuild `candidate_commit`",
            True,
        ),
        (
            "Method",
            "through a new Reviewer `Run.request`",
            "by reusing the candidate `Run.request`",
            True,
        ),
        (
            "Method",
            "both raw outputs and parsed results",
            "only parsed results",
            True,
        ),
        (
            "Completion",
            "which blocks Claim admission",
            "which does not block Claim admission",
            False,
        ),
        (
            "Forbidden",
            "substitute self-review for the fresh independent review",
            "substitute self-review for the missing independent review",
            False,
        ),
    ],
)
def test_independent_review_rejects_isolation_and_reproduction_polarity_mutations(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-independent-review.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Inputs": EXPECTED_REVIEW_INPUT_RULES,
        "Method": EXPECTED_REVIEW_METHOD_RULES,
        "Completion": EXPECTED_REVIEW_COMPLETION_RULES,
        "Forbidden": EXPECTED_REVIEW_FORBIDDEN_RULES,
    }[heading]
    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Method",
            "one coherent question, Claim, candidate",
            "spliced question, Claim, and candidate",
            True,
        ),
        (
            "Method",
            "model families to be known and different",
            "both model families may be unknown or the same",
            True,
        ),
        (
            "Completion",
            "families are equal",
            "families are equal, unknown, or unverifiable but accepted",
            False,
        ),
        (
            "Completion",
            "references are spliced across lineages, do not emit a `ReviewerReport`",
            "references are spliced across lineages, emit a `ReviewerReport`",
            False,
        ),
        (
            "Completion",
            "exact independence\n  blocker",
            "ignored independence blocker",
            False,
        ),
    ],
)
def test_independent_review_rejects_cross_lineage_and_nonindependent_family(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-independent-review.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Method": EXPECTED_REVIEW_METHOD_RULES,
        "Completion": EXPECTED_REVIEW_COMPLETION_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        ("Method", "excluding `packet_sha256`", "including `packet_sha256`", True),
        ("Method", "`sort_keys=True`", "`sort_keys=False`", True),
        (
            "Method",
            "host-issued before the Reviewer started",
            "self-issued after the Reviewer started",
            True,
        ),
        ("Method", "plain integer 0", "nonzero integer", True),
        (
            "Method",
            "`independence_evidence_ref` exactly to input "
            "`review_session_receipt_ref`",
            "`independence_evidence_ref` to a self-issued receipt",
            True,
        ),
        (
            "Forbidden",
            "Do not issue, synthesize, alter, or substitute "
            "`review_session_receipt_ref`",
            "The Reviewer may issue or substitute `review_session_receipt_ref`",
            False,
        ),
    ],
)
def test_independent_review_rejects_hash_tamper_and_self_issued_receipt(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-independent-review.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Method": EXPECTED_REVIEW_METHOD_RULES,
        "Forbidden": EXPECTED_REVIEW_FORBIDDEN_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Method",
            "`reviewer_session_id` to equal both input `reviewer_session_id`",
            "receipt `reviewer_session_id` may differ from the active session",
            True,
        ),
        (
            "Method",
            "`nonce` to be unique and unused",
            "`nonce` may be replayed",
            True,
        ),
        (
            "Completion",
            "old session or has been replayed",
            "receipt or nonce from an old session may be replayed",
            False,
        ),
        (
            "Forbidden",
            "Do not reuse a review session receipt, `reviewer_session_id`, or nonce",
            "Reuse a review session receipt, `reviewer_session_id`, or nonce",
            False,
        ),
    ],
)
def test_independent_review_rejects_session_receipt_mismatch_and_replay(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-independent-review.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Method": EXPECTED_REVIEW_METHOD_RULES,
        "Completion": EXPECTED_REVIEW_COMPLETION_RULES,
        "Forbidden": EXPECTED_REVIEW_FORBIDDEN_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


def test_claim_package_reads_adjudication_and_answers_every_objection() -> None:
    metadata, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )

    assert metadata["tools"] == [
        "Source.read",
        "Receipt.read",
        "Git.read",
        "Research.checkpoint",
    ]
    _assert_procedure_rules(
        body,
        "Inputs",
        EXPECTED_CLAIM_INPUT_RULES,
        numbered=False,
    )
    assert _required_fields(EXPECTED_CLAIM_INPUT_RULES[1]) == EXPECTED_ARTIFACTS[
        "AdjudicatedEvidence"
    ]
    _assert_procedure_rules(
        body,
        "Method",
        EXPECTED_CLAIM_METHOD_RULES,
        numbered=True,
    )


def test_claim_package_returns_all_fields_after_principal_adjudication() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )
    output = _procedure_rules(_procedure_section(body, "Output"), numbered=False)

    assert len(output) == 8
    assert output[0] == "Artifact: Return exactly one `ClaimPackage`."
    assert _required_fields(output[1]) == EXPECTED_ARTIFACTS["ClaimPackage"]
    assert output[2] == (
        "Disposition authority: Copy input `disposition` exactly; `accept` and `narrow` "
        "may represent only the scoped Claim admitted by the verified Principal response, "
        "while `reject` represents only a rejected adjudication record."
    )
    assert output[3] == (
        "Lineage binding: Copy `root_question_ref`, `candidate_commit`, "
        "`preregistration_ref`, `review_ref`, `principal_response_ref`, and "
        "`reproduction_ref` exactly from the verified input lineage."
    )
    assert output[4] == (
        "Decision authority binding: Copy `principal_authority_ref` and "
        "`principal_decision_receipt_ref` unchanged and set `authority_class` exactly to "
        "their matching `enforcement_class`; `cooperative` remains `cooperative`, and "
        "`protected` is permitted only when both exact canonical receipts say `protected`."
    )
    assert output[5] == (
        "Environment and checkpoint binding: Set `environment_ref` to the exact matching "
        "environment receipt and `checkpoint_ref` exactly to reserved "
        "`planned_checkpoint_ref`; never copy `principal_checkpoint_ref` into output "
        "`checkpoint_ref`."
    )
    assert output[6] == (
        "Evidence binding: Bind every evidence and counterevidence entry, limitation, "
        "uncertainty, reproduction command, and objection to its exact input reference."
    )
    assert output[7] == (
        "Review traceability: Include every material review objection and its explicit "
        "Principal disposition without omission, repair, or reinterpretation."
    )
    _assert_procedure_rules(
        body,
        "Completion",
        EXPECTED_CLAIM_COMPLETION_RULES,
        numbered=False,
    )
    _assert_procedure_rules(
        body,
        "Forbidden",
        EXPECTED_CLAIM_FORBIDDEN_RULES,
        numbered=False,
    )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Method",
            "Enumerate every material Reviewer objection",
            "Omit any inconvenient material Reviewer objection",
            True,
        ),
        (
            "Method",
            "disposition: `accept`, `narrow`, or `reject`",
            "inferred disposition",
            True,
        ),
        (
            "Method",
            "preserving contrary observations, counterexamples, and",
            "discarding contrary observations, counterexamples, and",
            True,
        ),
        (
            "Completion",
            "no fatal\n  objection",
            "fatal objections may remain",
            False,
        ),
        (
            "Completion",
            "fatal objection remains unresolved, do not emit or checkpoint",
            "fatal objection remains unresolved; emit and checkpoint",
            False,
        ),
        (
            "Forbidden",
            "only the Principal may perform Claim\n  admission or adjudication",
            "this procedure may perform Claim\n  admission and adjudication",
            False,
        ),
    ],
)
def test_claim_package_rejects_objection_and_admission_polarity_mutations(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Method": EXPECTED_CLAIM_METHOD_RULES,
        "Completion": EXPECTED_CLAIM_COMPLETION_RULES,
        "Forbidden": EXPECTED_CLAIM_FORBIDDEN_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Method",
            "match exactly across every input and every referenced artifact",
            "need not match across inputs and referenced artifacts",
            True,
        ),
        (
            "Method",
            "decision-receipt `issuer` and `actor` to equal the issuer and actor",
            "decision-receipt `issuer` and `actor` may be unauthorized",
            True,
        ),
        (
            "Method",
            "response, checkpoint, and disposition references",
            "unrelated response, checkpoint, and disposition references",
            True,
        ),
        (
            "Completion",
            "authority context, or enforcement class mismatches",
            "authority context or enforcement class may mismatch",
            False,
        ),
    ],
)
def test_claim_package_rejects_cross_lineage_and_forged_principal_authority(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Method": EXPECTED_CLAIM_METHOD_RULES,
        "Completion": EXPECTED_CLAIM_COMPLETION_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Inputs",
            "canonical AROS receipt store",
            "an inline payload from a filesystem path",
            False,
        ),
        (
            "Method",
            "canonical immutable receipt returned by",
            "inline unbound receipt payload",
            True,
        ),
        (
            "Method",
            "`authority_context_sha256` and `enforcement_class`",
            "mismatched `authority_context_sha256` and `enforcement_class`",
            True,
        ),
        (
            "Forbidden",
            "Do not accept an inline or unbound authority or decision receipt payload",
            "Accept an inline or unbound authority or decision receipt payload",
            False,
        ),
        (
            "Forbidden",
            "path or noncanonical store",
            "path or any noncanonical store",
            False,
        ),
    ],
)
def test_claim_package_rejects_forged_payload_noncanonical_path_and_context_mismatch(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Inputs": EXPECTED_CLAIM_INPUT_RULES,
        "Method": EXPECTED_CLAIM_METHOD_RULES,
        "Forbidden": EXPECTED_CLAIM_FORBIDDEN_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Method",
            "immutable host-issued reservation",
            "mutable self-issued reservation",
            True,
        ),
        (
            "Method",
            "`checkpoint_ref` exactly to reserved",
            "copy `principal_checkpoint_ref` into `checkpoint_ref`",
            True,
        ),
        (
            "Completion",
            "treat the checkpoint as already complete",
            "call the checkpoint again",
            False,
        ),
        (
            "Completion",
            "block as a replay conflict",
            "treat the mismatch as complete",
            False,
        ),
        (
            "Completion",
            "call `Research.checkpoint` exactly once",
            "call `Research.checkpoint` twice",
            False,
        ),
        (
            "Completion",
            "response to the single pending checkpoint call is lost",
            "response to the pending call is lost, call again",
            False,
        ),
        (
            "Forbidden",
            "Do not copy `principal_checkpoint_ref` into output `checkpoint_ref`",
            "Copy `principal_checkpoint_ref` into output `checkpoint_ref`",
            False,
        ),
    ],
)
def test_claim_package_rejects_checkpoint_reservation_mismatch_and_replay(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Method": EXPECTED_CLAIM_METHOD_RULES,
        "Completion": EXPECTED_CLAIM_COMPLETION_RULES,
        "Forbidden": EXPECTED_CLAIM_FORBIDDEN_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


def test_claim_package_calls_checkpoint_once_only_for_pending_status() -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )
    rules = _procedure_rules(_procedure_section(body, "Completion"), numbered=False)

    pending_calls = [
        rule
        for rule in rules
        if "call `Research.checkpoint` exactly once" in rule
    ]
    consumed_recovery = [
        rule
        for rule in rules
        if "status is `consumed`" in rule
        and "do not call `Research.checkpoint`" in rule
        and "return the existing `checkpoint_ref`" in rule
    ]
    response_loss = [
        rule
        for rule in rules
        if "response to the single pending checkpoint call is lost" in rule
        and "do not call `Research.checkpoint` again" in rule
    ]

    assert len(pending_calls) == 1
    assert len(consumed_recovery) == 1
    assert len(response_loss) == 1


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Method",
            "`authority_class` to equal that `enforcement_class`",
            "`authority_class` may differ from that `enforcement_class`",
            True,
        ),
        (
            "Method",
            "must be exactly `cooperative`",
            "may use any enforcement label",
            True,
        ),
        (
            "Forbidden",
            "Do not call cooperative authority protected",
            "Call cooperative authority protected",
            False,
        ),
        (
            "Forbidden",
            "change `authority_class`; preserve",
            "change `authority_class`; upgrade",
            False,
        ),
    ],
)
def test_claim_package_preserves_cooperative_and_protected_authority_class(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Method": EXPECTED_CLAIM_METHOD_RULES,
        "Forbidden": EXPECTED_CLAIM_FORBIDDEN_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


@pytest.mark.parametrize(
    ("heading", "old", "new", "numbered"),
    [
        (
            "Method",
            "do not describe it as an admitted,\n   supported, or scientific negative Claim",
            "describe it as an admitted, supported scientific negative Claim",
            True,
        ),
        (
            "Method",
            "rejection alone is never scientific evidence",
            "rejection alone is scientific evidence",
            True,
        ),
        (
            "Completion",
            "convert rejection into a scientific negative result",
            "convert rejection into a supported scientific Claim",
            False,
        ),
        (
            "Forbidden",
            "rejection is an adjudication outcome, not scientific evidence",
            "rejection is scientific evidence",
            False,
        ),
    ],
)
def test_claim_package_rejects_rejection_laundering(
    heading: str, old: str, new: str, numbered: bool
) -> None:
    _, body = _parse_procedure_frontmatter(
        PROCEDURES_ROOT / "aros-claim-package.md"
    )
    mutated = body.replace(old, new, 1)
    assert mutated != body
    expected = {
        "Method": EXPECTED_CLAIM_METHOD_RULES,
        "Completion": EXPECTED_CLAIM_COMPLETION_RULES,
        "Forbidden": EXPECTED_CLAIM_FORBIDDEN_RULES,
    }[heading]

    with pytest.raises(AssertionError):
        _assert_procedure_rules(
            mutated,
            heading,
            expected,
            numbered=numbered,
        )


def test_contract_loader_fifo_swap_is_prompt_and_leaks_no_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, value, path = _contract_candidate(tmp_path)
    _write_contract_candidate(path, value)
    real_open = os.open
    before_descriptors = set(os.listdir("/proc/self/fd"))

    def replacing_open(candidate: object, flags: int) -> int:
        path.unlink()
        os.mkfifo(path)
        return real_open(candidate, flags)

    def interrupt_blocked_open(_signum: int, _frame: object) -> None:
        raise TimeoutError("contract FIFO open blocked")

    monkeypatch.setattr(module.os, "open", replacing_open)
    previous_handler = signal.signal(signal.SIGALRM, interrupt_blocked_open)
    started = time.monotonic()
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.25)
        with pytest.raises(ValueError, match="regular file"):
            module.load_contracts(path)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)

    assert time.monotonic() - started < 1.0
    assert set(os.listdir("/proc/self/fd")) == before_descriptors


def test_contract_loader_uses_only_standard_library_imports() -> None:
    _contract_module()
    validate_path = PROGRAM_ROOT / "validate.py"
    tree = ast.parse(validate_path.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert imports <= {
        "__future__",
        "dataclasses",
        "hashlib",
        "importlib",
        "json",
        "marshal",
        "math",
        "os",
        "pathlib",
        "re",
        "stat",
        "subprocess",
        "sys",
        "types",
        "typing",
    }


def _copied_program(tmp_path: Path) -> Path:
    copied = tmp_path / "research_program"
    shutil.copytree(
        PROGRAM_ROOT,
        copied,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return copied


def _compile_program_module(program: Path, module_name: str) -> Path:
    source = program / ("__init__.py" if module_name == "__init__" else "validate.py")
    py_compile.compile(str(source), doraise=True)
    return Path(importlib.util.cache_from_source(str(source)))


def test_program_validator_returns_only_canonical_validation_identity(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)

    result = module.validate_program(program)

    sources = json.loads((program / "SOURCES.json").read_text(encoding="utf-8"))[
        "sources"
    ]
    expected_sources = sorted(
        ({"id": source["id"], "commit": source["commit"]} for source in sources),
        key=lambda source: source["id"],
    )
    expected_procedures = [
        {
            "name": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "tools": list(EXPECTED_PROCEDURES[name][2]),
        }
        for name, path in sorted(
            (path.stem, path) for path in (program / "procedures").glob("*.md")
        )
    ]
    expected = {
        "schema_version": 1,
        "state": "valid",
        "sources": expected_sources,
        "contract_sha256": hashlib.sha256(
            (program / "contracts/procedure_contracts.json").read_bytes()
        ).hexdigest(),
        "procedures": expected_procedures,
    }
    assert result == expected
    assert not ({"score", "pass", "quality", "verdict"} & set(result))


def test_program_validator_uses_exact_approved_procedure_hashes(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)

    result = module.validate_program(program)

    assert module._PROCEDURE_SHA256 == APPROVED_PROCEDURE_SHA256
    assert {item["name"]: item["sha256"] for item in result["procedures"]} == (
        APPROVED_PROCEDURE_SHA256
    )


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        (
            "aros-source-research",
            "Build an auditable evidence packet",
            "Build a traceable evidence packet",
        ),
        (
            "aros-rival-mechanisms",
            "Turn bound source evidence",
            "Transform bound source evidence",
        ),
        (
            "aros-experiment-design",
            "Choose one information-seeking experiment",
            "Design one information-seeking experiment",
        ),
        (
            "aros-evidence-update",
            "Turn immutable run, evaluation, and raw evidence",
            "Convert immutable run, evaluation, and raw evidence",
        ),
        (
            "aros-independent-review",
            "Independently reproduce and attack",
            "Independently replicate and challenge",
        ),
        (
            "aros-claim-package",
            "Package a verified Principal adjudication",
            "Assemble a verified Principal adjudication",
        ),
    ],
)
def test_program_validator_rejects_any_procedure_paraphrase(
    tmp_path: Path, name: str, old: str, new: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / f"procedures/{name}.md"
    text = procedure.read_text(encoding="utf-8")
    assert old in text
    procedure.write_text(text.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(ValueError, match="approved SHA-256"):
        module.validate_program(program)


@pytest.mark.parametrize(
    "variant",
    [
        "Use a fixed number of rounds.",
        "The best pilot advances automatically.",
        "Use /bin/sh to run -c experiment.py.",
        "Call os.system to launch the experiment.",
        "Do not allow delays before Bash executes the experiment.",
    ],
)
def test_program_validator_hash_gate_rejects_cited_runtime_variants(
    tmp_path: Path, variant: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace("\n## Output\n", f"\n7. {variant}\n\n## Output\n", 1),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError, match="approved SHA-256|forbidden runtime authority"
    ):
        module.validate_program(program)


def test_program_validator_reads_contract_once_per_binding_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    contract = program / "contracts/procedure_contracts.json"
    real_open = os.open
    contract_opens = 0

    def counting_open(candidate: object, flags: int) -> int:
        nonlocal contract_opens
        if Path(candidate) == contract:
            contract_opens += 1
        return real_open(candidate, flags)

    monkeypatch.setattr(module.os, "open", counting_open)

    module.validate_program(program)

    assert contract_opens == 2


@pytest.mark.parametrize(
    ("relative_path", "target"),
    [
        ("SOURCES.json", SOURCES_PATH),
        ("contracts", PROGRAM_ROOT / "contracts"),
        ("procedures", PROCEDURES_ROOT),
        (
            "procedures/aros-source-research.md",
            PROCEDURES_ROOT / "aros-source-research.md",
        ),
    ],
)
def test_program_validator_rejects_symlinked_program_paths(
    tmp_path: Path, relative_path: str, target: Path
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    candidate = program / relative_path
    if candidate.is_dir():
        shutil.rmtree(candidate)
    else:
        candidate.unlink()
    candidate.symlink_to(target, target_is_directory=target.is_dir())

    with pytest.raises(ValueError, match="symlink"):
        module.validate_program(program)


@pytest.mark.parametrize(
    ("relative_path", "payload"),
    [
        (".provenance", b"hidden\n"),
        ("build/notes.bin", b"generated\n"),
        ("unknown.txt", b"unknown\n"),
        ("procedures/.hidden", b"hidden\n"),
    ],
)
def test_program_validator_rejects_unknown_hidden_and_build_entries(
    tmp_path: Path, relative_path: str, payload: bytes
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    candidate = program / relative_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(payload)

    with pytest.raises(ValueError, match="inventory"):
        module.validate_program(program)


def test_program_validator_rejects_hidden_symlink_before_ignore(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    (program / ".hidden-link").symlink_to(SOURCES_PATH)

    with pytest.raises(ValueError, match="symlink|inventory"):
        module.validate_program(program)


def test_program_validator_revalidates_inventory_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    real_validate_inventory = module._validate_program_inventory
    calls = 0

    def injecting_inventory(root: Path) -> object:
        nonlocal calls
        calls += 1
        bindings = real_validate_inventory(root)
        if calls == 1:
            (root / "unknown.txt").write_text("late\n", encoding="utf-8")
        return bindings

    monkeypatch.setattr(module, "_validate_program_inventory", injecting_inventory)

    with pytest.raises(ValueError, match="inventory"):
        module.validate_program(program)

    assert calls == 2


def test_program_inventory_binds_every_allowed_regular_file(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)

    bindings = module._validate_program_inventory(program)

    expected = {
        "SOURCES.json",
        "__init__.py",
        "validate.py",
        "contracts/procedure_contracts.json",
        *(f"procedures/{name}.md" for name in EXPECTED_PROCEDURES),
    }
    assert set(bindings) == expected
    for relative, binding in bindings.items():
        metadata = (program / relative).lstat()
        assert binding.dev == metadata.st_dev
        assert binding.ino == metadata.st_ino
        assert binding.size == metadata.st_size
        assert binding.mtime_ns == metadata.st_mtime_ns
        assert binding.ctime_ns == metadata.st_ctime_ns
        assert binding.sha256 == hashlib.sha256(
            (program / relative).read_bytes()
        ).hexdigest()
        assert binding.raw == (program / relative).read_bytes()


@pytest.mark.parametrize(
    "relative_path",
    [
        "procedures/aros-source-research.md",
        "contracts/procedure_contracts.json",
        "SOURCES.json",
    ],
)
def test_program_validator_rejects_late_same_name_content_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative_path: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    real_validate_inventory = module._validate_program_inventory
    calls = 0

    def mutating_final_inventory(root: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            candidate = root / relative_path
            candidate.write_bytes(candidate.read_bytes() + b"\n")
        return real_validate_inventory(root)

    monkeypatch.setattr(
        module, "_validate_program_inventory", mutating_final_inventory
    )

    with pytest.raises(ValueError, match="changed during validation"):
        module.validate_program(program)


def test_program_validator_rejects_late_same_content_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    real_validate_inventory = module._validate_program_inventory
    calls = 0

    def replacing_final_inventory(root: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            candidate = root / "SOURCES.json"
            original = candidate.read_bytes()
            candidate.unlink()
            candidate.write_bytes(original)
        return real_validate_inventory(root)

    monkeypatch.setattr(
        module, "_validate_program_inventory", replacing_final_inventory
    )

    with pytest.raises(ValueError, match="changed during validation"):
        module.validate_program(program)


def test_program_validator_rejects_late_mutate_then_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    real_validate_inventory = module._validate_program_inventory
    calls = 0

    def restoring_final_inventory(root: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            candidate = root / "contracts/procedure_contracts.json"
            original = candidate.read_bytes()
            candidate.write_bytes(original + b"\n")
            candidate.write_bytes(original)
        return real_validate_inventory(root)

    monkeypatch.setattr(
        module, "_validate_program_inventory", restoring_final_inventory
    )

    with pytest.raises(ValueError, match="changed during validation"):
        module.validate_program(program)


def test_program_validator_rejects_transient_procedure_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedures = program / "procedures"
    alternate = tmp_path / "alternate-procedures"
    held = tmp_path / "held-procedures"
    shutil.copytree(procedures, alternate)
    alternate_source = alternate / "aros-source-research.md"
    alternate_source.write_text(
        alternate_source.read_text(encoding="utf-8").replace(
            "Build an auditable evidence packet",
            "Build a traceable evidence packet",
            1,
        ),
        encoding="utf-8",
    )
    source_path = procedures / "aros-source-research.md"
    real_read_bound = module._read_bound_regular_bytes
    source_reads = 0

    def swapping_read(
        path: Path, *, label: str, limit: int
    ) -> tuple[bytes, object]:
        nonlocal source_reads
        if Path(path) != source_path:
            return real_read_bound(path, label=label, limit=limit)
        source_reads += 1
        if source_reads != 2:
            return real_read_bound(path, label=label, limit=limit)
        procedures.rename(held)
        alternate.rename(procedures)
        try:
            return real_read_bound(path, label=label, limit=limit)
        finally:
            procedures.rename(alternate)
            held.rename(procedures)

    monkeypatch.setattr(module, "_read_bound_regular_bytes", swapping_read)

    with pytest.raises(ValueError, match="changed during validation"):
        module.validate_program(program)


def test_program_validator_parses_retained_bytes_during_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedures = program / "procedures"
    alternate = tmp_path / "alternate-procedures"
    held = tmp_path / "held-procedures"
    shutil.copytree(procedures, alternate)
    alternate_source = alternate / "aros-source-research.md"
    alternate_source.write_text(
        alternate_source.read_text(encoding="utf-8").replace(
            "Build an auditable evidence packet",
            "Build a traceable evidence packet",
            1,
        ),
        encoding="utf-8",
    )
    real_parse_frontmatter = module._parse_frontmatter
    swapped = False

    def swapping_parse(text: str, name: str) -> tuple[dict[str, object], str]:
        nonlocal swapped
        if name != "aros-source-research":
            return real_parse_frontmatter(text, name)
        procedures.rename(held)
        alternate.rename(procedures)
        swapped = True
        try:
            assert "Build a traceable evidence packet" in (
                procedures / "aros-source-research.md"
            ).read_text(encoding="utf-8")
            return real_parse_frontmatter(text, name)
        finally:
            procedures.rename(alternate)
            held.rename(procedures)

    monkeypatch.setattr(module, "_parse_frontmatter", swapping_parse)

    result = module.validate_program(program)

    assert swapped
    assert result["state"] == "valid"
    assert {item["name"]: item["sha256"] for item in result["procedures"]} == (
        APPROVED_PROCEDURE_SHA256
    )


def test_program_validator_returns_hashes_from_final_bound_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    real_validate_inventory = module._validate_program_inventory
    inventories: list[object] = []

    def capturing_inventory(root: Path) -> object:
        bindings = real_validate_inventory(root)
        inventories.append(bindings)
        return bindings

    monkeypatch.setattr(module, "_validate_program_inventory", capturing_inventory)

    result = module.validate_program(program)

    final = inventories[-1]
    assert result["contract_sha256"] == final[
        "contracts/procedure_contracts.json"
    ].sha256
    assert {item["name"]: item["sha256"] for item in result["procedures"]} == {
        name: final[f"procedures/{name}.md"].sha256
        for name in EXPECTED_PROCEDURES
    }


def test_source_isolation_scans_retained_bytes_when_live_path_is_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    validate_path = program / "validate.py"
    validate_path.write_text(
        validate_path.read_text(encoding="utf-8")
        + f"\nUPSTREAM = {UPSTREAM_PRODUCT_NAMES[0]!r}\n",
        encoding="utf-8",
    )
    held = tmp_path / "held-validate.py"
    real_validate_isolation = module._validate_source_isolation

    def moving_isolation(
        root: Path,
        sources_path: Path,
        sources: object,
        retained_content: object,
    ) -> None:
        validate_path.rename(held)
        try:
            real_validate_isolation(
                root, sources_path, sources, retained_content
            )
        finally:
            held.rename(validate_path)

    monkeypatch.setattr(module, "_validate_source_isolation", moving_isolation)

    with pytest.raises(ValueError, match="upstream product name"):
        module.validate_program(program)


def test_program_validator_allows_only_module_bytecode_cache(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    _compile_program_module(program, "__init__")
    _compile_program_module(program, "validate")

    assert module.validate_program(program)["state"] == "valid"


@pytest.mark.parametrize(
    "invalidation_mode",
    [
        py_compile.PycInvalidationMode.CHECKED_HASH,
        py_compile.PycInvalidationMode.UNCHECKED_HASH,
    ],
)
def test_program_validator_allows_source_bound_hash_bytecode(
    tmp_path: Path, invalidation_mode: py_compile.PycInvalidationMode
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    validate_path = program / "validate.py"
    py_compile.compile(
        str(validate_path),
        doraise=True,
        invalidation_mode=invalidation_mode,
    )

    assert module.validate_program(program)["state"] == "valid"


def test_program_validator_rejects_empty_bytecode_cache(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    (program / "__pycache__").mkdir()

    with pytest.raises(ValueError, match="inventory"):
        module.validate_program(program)


@pytest.mark.parametrize("sensitive", ["sources", "commit", "repository"])
def test_program_validator_rejects_source_details_embedded_in_bytecode(
    tmp_path: Path, sensitive: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources_path = program / "SOURCES.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    if sensitive == "sources":
        value = sources_path.read_text(encoding="utf-8")
    else:
        value = sources["sources"][0][sensitive]
    validate_path = program / "validate.py"
    original = validate_path.read_text(encoding="utf-8")
    validate_path.write_text(
        f"{original}\nSENSITIVE = {value!r}\n", encoding="utf-8"
    )
    _compile_program_module(program, "validate")
    validate_path.write_text(original, encoding="utf-8")

    with pytest.raises(ValueError, match="source details|provenance"):
        module.validate_program(program)


def test_program_validator_rejects_exact_unicode_source_detail_bytes(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources_path = program / "SOURCES.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    sensitive = "ÄAAAAAAA"
    sources["sources"][0]["adaptation"] = sensitive
    sources_path.write_text(
        json.dumps(sources, ensure_ascii=False), encoding="utf-8"
    )
    validate_path = program / "validate.py"
    validate_path.write_text(
        validate_path.read_text(encoding="utf-8")
        + f"\nSENSITIVE = {sensitive!r}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source details"):
        module.validate_program(program)


@pytest.mark.parametrize("damage", ["magic", "flags"])
def test_program_validator_rejects_invalid_bytecode_header(
    tmp_path: Path, damage: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    cache_path = _compile_program_module(program, "validate")
    raw = bytearray(cache_path.read_bytes())
    if damage == "magic":
        raw[0] ^= 0xFF
    else:
        raw[4:8] = (4).to_bytes(4, "little")
    cache_path.write_bytes(raw)

    with pytest.raises(ValueError, match="inventory|bytecode"):
        module.validate_program(program)


def test_program_validator_rejects_wrong_bytecode_implementation_tag(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    cache_path = _compile_program_module(program, "validate")
    cache_path.rename(cache_path.with_name("validate.cpython-999.pyc"))

    with pytest.raises(ValueError, match="inventory|bytecode"):
        module.validate_program(program)


def test_program_validator_rejects_bytecode_for_wrong_source_module(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    cache_path = Path(
        importlib.util.cache_from_source(str(program / "validate.py"))
    )
    cache_path.parent.mkdir()
    unrelated = tmp_path / "unrelated.py"
    unrelated.write_text("value = 1\n", encoding="utf-8")
    py_compile.compile(str(unrelated), cfile=str(cache_path), doraise=True)

    with pytest.raises(ValueError, match="inventory|bytecode"):
        module.validate_program(program)


def test_program_validator_rejects_bytecode_from_same_named_other_source(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    cache_path = Path(
        importlib.util.cache_from_source(str(program / "validate.py"))
    )
    cache_path.parent.mkdir()
    other = tmp_path / "other/validate.py"
    other.parent.mkdir()
    other.write_text("unrelated = True\n", encoding="utf-8")
    py_compile.compile(
        str(other),
        cfile=str(cache_path),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )

    with pytest.raises(ValueError, match="inventory|bytecode"):
        module.validate_program(program)


@pytest.mark.parametrize(
    "relative_path",
    [
        "__pycache__/unknown.cpython-312.pyc",
        "__pycache__/validate.evil.pyc",
        "__pycache__/validate.txt",
        "procedures/__pycache__/validate.cpython-312.pyc",
    ],
)
def test_program_validator_rejects_non_module_bytecode_cache_entries(
    tmp_path: Path, relative_path: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    candidate = program / relative_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"bytecode")

    with pytest.raises(ValueError, match="inventory"):
        module.validate_program(program)


@pytest.mark.parametrize("payload", [b"\xff", b" " * (128 * 1024 + 1)])
def test_program_validator_rejects_non_utf8_or_oversize_procedure(
    tmp_path: Path, payload: bytes
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-source-research.md"
    procedure.write_bytes(payload)

    with pytest.raises(ValueError, match="UTF-8|128 KiB"):
        module.validate_program(program)


def test_program_validator_rejects_unknown_procedure_filename(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    (program / "procedures/extra.md").write_text(
        (program / "procedures/aros-source-research.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inventory|exactly the six"):
        module.validate_program(program)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("name: aros-source-research", "name: renamed", "name"),
        ("  - Source.search", "  - Git.read", "contract"),
        ("source_ids:\n", "source_ids: source-1\n", "frontmatter"),
        ("input: ResearchQuestion", "input: SourcePacket", "contract"),
    ],
)
def test_program_validator_rejects_frontmatter_contract_drift(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-source-research.md"
    text = procedure.read_text(encoding="utf-8")
    assert old in text
    procedure.write_text(text.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        module.validate_program(program)


@pytest.mark.parametrize(
    "insertion",
    [
        "name: duplicate\n",
        "unknown: value\n",
        "tools: Git.read\n",
    ],
)
def test_program_validator_rejects_duplicate_unknown_or_wrong_type_frontmatter(
    tmp_path: Path, insertion: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-source-research.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(text.replace("---\n", f"---\n{insertion}", 1), encoding="utf-8")

    with pytest.raises(ValueError, match="frontmatter"):
        module.validate_program(program)


def test_program_validator_rejects_reordered_or_duplicate_headings(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-rival-mechanisms.md"
    text = procedure.read_text(encoding="utf-8")
    text = text.replace("## Purpose", "## Temporary", 1)
    text = text.replace("## Inputs", "## Purpose", 1)
    text = text.replace("## Temporary", "## Inputs", 1)
    procedure.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="headings"):
        module.validate_program(program)


def test_program_validator_rejects_duplicate_source_json_key(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources = program / "SOURCES.json"
    text = sources.read_text(encoding="utf-8")
    sources.write_text(
        text.replace(
            '"schema_version": 1,',
            '"schema_version": 1,\n  "schema_version": 1,',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate JSON key"):
        module.validate_program(program)


def test_program_validator_rejects_duplicate_source_id(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources_path = program / "SOURCES.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    sources["sources"][1]["id"] = sources["sources"][0]["id"]
    sources_path.write_text(json.dumps(sources), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate source id"):
        module.validate_program(program)


@pytest.mark.parametrize("mutation", ["tree", "directory", "duplicate_path"])
def test_program_validator_verifies_source_commits_and_blobs(
    tmp_path: Path, mutation: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources_path = program / "SOURCES.json"
    value = json.loads(sources_path.read_text(encoding="utf-8"))
    source = value["sources"][0]
    repository = Path(source["repository"])
    if mutation == "tree":
        source["commit"] = _git(repository, "rev-parse", f'{source["commit"]}^{{tree}}')
    elif mutation == "directory":
        source["selected_paths"][0] = "skills"
    else:
        source["selected_paths"].append(source["selected_paths"][0])
    sources_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="Git|commit|blob|duplicate"):
        module.validate_program(program)


def test_source_loader_ignores_git_replace_objects(tmp_path: Path) -> None:
    module = _contract_module()
    repository = tmp_path / "source-repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "source@example.invalid")
    _git(repository, "config", "user.name", "Source Test")
    (repository / "original.txt").write_text("original\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "original")
    original_commit = _git(repository, "rev-parse", "HEAD")
    (repository / "replacement-only.txt").write_text(
        "replacement\n", encoding="utf-8"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "replacement")
    replacement_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "replace", original_commit, replacement_commit)
    sources_path = tmp_path / "SOURCES.json"
    sources_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "id": "source-1",
                        "repository": str(repository),
                        "commit": original_commit,
                        "license": "MIT",
                        "selected_paths": ["replacement-only.txt"],
                        "adaptation": "Test replace-object isolation.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Git|blob"):
        module.load_sources(sources_path)


def test_program_validator_rejects_second_source_record(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    (program / "provenance.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="inventory|source or provenance"):
        module.validate_program(program)


@pytest.mark.parametrize(
    "grant",
    [
        "Use Bash to execute the experiment directly.",
        "Launch a subprocess for remote execution.",
        "Submit the work to a job queue and upload the result.",
        "Publish the result and merge it automatically.",
        "Use scheduler authority to send notifications.",
        "Use a score threshold and a fixed-round rule.",
        "Compute a numeric score and rank the highest result first.",
        "Automatically select a top winner.",
    ],
)
def test_program_validator_rejects_runtime_authority_granted_by_method(
    tmp_path: Path, grant: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace("\n## Output\n", f"\n7. {grant}\n\n## Output\n", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden runtime authority"):
        module.validate_program(program)


@pytest.mark.parametrize(
    "grant",
    [
        "Use sh -c to perform the experiment.",
        "Use bash -c to perform the experiment.",
        "Use zsh -c to perform the experiment.",
        "Use a shell command to perform the experiment.",
        "Use a subprocess to perform the experiment.",
        "Use exactly ten rounds before stopping.",
        "Use a fixed 10 rounds before stopping.",
        "Use four iterations before stopping.",
        "Use 3 cycles before stopping.",
        "Use thirty rounds before stopping.",
        "Use one hundred and ten cycles before stopping.",
        "Choose the best pilot result automatically.",
        "Automatically select the top result.",
        "Automatically pick the best pilot.",
        "Automatically choose a winner.",
        "Auto choose the best pilot result.",
        "Auto-pick the best pilot result.",
        "Automatically choosing the top result is required.",
        "The best pilot was chosen automatically.",
    ],
)
def test_program_validator_rejects_shell_fixed_round_and_auto_choice_variants(
    tmp_path: Path, grant: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace("\n## Output\n", f"\n7. {grant}\n\n## Output\n", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden runtime authority"):
        module.validate_program(program)


@pytest.mark.parametrize(
    "prohibition",
    [
        "Do not use sh -c to perform the experiment.",
        "Do not use exactly ten rounds before stopping.",
        "Do not use thirty rounds before stopping.",
        "Do not choose the best pilot result automatically.",
        "Do not auto-pick the best pilot result.",
    ],
)
def test_program_validator_allows_direct_new_runtime_prohibitions(
    tmp_path: Path, prohibition: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(f"{text.rstrip()}\n- {prohibition}\n", encoding="utf-8")

    _, body = module._parse_frontmatter(
        procedure.read_text(encoding="utf-8"), "aros-experiment-design"
    )
    sections = module._sections(body, "aros-experiment-design")
    module._validate_runtime_actions(sections, "aros-experiment-design")


@pytest.mark.parametrize(
    "grant",
    [
        "Use Bash to run the experiment.\n\n",
        "Compute a numeric score and rank the highest result first.\n\n",
    ],
)
def test_program_validator_rejects_authority_before_first_heading(
    tmp_path: Path, grant: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace("---\n\n## Purpose", f"---\n\n{grant}## Purpose", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="heading|forbidden runtime authority"):
        module.validate_program(program)


def test_program_validator_allows_prohibitions_but_rejects_reversed_polarity(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    assert "Do not use a shell" in text
    procedure.write_text(
        text.replace("Do not use a shell", "Do not prohibit shell use", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden runtime authority|prohibitions"):
        module.validate_program(program)


def test_program_validator_rejects_grant_after_prohibition_in_same_rule(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace(
            "Do not use a shell, subprocess, SSH, remote execution, job queue, upload, or\n"
            "  notification service.",
            "Do not use a shell. Use Bash to execute the experiment.",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden runtime authority"):
        module.validate_program(program)


def test_program_validator_does_not_borrow_unrelated_negative_polarity(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace(
            "\n## Output\n",
            "\n7. Proceed without delay and use Bash to execute the experiment.\n"
            "\n## Output\n",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden runtime authority"):
        module.validate_program(program)


@pytest.mark.parametrize(
    "grant",
    [
        "Do not hesitate to use Bash to execute the experiment.",
        "It is not an error to use Bash to execute the experiment.",
        "Do not use delays while Bash is authorized for direct work.",
        "Do not use delays, Bash is permitted for direct work.",
    ],
)
def test_program_validator_rejects_negation_complement_grants(
    tmp_path: Path, grant: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-experiment-design.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace("\n## Output\n", f"\n7. {grant}\n\n## Output\n", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forbidden runtime authority"):
        module.validate_program(program)


def test_program_validator_keeps_exact_source_details_only_in_sources(
    tmp_path: Path,
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources = json.loads((program / "SOURCES.json").read_text(encoding="utf-8"))
    leaked_commit = sources["sources"][0]["commit"]
    procedure = program / "procedures/aros-source-research.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace(
            "Build an auditable",
            f"Inspect source commit {leaked_commit} and build an auditable",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inventory|source details"):
        module.validate_program(program)


def test_program_validator_scans_yaml_for_source_detail_leaks(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources = json.loads((program / "SOURCES.json").read_text(encoding="utf-8"))
    leaked_commit = sources["sources"][0]["commit"]
    (program / "notes.yaml").write_text(
        f"source_commit: {leaked_commit}\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="inventory|source details"):
        module.validate_program(program)


@pytest.mark.parametrize("suffix", [".toml", ".bin"])
def test_program_validator_scans_every_file_for_source_detail_leaks(
    tmp_path: Path, suffix: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources = json.loads((program / "SOURCES.json").read_text(encoding="utf-8"))
    leaked_commit = sources["sources"][0]["commit"]
    (program / f"notes{suffix}").write_bytes(leaked_commit.encode())

    with pytest.raises(ValueError, match="inventory|source details"):
        module.validate_program(program)


def test_program_validator_errors_are_bounded(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    sources_path = program / "SOURCES.json"
    oversized_key = "x" * 100_000
    sources_path.write_text(
        json.dumps({"schema_version": 1, "sources": [], oversized_key: None}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as error:
        module.validate_program(program)

    assert len(str(error.value)) <= 512


def test_program_validator_path_errors_are_bounded(tmp_path: Path) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    directory = program
    for index in range(15):
        directory /= f"{index:02d}-" + "x" * 190
        directory.mkdir()
    (directory / "linked").symlink_to(SOURCES_PATH)

    with pytest.raises(ValueError) as error:
        module.validate_program(program)

    assert len(str(error.value)) <= 512


@pytest.mark.parametrize("upstream_name", UPSTREAM_PRODUCT_NAMES)
def test_program_validator_allows_upstream_names_only_in_sources(
    tmp_path: Path, upstream_name: str
) -> None:
    module = _contract_module()
    program = _copied_program(tmp_path)
    procedure = program / "procedures/aros-source-research.md"
    text = procedure.read_text(encoding="utf-8")
    procedure.write_text(
        text.replace("Build an auditable", f"Use {upstream_name} to build an auditable", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="upstream product name"):
        module.validate_program(program)
