"""Bounded, read-only ResearchAttentionPacket behavior tests."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import arbor.aros.attention as attention_module
import arbor.aros.store as store_module
from arbor.aros.attention import (
    AttentionAuthorityContext,
    ResearchAttentionService,
)
from arbor.aros.runs import RunService
from arbor.aros.store import atomic_write_json
from arbor.aros.workspace import boot_packet, boot_workspace, init_workspace
from arbor.aros.worktrees import bind_repository
from tests import test_aros_observations as observation_support


TOP_LEVEL_KEYS = {
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


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_git(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "attention@example.invalid")
    _git(root, "config", "user.name", "Attention Test")


def _commit_workspace(root: Path, message: str = "checkpoint") -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


def _init_workspace(root: Path, *, with_question: bool = True) -> str:
    _init_git(root)
    init_workspace(root, "Determine the load-bearing mechanism.")
    if with_question:
        _write_question_views(root)
    return _commit_workspace(root, "initialize attention workspace")


def _write_question_views(root: Path, *, marker: str = "canonical") -> None:
    (root / "questions" / "FRONTIER.md").write_text(
        "---\nfocus_question: Q-0001\n---\n# Research Frontier\n",
        encoding="utf-8",
    )
    question = root / "questions" / "Q-0001" / "question.md"
    question.parent.mkdir(parents=True, exist_ok=True)
    question.write_text(
        "---\n"
        "id: Q-0001\n"
        "status: open\n"
        "---\n"
        "# Question\n\n"
        f"Does the {marker} mediator determine the outcome?\n\n"
        "## Current best answer\n\n"
        f"Exact {marker} answer.\n\n"
        "## Current uncertainty\n\n"
        f"Exact {marker} uncertainty.\n\n"
        "## Resolution criterion\n\n"
        "Resolve only after the preregistered contrast.\n\n"
        "## Stop / pivot criterion\n\n"
        "Pivot if the controlled contrast is null.\n\n"
        "## Expected information gain\n\n"
        "High only for the controlled contrast.\n",
        encoding="utf-8",
    )
    (root / "memory" / "NOW.md").write_text(
        "# Current State\n\n"
        "## Current uncertainty\n\n"
        f"NOW preserves {marker} uncertainty verbatim.\n\n"
        "## Current obligations\n\n"
        "Run the preregistered control before changing the claim.\n",
        encoding="utf-8",
    )
    (root / "model" / "CURRENT.md").write_text(
        "# Current Model\n\n"
        "## Current uncertainty\n\n"
        f"Model preserves {marker} uncertainty verbatim.\n",
        encoding="utf-8",
    )


def _finish_observation_workspace(root: Path) -> str:
    init_workspace(root, "Inspect the returned observation.")
    return _commit_workspace(root, "add attention views")


def _compact_json(packet: dict[str, object]) -> str:
    return json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _section_map(packet: dict[str, object]) -> dict[str, dict[str, object]]:
    active = packet["active_question"]
    assert isinstance(active, dict)
    sections = active["sections"]
    assert isinstance(sections, list)
    return {str(section["heading"]): section for section in sections}


def _paths(entries: object) -> set[str]:
    assert isinstance(entries, list)
    return {str(entry["path"]) for entry in entries}


def _mark_run_lost(root: Path, service: RunService, run_id: str) -> None:
    status = service.status(run_id, reconcile=False)
    status.update(
        {
            "state": "lost",
            "reason": "process_absent_without_final_receipt",
        }
    )
    atomic_write_json(root / ".aros" / "runs" / run_id / "status.json", status)


def test_attention_packet_has_exact_top_level_shape(tmp_path: Path) -> None:
    _init_workspace(tmp_path)

    packet = ResearchAttentionService(tmp_path).build()

    assert set(packet) == TOP_LEVEL_KEYS
    assert packet["schema_version"] == 1
    assert packet["recent_evidence_delta"] == []
    assert packet["hypotheses"].keys() == {"leading", "competing"}
    assert packet["current_obligations"].keys() == {
        "scientific",
        "institutional",
    }


def test_attention_packet_is_deterministic_and_not_persisted(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(tmp_path).parts
    }
    status_before = _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all")
    service = ResearchAttentionService(tmp_path)

    first = service.build()
    second = service.build()

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(tmp_path).parts
    }
    assert first == second
    assert before == after
    assert _git(tmp_path, "status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert "generated_at" not in _compact_json(first)


def test_attention_separates_head_from_dirty_pending_edits(tmp_path: Path) -> None:
    head = _init_workspace(tmp_path)
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Current uncertainty\n\nDIRTY_PENDING_MEANING\n",
        encoding="utf-8",
    )

    packet = ResearchAttentionService(tmp_path).build()

    snapshot = packet["snapshot"]
    assert snapshot["canonical"] == head
    assert snapshot["candidate"]["head"] == head
    assert "memory/NOW.md" in _paths(
        snapshot["candidate"]["git_status"]["dirty_paths"]
    )
    assert "DIRTY_PENDING_MEANING" not in _compact_json(packet)
    assert "NOW preserves canonical uncertainty verbatim." in _compact_json(packet)


def test_attention_quotes_question_sections_without_summarizing(tmp_path: Path) -> None:
    _init_workspace(tmp_path)

    packet = ResearchAttentionService(tmp_path).build()
    sections = _section_map(packet)

    assert sections["Question"]["excerpt"] == (
        "Does the canonical mediator determine the outcome?"
    )
    assert sections["Current best answer"]["excerpt"] == "Exact canonical answer."
    assert sections["Current uncertainty"]["excerpt"] == (
        "Exact canonical uncertainty."
    )
    assert sections["Resolution criterion"]["excerpt"] == (
        "Resolve only after the preregistered contrast."
    )
    assert sections["Stop / pivot criterion"]["excerpt"] == (
        "Pivot if the controlled contrast is null."
    )
    assert sections["Expected information gain"]["excerpt"] == (
        "High only for the controlled contrast."
    )
    assert all(section["path"] == "questions/Q-0001/question.md" for section in sections.values())


def test_attention_deduplicates_eval_linked_run(tmp_path: Path) -> None:
    installed = observation_support._install_eval_receipt(tmp_path)
    _finish_observation_workspace(tmp_path)

    packet = ResearchAttentionService(tmp_path).build()

    returns = packet["unassimilated_returns"]
    assert [item["ref"] for item in returns] == [installed["receipt_ref"]]
    assert returns[0]["kind"] == "measurement"
    assert all(item["kind"] != "run_final" for item in returns)


def test_attention_terminal_record_appears_only_in_unassimilated(tmp_path: Path) -> None:
    _service, manifest, _final = observation_support._install_run_final(tmp_path)
    _finish_observation_workspace(tmp_path)
    ref = f"runs/{manifest['run_id']}/final.json"

    packet = ResearchAttentionService(tmp_path).build()

    assert [item["ref"] for item in packet["unassimilated_returns"]] == [ref]
    assert all(item["ref"] != ref for item in packet["pending_measurements"])


def test_attention_lost_without_receipt_appears_only_in_pending_measurements(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path, with_question=False)
    service = RunService(tmp_path)
    manifest = service.prepare(
        [sys.executable, "-c", "pass"],
        idempotency_key="lost-attention-run",
        actor="principal",
        security_profile="trusted-local",
    )
    run_id = str(manifest["run_id"])
    _mark_run_lost(tmp_path, service, run_id)

    packet = ResearchAttentionService(tmp_path).build()

    assert [item["ref"] for item in packet["pending_measurements"]] == [
        f"runs/{run_id}/manifest.json"
    ]
    assert packet["pending_measurements"][0]["process_state"] == "lost"
    assert packet["unassimilated_returns"] == []


def test_attention_missing_authority_is_unavailable_not_invented(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path, with_question=False)

    packet = ResearchAttentionService(tmp_path).build()

    assert packet["authority"] == {
        "state": "unavailable",
        "enforcement_class": "unavailable",
        "reason": "host_context_not_supplied",
    }
    assert packet["remaining_budget"] == {
        "state": "not_configured",
        "enforcement_class": "unavailable",
        "reason": "host_context_not_supplied",
    }
    assert "capabilities" not in packet["authority"]


def test_attention_json_and_text_share_one_object(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    service = ResearchAttentionService(tmp_path)

    packet = boot_packet(tmp_path)
    rendered = service.render_text(packet)

    assert json.loads(rendered) == packet
    assert json.loads(boot_workspace(tmp_path)) == packet
    assert rendered == _compact_json(packet)


def test_attention_bounds_multibyte_text_and_reports_omissions(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    question = tmp_path / "questions" / "Q-0001" / "question.md"
    original = question.read_text(encoding="utf-8")
    question.write_text(original.replace("Exact canonical uncertainty.", "研究🙂" * 2_000), encoding="utf-8")
    rivals = tmp_path / "model" / "rivals"
    rivals.mkdir()
    for index in range(40):
        (rivals / f"R-{index:02d}.md").write_text(
            f"# Rival {index}\n\n竞争🙂" * 20,
            encoding="utf-8",
        )
    _commit_workspace(tmp_path, "add oversized semantic views")
    service = ResearchAttentionService(tmp_path)

    packet = service.build(max_chars=8_000)
    encoded = _compact_json(packet)
    rendered = service.render_text(packet)

    assert len(encoded) <= 8_000
    assert len(rendered) <= 8_000
    assert len(encoded.encode("utf-8")) > len(encoded)
    assert encoded.encode("utf-8").decode("utf-8") == encoded
    assert "truncated" in packet["warnings"]
    assert packet["omitted"]
    assert any(count > 0 for count in packet["omitted"].values())

    minimum = service.build(max_chars=512)
    assert len(_compact_json(minimum)) <= 512
    assert len(service.render_text(minimum)) <= 512
    assert sum(minimum["omitted"].values()) > 0
    assert "aros boot" in minimum["omitted"]
    for invalid in (511, 16_001, True):
        with pytest.raises(ValueError, match="max_chars"):
            service.build(max_chars=invalid)


def test_attention_minimum_budget_preserves_shapes_and_accounts_omissions(
    tmp_path: Path,
) -> None:
    _terminal_service, terminal_manifest, _final = (
        observation_support._install_run_final(tmp_path)
    )
    init_workspace(tmp_path, "Inspect complete and pending work.")
    _write_question_views(tmp_path)
    now = tmp_path / "memory" / "NOW.md"
    now.write_text(
        now.read_text(encoding="utf-8")
        + "\n## Current blockers\n\nAwait the calibration fixture.\n",
        encoding="utf-8",
    )
    rivals = tmp_path / "model" / "rivals"
    rivals.mkdir()
    (rivals / "R-0001.md").write_text("# Rival\n", encoding="utf-8")
    head = _commit_workspace(tmp_path, "populate minimum attention fixture")
    runs = RunService(tmp_path)
    pending_manifest = runs.prepare(
        [sys.executable, "-c", "pass"],
        idempotency_key="minimum-attention-pending",
        actor="principal",
        security_profile="trusted-local",
    )
    now.write_text("DIRTY_CANDIDATE_MEANING", encoding="utf-8")
    context = AttentionAuthorityContext(
        authority={
            "state": "available",
            "enforcement_class": "hard",
            "capabilities": ["inspect"],
        },
        remaining_budget={
            "state": "exhausted",
            "enforcement_class": "hard",
            "remaining_tokens": 0,
        },
        institutional_obligations=(
            {"obligation_id": "ACK-1", "kind": "review"},
        ),
    )
    service = ResearchAttentionService(tmp_path)
    roomy = service.build(max_chars=16_000, context=context)

    assert roomy["active_question"]
    assert roomy["current_uncertainty"]
    assert roomy["hypotheses"]["competing"]
    assert [item["ref"] for item in roomy["pending_measurements"]] == [
        f"runs/{pending_manifest['run_id']}/manifest.json"
    ]
    assert [item["ref"] for item in roomy["unassimilated_returns"]] == [
        f"runs/{terminal_manifest['run_id']}/final.json"
    ]
    assert roomy["current_obligations"]["scientific"]
    assert roomy["current_obligations"]["institutional"]
    assert roomy["blocked_reasons"]

    packet = service.build(max_chars=512, context=context)
    encoded = _compact_json(packet)

    assert set(packet) == TOP_LEVEL_KEYS
    assert packet["authority"] == {"state": "available"}
    assert packet["remaining_budget"] == {"state": "exhausted"}
    assert set(packet["hypotheses"]) == {"leading", "competing"}
    assert set(packet["current_obligations"]) == {
        "scientific",
        "institutional",
    }
    assert packet["snapshot"] == {"canonical": head, "candidate": {}}
    assert "DIRTY_CANDIDATE_MEANING" not in encoded
    assert {"index_incomplete", "truncated"} <= set(packet["warnings"])
    assert sum(packet["omitted"].values()) > 10
    assert "aros boot" in packet["omitted"]
    assert len(encoded) <= 512
    assert json.loads(service.render_text(packet)) == packet


def test_attention_reads_sections_after_one_megabyte_without_rejecting_view(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    padding = "x" * (1_048_576 + 200)
    question = tmp_path / "questions" / "Q-0001" / "question.md"
    question.write_text(
        "---\nid: Q-0001\nstatus: open\n---\n"
        "# Question\n\n"
        f"{padding}\n\n"
        "## Current best answer\n\nLate exact answer.\n\n"
        "## Current uncertainty\n\nLate exact question uncertainty.\n\n"
        "## Resolution criterion\n\nLate resolution criterion.\n\n"
        "## Stop / pivot criterion\n\nLate stop criterion.\n\n"
        "## Expected information gain\n\nLate information gain.\n",
        encoding="utf-8",
    )
    now = tmp_path / "memory" / "NOW.md"
    now.write_text(
        f"# Current State\n\n{padding}\n\n"
        "## Current uncertainty\n\nLate exact NOW uncertainty.\n",
        encoding="utf-8",
    )
    _commit_workspace(tmp_path, "store large canonical semantic views")

    packet = ResearchAttentionService(tmp_path).build(max_chars=16_000)
    sections = _section_map(packet)

    assert sections["Current uncertainty"]["excerpt"] == (
        "Late exact question uncertainty."
    )
    assert any(
        item["path"] == "memory/NOW.md"
        and item["excerpt"] == "Late exact NOW uncertainty."
        for item in packet["current_uncertainty"]
    )
    assert packet["active_question"]["content_sha256"] == hashlib.sha256(
        question.read_bytes()
    ).hexdigest()
    assert not any("semantic_view_too_large" in warning for warning in packet["warnings"])


def test_attention_exposes_current_and_rival_hypothesis_refs(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    rivals = tmp_path / "model" / "rivals"
    rivals.mkdir()
    (rivals / "R-0002.md").write_text("# Rival two\n", encoding="utf-8")
    (rivals / "R-0001.md").write_text("# Rival one\n", encoding="utf-8")
    _commit_workspace(tmp_path, "add rival models")

    packet = ResearchAttentionService(tmp_path).build()

    assert _paths(packet["hypotheses"]["leading"]) == {"model/CURRENT.md"}
    assert [item["path"] for item in packet["hypotheses"]["competing"]] == [
        "model/rivals/R-0001.md",
        "model/rivals/R-0002.md",
    ]
    assert all("confidence" not in item and "rank" not in item for item in packet["hypotheses"]["competing"])


def test_attention_separates_scientific_and_institutional_obligations(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path)
    institutional = (
        {"obligation_id": "ACK-7", "kind": "publication_review"},
        {"obligation_id": "LEASE-2", "kind": "renewal"},
    )
    authority = {"state": "available", "enforcement_class": "mediated"}
    context = AttentionAuthorityContext(
        authority=authority,
        remaining_budget={"state": "available", "tokens": 900},
        institutional_obligations=institutional,
    )
    authority["state"] = "mutated_after_context_creation"

    packet = ResearchAttentionService(tmp_path).build(context=context)

    obligations = packet["current_obligations"]
    assert obligations["scientific"][0]["heading"] == "Current obligations"
    assert obligations["scientific"][0]["excerpt"] == (
        "Run the preregistered control before changing the claim."
    )
    assert obligations["institutional"] == list(institutional)
    assert all("assimilated" not in item for item in obligations["institutional"])
    assert packet["authority"]["state"] == "available"
    with pytest.raises(TypeError):
        context.authority["state"] = "mutated"  # type: ignore[index]


def test_attention_reports_budget_available_unavailable_and_exhausted(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path, with_question=False)
    service = ResearchAttentionService(tmp_path)
    assert service.build()["remaining_budget"]["state"] == "not_configured"

    available = {"state": "available", "remaining_tokens": 77, "enforcement_class": "hard"}
    available_packet = service.build(
        context=AttentionAuthorityContext(
            authority={"state": "available"},
            remaining_budget=available,
            institutional_obligations=(),
        )
    )
    exhausted = {"state": "exhausted", "remaining_tokens": 0, "enforcement_class": "hard"}
    exhausted_packet = service.build(
        context=AttentionAuthorityContext(
            authority={"state": "available"},
            remaining_budget=exhausted,
            institutional_obligations=(),
        )
    )

    assert available_packet["remaining_budget"] == available
    assert exhausted_packet["remaining_budget"] == exhausted
    assert any(item["layer"] == "budget" for item in exhausted_packet["blocked_reasons"])
    assert not any(item["layer"] == "budget" for item in available_packet["blocked_reasons"])


def test_attention_preserves_blocker_owning_layers(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    (tmp_path / "memory" / "NOW.md").write_text(
        "# Current State\n\n## Current blockers\n\nNeed the missing calibration.\n",
        encoding="utf-8",
    )
    _commit_workspace(tmp_path, "record semantic blocker")
    runs = RunService(tmp_path)
    manifest = runs.prepare(
        [sys.executable, "-c", "pass"],
        idempotency_key="layered-blocker-run",
        actor="principal",
        security_profile="trusted-local",
    )
    _mark_run_lost(tmp_path, runs, str(manifest["run_id"]))
    context = AttentionAuthorityContext(
        authority={"state": "blocked", "reason": "lease_expired"},
        remaining_budget={"state": "exhausted", "remaining_tokens": 0},
        institutional_obligations=(),
    )

    packet = ResearchAttentionService(tmp_path).build(context=context)

    assert {item["layer"] for item in packet["blocked_reasons"]} == {
        "semantic",
        "operational",
        "authority",
        "budget",
    }


def test_attention_focus_question_may_be_null(tmp_path: Path) -> None:
    _init_workspace(tmp_path, with_question=False)

    packet = ResearchAttentionService(tmp_path).build()

    assert packet["active_question"] is None
    assert all("invented" not in warning for warning in packet["warnings"])


def test_attention_combines_canonical_head_with_candidate_pending_state(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    candidate = tmp_path / "candidate"
    canonical.mkdir()
    canonical_head = _init_workspace(canonical)
    subprocess.run(["git", "clone", "-q", str(canonical), str(candidate)], check=True)
    _git(candidate, "config", "user.email", "candidate@example.invalid")
    _git(candidate, "config", "user.name", "Candidate Test")
    runs = RunService(candidate)
    manifest = runs.prepare(
        [sys.executable, "-c", "pass"],
        idempotency_key="candidate-pending-run",
        actor="principal",
        security_profile="trusted-local",
    )
    _write_question_views(candidate, marker="dirty candidate")

    packet = ResearchAttentionService(
        candidate,
        canonical_repository=bind_repository(canonical),
    ).build()

    assert packet["snapshot"]["canonical"] == canonical_head
    assert packet["snapshot"]["canonical_repository"] == str(canonical.resolve())
    assert packet["snapshot"]["candidate"]["repository"] == str(candidate.resolve())
    assert "questions/Q-0001/question.md" in _paths(
        packet["snapshot"]["candidate"]["git_status"]["dirty_paths"]
    )
    assert "dirty candidate" not in _compact_json(packet)
    assert "Exact canonical answer." in _compact_json(packet)
    assert [item["ref"] for item in packet["pending_measurements"]] == [
        f"runs/{manifest['run_id']}/manifest.json"
    ]


def test_attention_run_inventory_failure_is_unavailable_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path, with_question=False)
    runs = RunService(tmp_path)
    manifest = runs.prepare(
        [sys.executable, "-c", "pass"],
        idempotency_key="attention-run-alias",
        actor="principal",
        security_profile="trusted-local",
    )
    run_id = str(manifest["run_id"])
    status = tmp_path / ".aros" / "runs" / run_id / "status.json"
    digest = hashlib.sha256(os.fsencode(status.name)).hexdigest()
    alias = status.parent / f".aros-json-{digest}.attention-crash.tmp"
    os.link(status, alias, follow_symlinks=False)
    before = {
        path: (path.lstat().st_ino, path.lstat().st_nlink, path.read_bytes())
        for path in (status, alias)
    }
    synced: list[Path] = []
    monkeypatch.setattr(store_module, "_fsync_directory", synced.append)

    packet = ResearchAttentionService(tmp_path).build()

    run_availability = packet["snapshot"]["candidate"]["availability"]["runs"]
    assert run_availability["state"] == "unavailable"
    assert run_availability["error"]
    assert "operational_read_failed:runs" in packet["warnings"]
    assert any(
        blocker["layer"] == "operational"
        and blocker["ref"] == "run_inventory"
        for blocker in packet["blocked_reasons"]
    )
    assert packet["pending_measurements"] == []
    assert synced == []
    assert {
        path: (path.lstat().st_ino, path.lstat().st_nlink, path.read_bytes())
        for path in (status, alias)
    } == before


def test_attention_terminal_inventory_failure_disables_pending_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, manifest, _final = observation_support._install_run_final(tmp_path)
    _finish_observation_workspace(tmp_path)

    def unavailable(_catalog: object) -> object:
        raise attention_module.ObservationError("terminal catalog unavailable")

    monkeypatch.setattr(
        attention_module.ObservationCatalog,
        "enumerate_terminal",
        unavailable,
    )

    packet = ResearchAttentionService(tmp_path).build()

    terminal = packet["snapshot"]["candidate"]["availability"][
        "terminal_observations"
    ]
    assert terminal["state"] == "unavailable"
    assert packet["pending_measurements"] == []
    assert packet["unassimilated_returns"] == []
    assert not any(
        item.get("measurement_state") == "terminal_observation_missing"
        for item in packet["pending_measurements"]
    )
    assert str(manifest["run_id"]) not in _compact_json(packet)


def test_attention_git_status_failure_is_not_reported_as_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    real_git_result = attention_module._worktrees._git_result

    def failing_status(repository, *args, **kwargs):
        if "status" in args:
            return subprocess.CompletedProcess(args, 1, b"", b"status failed")
        return real_git_result(repository, *args, **kwargs)

    monkeypatch.setattr(attention_module._worktrees, "_git_result", failing_status)

    packet = ResearchAttentionService(tmp_path).build()

    status = packet["snapshot"]["candidate"]["git_status"]
    assert status["state"] == "unavailable"
    assert "dirty" not in status
    assert any(
        blocker["layer"] == "operational"
        and blocker["ref"] == "candidate_git_status"
        for blocker in packet["blocked_reasons"]
    )


def test_attention_uses_semantic_owner_for_canonical_git_blobs(tmp_path: Path) -> None:
    _init_workspace(tmp_path)
    question = tmp_path / "questions" / "Q-0001" / "question.md"
    question.write_text(
        question.read_text(encoding="utf-8")
        + "\n## Evidence links\n\nnot-json\n",
        encoding="utf-8",
    )
    _commit_workspace(tmp_path, "add invalid owned semantic document")

    packet = ResearchAttentionService(tmp_path).build()

    assert packet["active_question"] is not None
    assert "malformed_semantic_view:questions/Q-0001/question.md" in packet[
        "warnings"
    ]
    assert not packet["active_question"].get("sections")


def test_attention_context_is_deeply_immutable_and_bounded(
    tmp_path: Path,
) -> None:
    _init_workspace(tmp_path, with_question=False)
    source = {
        "state": "available",
        "nested": {"capabilities": ["inspect"]},
    }
    obligations = tuple(
        {"obligation_id": f"OB-{index:04d}", "kind": "review"}
        for index in range(1_000)
    )
    context = AttentionAuthorityContext(
        authority=source,
        remaining_budget={"state": "available", "note": "界" * 50_000},
        institutional_obligations=obligations,
    )
    source["nested"]["capabilities"].append("mutated")  # type: ignore[index]

    roomy = ResearchAttentionService(tmp_path).build(
        max_chars=16_000,
        context=context,
    )
    minimum = ResearchAttentionService(tmp_path).build(
        max_chars=512,
        context=context,
    )

    assert roomy["authority"]["nested"]["capabilities"] == ["inspect"]
    assert "note" not in roomy["remaining_budget"]
    assert roomy["omitted"]["institutional obligations"] == 968
    assert any(
        pointer.startswith("remaining_budget.note#sha256=") and count == 1
        for pointer, count in roomy["omitted"].items()
    )
    with pytest.raises((AttributeError, TypeError)):
        context.authority["nested"]["capabilities"].append("blocked")  # type: ignore[index,union-attr]
    assert len(roomy["current_obligations"]["institutional"]) <= 32
    assert minimum["authority"] == {"state": "available"}
    assert minimum["remaining_budget"] == {"state": "available"}
    assert all(isinstance(count, int) and count > 0 for count in minimum["omitted"].values())
    assert "aros boot" in minimum["omitted"]
    assert len(_compact_json(minimum)) <= 512
    with pytest.raises(TypeError, match="finite JSON-compatible"):
        AttentionAuthorityContext(
            authority={"state": "available", "nested": [float("nan")]},
            remaining_budget={"state": "available"},
            institutional_obligations=(),
        )


def test_attention_snapshot_and_omitted_schema_are_stable_across_budgets(
    tmp_path: Path,
) -> None:
    head = _init_workspace(tmp_path)
    service = ResearchAttentionService(tmp_path)

    roomy = service.build(max_chars=8_000)
    minimum = service.build(max_chars=512)

    for packet in (roomy, minimum):
        assert packet["snapshot"]["canonical"] == head
        assert isinstance(packet["snapshot"]["candidate"], dict)
        assert isinstance(packet["authority"], dict)
        assert isinstance(packet["remaining_budget"], dict)
        assert isinstance(packet["hypotheses"], dict)
        assert isinstance(packet["current_obligations"], dict)
        assert all(
            isinstance(pointer, str) and isinstance(count, int) and count > 0
            for pointer, count in packet["omitted"].items()
        )


def test_attention_fails_when_repository_snapshot_drifts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path)
    real_facts = attention_module._repository_facts
    calls = 0

    def drifting_facts(repository):
        nonlocal calls
        calls += 1
        facts = real_facts(repository)
        if calls >= 3:
            facts["head"] = "0" * 40
        return facts

    monkeypatch.setattr(attention_module, "_repository_facts", drifting_facts)

    with pytest.raises(ValueError, match="snapshot|changed|drift"):
        ResearchAttentionService(tmp_path).build()
