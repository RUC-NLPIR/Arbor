"""Git-derived, fail-closed transition assimilation index behavior."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from arbor.aros.checkpoint import CheckpointService
from arbor.aros.store import canonical_json_bytes
from arbor.aros.worktrees import bind_repository
from tests import test_aros_checkpoint as checkpoint_support
from tests import test_aros_observations as observation_support


CACHE_RELATIVE = Path(".aros/indexes/transition-index.json")
CACHE_FIELDS = {
    "schema_version",
    "head",
    "validated_through",
    "assimilations",
    "latest_evidence_transition",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    ).stdout


def _api() -> tuple[object, object]:
    from arbor.aros.transition_index import TransitionIndex, TransitionIndexError

    return TransitionIndex, TransitionIndexError


@dataclass
class _AdmittedFixture:
    candidate: Path
    canonical: Path
    canonical_ref: str
    observation_ref: str
    observation_target: str
    assimilation_commit: str


def _finalize(
    candidate: Path,
    canonical: Path,
    proposal_ref: str,
    *,
    message: str,
) -> str:
    canonical_ref = _git(candidate, "symbolic-ref", "HEAD")
    service = CheckpointService(
        candidate,
        canonical_repository=bind_repository(canonical),
        canonical_ref=canonical_ref,
        clock=lambda: 1_500,
    )
    prepared = service.prepare(proposal_ref, message)
    receipt_raw = checkpoint_support._allow_receipt_bytes(
        candidate_subject_sha256=prepared.candidate_subject_sha256,
        audit_payload_sha256=prepared.audit_payload_sha256,
        canonical_ref=canonical_ref,
    )
    fence = checkpoint_support._fence_bytes(json.loads(receipt_raw))
    result = service.finalize(prepared.prepared_ref, receipt_raw, fence)
    return str(result["commit"])


def _admitted_assimilation(root: Path) -> _AdmittedFixture:
    candidate = root / "candidate"
    canonical = root / "canonical"
    _run_service, manifest, _final = observation_support._install_run_final(candidate)
    _git(root, "clone", "-q", str(candidate), str(canonical))
    _git(canonical, "config", "user.email", "index@example.invalid")
    _git(canonical, "config", "user.name", "Transition Index Test")
    base = _git(candidate, "rev-parse", "HEAD")
    observation_ref = f"runs/{manifest['run_id']}/final.json"
    now = candidate / "memory" / "NOW.md"
    now.parent.mkdir()
    link = {
        "observation_ref": observation_ref,
        "relation": "context",
        "scope": "The completed process is bounded operational context.",
    }
    now.write_text(
        "# Current State\n\n## Findings\n\nAssimilated returned process context.\n\n"
        "## Evidence links\n\n"
        + json.dumps(link, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    proposal_ref = checkpoint_support._write_proposal(
        candidate,
        "T-assimilate-run",
        base,
        ["memory/NOW.md"],
        [
            {
                "observation_ref": observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Evidence links",
            }
        ],
    )
    commit = _finalize(
        candidate,
        canonical,
        proposal_ref,
        message="Assimilate returned run.\n",
    )
    return _AdmittedFixture(
        candidate=candidate,
        canonical=canonical,
        canonical_ref=_git(candidate, "symbolic-ref", "HEAD"),
        observation_ref=observation_ref,
        observation_target=str(manifest["base_commit"]),
        assimilation_commit=commit,
    )


def _admit_followup(
    fixture: _AdmittedFixture,
    transition_id: str,
    *,
    assimilate: bool,
) -> str:
    base = _git(fixture.candidate, "rev-parse", "HEAD")
    now = fixture.candidate / "memory" / "NOW.md"
    now.write_text(
        now.read_text(encoding="utf-8")
        + f"\n## Follow-up {transition_id}\n\nFollow-up transition {transition_id}.\n",
        encoding="utf-8",
    )
    assimilations = (
        [
            {
                "observation_ref": fixture.observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ]
        if assimilate
        else []
    )
    proposal_ref = checkpoint_support._write_proposal(
        fixture.candidate,
        transition_id,
        base,
        ["memory/NOW.md"],
        assimilations,
    )
    return _finalize(
        fixture.candidate,
        fixture.canonical,
        proposal_ref,
        message=f"Follow-up {transition_id}.\n",
    )


def _index(fixture: _AdmittedFixture) -> object:
    TransitionIndex, _error = _api()
    return TransitionIndex(
        bind_repository(fixture.candidate),
        bind_repository(fixture.canonical),
    )


def _cache_path(fixture: _AdmittedFixture) -> Path:
    return fixture.candidate / CACHE_RELATIVE


def _read_cache(fixture: _AdmittedFixture) -> dict[str, object]:
    value = json.loads(_cache_path(fixture).read_bytes())
    assert isinstance(value, dict)
    return value


def _write_cache(fixture: _AdmittedFixture, value: object) -> None:
    _cache_path(fixture).write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def test_only_admitted_ancestral_assimilation_clears_observation(
    tmp_path: Path,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    index = _index(fixture)

    missing = index.read()
    rebuilt = index.rebuild()
    reread = index.read()

    assert missing.state == "index_incomplete"
    assert missing.assimilations == {}
    assert rebuilt.state == "complete"
    assert reread == rebuilt
    records = rebuilt.assimilations[fixture.observation_ref]
    assert len(records) == 1
    assert records[0].transition_id == "T-assimilate-run"
    assert records[0].commit == fixture.assimilation_commit
    assert records[0].affected_paths == ("memory/NOW.md",)
    assert records[0].rationale == "memory/NOW.md#Evidence links"
    assert len(records[0].record_sha256) == 64


def test_naked_link_manual_proposal_runtime_event_deny_and_unused_allow_do_not_clear(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _service, manifest, _final = observation_support._install_run_final(repository)
    observation_ref = f"runs/{manifest['run_id']}/final.json"
    now = repository / "memory" / "NOW.md"
    now.parent.mkdir()
    link = {
        "observation_ref": observation_ref,
        "relation": "context",
        "scope": "A naked citation is not an assimilation.",
    }
    now.write_text(
        "# Current State\n\n## Findings\n\n"
        + json.dumps(link, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    checkpoint_support._write_proposal(
        repository,
        "T-manual",
        _git(repository, "rev-parse", "HEAD"),
        ["memory/NOW.md"],
        [
            {
                "observation_ref": observation_ref,
                "affected_paths": ["memory/NOW.md"],
                "rationale": "memory/NOW.md#Findings",
            }
        ],
    )
    (repository / "transitions/T-manual/audit.json").write_text(
        "{}\n", encoding="utf-8"
    )
    runtime = repository / ".aros/events/EVT-T-manual-admitted.json"
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_text("{}\n", encoding="utf-8")
    unused = repository / ".aros/admissions/T-manual-allow.json"
    unused.parent.mkdir(parents=True, exist_ok=True)
    unused.write_bytes(checkpoint_support._allow_receipt_bytes())
    denial = repository / "transitions/T-manual/denial.json"
    denial.write_text('{"decision":"deny"}\n', encoding="utf-8")
    _git(repository, "add", "memory", "transitions")
    _git(repository, "commit", "-qm", "manual non-admission artifacts")
    TransitionIndex, _error = _api()
    index = TransitionIndex(bind_repository(repository), bind_repository(repository))

    rebuilt = index.rebuild()

    assert rebuilt.state == "complete"
    assert rebuilt.assimilations == {}
    assert rebuilt.latest_evidence_transition is None


@pytest.mark.parametrize(
    "mutation",
    ("deleted", "stale", "malformed", "forged", "incomplete"),
)
def test_untrustworthy_cache_redisplays_pending(
    tmp_path: Path,
    mutation: str,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    index = _index(fixture)
    complete = index.rebuild()
    assert complete.state == "complete"
    cache = _read_cache(fixture)
    path = _cache_path(fixture)
    if mutation == "deleted":
        path.unlink()
    elif mutation == "stale":
        cache["head"] = "0" * 40
        _write_cache(fixture, cache)
    elif mutation == "malformed":
        path.write_bytes(b'{"schema_version":1')
    elif mutation == "forged":
        records = cache["assimilations"]
        assert isinstance(records, dict)
        item = records[fixture.observation_ref]
        assert isinstance(item, list)
        item[0]["record_sha256"] = "f" * 64
        _write_cache(fixture, cache)
    else:
        cache["assimilations"] = {}
        _write_cache(fixture, cache)

    state = index.read()

    assert state.state == "index_incomplete"
    assert state.assimilations == {}
    assert state.latest_evidence_transition is None


def test_rebuild_rejects_cache_parent_symlink_swap_without_external_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    indexes = _cache_path(fixture).parent
    indexes.mkdir(parents=True)
    displaced = fixture.candidate / ".aros/indexes-displaced"
    external = tmp_path / "external"
    external.mkdir()
    module = __import__("arbor.aros.transition_index", fromlist=["unused"])
    real_preflight = module._require_safe_cache_parent

    def swap_after_preflight(root: Path) -> None:
        real_preflight(root)
        indexes.rename(displaced)
        indexes.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(
        module,
        "_require_safe_cache_parent",
        swap_after_preflight,
    )

    state = _index(fixture).rebuild()

    assert state.state == "index_incomplete"
    assert not (external / "transition-index.json").exists()


def test_rebuild_validates_cache_node_bound_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    module = __import__("arbor.aros.transition_index", fromlist=["unused"])
    monkeypatch.setattr(module, "MAX_CACHE_NODES", 1)

    state = _index(fixture).rebuild()

    assert state.state == "index_incomplete"
    assert not _cache_path(fixture).exists()


def test_normal_read_is_bounded_and_never_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    index = _index(fixture)
    assert index.rebuild().state == "complete"
    before = _cache_path(fixture).read_bytes()
    module = __import__("arbor.aros.transition_index", fromlist=["unused"])
    calls = 0
    real_validate = module._validate_admitted_transition

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return real_validate(*args, **kwargs)

    def forbidden_write(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("normal transition-index read attempted a write")

    monkeypatch.setattr(module, "_validate_admitted_transition", counted)
    monkeypatch.setattr(module, "_publish_cache", forbidden_write)

    state = index.read()

    assert state.state == "complete"
    assert calls <= 256
    assert _cache_path(fixture).read_bytes() == before


def test_rebuild_uses_full_limits_beyond_the_normal_transition_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    later = _admit_followup(fixture, "T-rebuild-second", assimilate=True)
    module = __import__("arbor.aros.transition_index", fromlist=["unused"])
    monkeypatch.setattr(module, "MAX_NORMAL_TRANSITIONS", 1)
    index = _index(fixture)

    rebuilt = index.rebuild()

    assert rebuilt.state == "complete"
    records = rebuilt.assimilations[fixture.observation_ref]
    assert [record.transition_id for record in records] == [
        "T-assimilate-run",
        "T-rebuild-second",
    ]
    assert [record.commit for record in records] == [
        fixture.assimilation_commit,
        later,
    ]
    latest = rebuilt.latest_evidence_transition
    assert latest is not None
    assert latest.transition_id == "T-assimilate-run"
    assert latest.commit == fixture.assimilation_commit
    cache = _read_cache(fixture)
    assert [
        item["transition_id"]
        for item in cache["assimilations"][fixture.observation_ref]
    ] == ["T-assimilate-run", "T-rebuild-second"]
    assert cache["latest_evidence_transition"]["transition_id"] == (
        "T-assimilate-run"
    )

    normal = index.read()

    assert normal.state == "index_incomplete"
    assert normal.assimilations == {}
    assert normal.latest_evidence_transition is None


def test_more_than_256_admission_paths_fails_closed_without_writing(
    tmp_path: Path,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    index = _index(fixture)
    assert index.rebuild().state == "complete"
    for number in range(256):
        path = fixture.canonical / f"transitions/T-overflow-{number:03d}/admission.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
    _git(fixture.canonical, "add", "transitions")
    _git(fixture.canonical, "commit", "-qm", "overflow transition admissions")
    head = _git(fixture.canonical, "rev-parse", "HEAD")
    cache = _read_cache(fixture)
    cache["head"] = head
    cache["validated_through"] = head
    _write_cache(fixture, cache)
    before = _cache_path(fixture).read_bytes()

    state = index.read()

    assert state.state == "index_incomplete"
    assert state.assimilations == {}
    assert _cache_path(fixture).read_bytes() == before


def test_rebuild_preserves_attribution_and_atomically_writes_exact_cache(
    tmp_path: Path,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    later = _admit_followup(fixture, "T-assimilate-again", assimilate=True)
    index = _index(fixture)

    state = index.rebuild()
    cache = _read_cache(fixture)

    assert state.state == "complete"
    assert set(cache) == CACHE_FIELDS
    assert cache["head"] == later
    assert cache["validated_through"] == later
    records = state.assimilations[fixture.observation_ref]
    assert [record.commit for record in records] == [
        fixture.assimilation_commit,
        later,
    ]
    assert [record.transition_id for record in records] == [
        "T-assimilate-run",
        "T-assimilate-again",
    ]
    assert not list(_cache_path(fixture).parent.glob(".*.tmp"))


def test_latest_evidence_transition_skips_later_empty_transition(
    tmp_path: Path,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    operational = _admit_followup(fixture, "T-operational-only", assimilate=False)

    state = _index(fixture).rebuild()

    assert state.state == "complete"
    assert operational != fixture.assimilation_commit
    latest = state.latest_evidence_transition
    assert latest is not None
    assert latest.transition_id == "T-assimilate-run"
    assert latest.commit == fixture.assimilation_commit
    assert [item.observation_ref for item in latest.assimilations] == [
        fixture.observation_ref
    ]


def test_latest_evidence_skips_later_assimilation_without_new_evidence_link(
    tmp_path: Path,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    assimilation_only = _admit_followup(
        fixture,
        "T-assimilation-without-link",
        assimilate=True,
    )

    state = _index(fixture).rebuild()

    assert state.state == "complete"
    assert [record.commit for record in state.assimilations[fixture.observation_ref]] == [
        fixture.assimilation_commit,
        assimilation_only,
    ]
    latest = state.latest_evidence_transition
    assert latest is not None
    assert latest.transition_id == "T-assimilate-run"
    assert latest.commit == fixture.assimilation_commit
    assert latest.evidence_links


def test_copied_receipt_in_unrelated_commit_is_not_admission(
    tmp_path: Path,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    copied = fixture.canonical / "transitions/T-copied/admission.json"
    copied.parent.mkdir(parents=True)
    copied.write_bytes(
        _git_bytes(
            fixture.canonical,
            "show",
            "HEAD:transitions/T-assimilate-run/admission.json",
        )
    )
    proposal = fixture.canonical / "transitions/T-copied/proposal.json"
    audit = fixture.canonical / "transitions/T-copied/audit.json"
    proposal.write_bytes(
        _git_bytes(
            fixture.canonical,
            "show",
            "HEAD:transitions/T-assimilate-run/proposal.json",
        )
    )
    audit.write_bytes(
        _git_bytes(
            fixture.canonical,
            "show",
            "HEAD:transitions/T-assimilate-run/audit.json",
        )
    )
    _git(fixture.canonical, "add", "transitions/T-copied")
    _git(fixture.canonical, "commit", "-qm", "copy receipt outside its admission")

    state = _index(fixture).rebuild()

    assert state.state == "index_incomplete"
    assert state.assimilations == {}


def test_rebuild_rejects_deleted_admitted_transition_record(tmp_path: Path) -> None:
    fixture = _admitted_assimilation(tmp_path)
    index = _index(fixture)
    assert index.rebuild().state == "complete"
    before = _cache_path(fixture).read_bytes()
    transition = fixture.canonical / "transitions/T-assimilate-run"
    transition.mkdir(parents=True, exist_ok=True)
    for name in ("proposal.json", "audit.json", "admission.json"):
        (transition / name).write_bytes(
            _git_bytes(
                fixture.canonical,
                "show",
                f"HEAD:transitions/T-assimilate-run/{name}",
            )
        )
    _git(fixture.canonical, "add", "transitions/T-assimilate-run")
    _git(
        fixture.canonical,
        "rm",
        "transitions/T-assimilate-run/admission.json",
    )
    _git(fixture.canonical, "commit", "-qm", "delete admitted transition record")

    state = index.rebuild()

    assert state.state == "index_incomplete"
    assert state.assimilations == {}
    assert _cache_path(fixture).read_bytes() == before


def test_normal_read_rejects_in_window_deleted_transition_with_forged_empty_cache(
    tmp_path: Path,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    index = _index(fixture)
    assert index.rebuild().state == "complete"
    transition = fixture.canonical / "transitions/T-assimilate-run"
    transition.mkdir(parents=True, exist_ok=True)
    for name in ("proposal.json", "audit.json", "admission.json"):
        (transition / name).write_bytes(
            _git_bytes(
                fixture.canonical,
                "show",
                f"HEAD:transitions/T-assimilate-run/{name}",
            )
        )
    _git(fixture.canonical, "add", "transitions/T-assimilate-run")
    _git(fixture.canonical, "rm", "transitions/T-assimilate-run/admission.json")
    _git(fixture.canonical, "commit", "-qm", "delete admitted transition in window")
    head = _git(fixture.canonical, "rev-parse", "HEAD")
    _write_cache(
        fixture,
        {
            "schema_version": 1,
            "head": head,
            "validated_through": head,
            "assimilations": {},
            "latest_evidence_transition": None,
        },
    )

    state = index.read()

    assert state.state == "index_incomplete"
    assert state.assimilations == {}


def test_normal_read_never_trusts_cache_beyond_first_parent_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    index = _index(fixture)
    assert index.rebuild().state == "complete"
    transition = fixture.canonical / "transitions/T-assimilate-run"
    transition.mkdir(parents=True, exist_ok=True)
    for name in ("proposal.json", "audit.json", "admission.json"):
        (transition / name).write_bytes(
            _git_bytes(
                fixture.canonical,
                "show",
                f"HEAD:transitions/T-assimilate-run/{name}",
            )
        )
    _git(fixture.canonical, "add", "transitions/T-assimilate-run")
    _git(fixture.canonical, "rm", "transitions/T-assimilate-run/admission.json")
    _git(fixture.canonical, "commit", "-qm", "hide old admission from HEAD")
    _git(fixture.canonical, "commit", "--allow-empty", "-qm", "later operation")
    head = _git(fixture.canonical, "rev-parse", "HEAD")
    _write_cache(
        fixture,
        {
            "schema_version": 1,
            "head": head,
            "validated_through": head,
            "assimilations": {},
            "latest_evidence_transition": None,
        },
    )
    module = __import__("arbor.aros.transition_index", fromlist=["unused"])
    monkeypatch.setattr(module, "MAX_NORMAL_HISTORY_COMMITS", 2)

    state = index.read()

    assert state.state == "index_incomplete"
    assert state.assimilations == {}


@pytest.mark.parametrize("tamper", ("receipt", "audit", "ancestry", "immutable-ref"))
def test_commit_validation_fails_closed_on_bound_fact_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    fixture = _admitted_assimilation(tmp_path)
    index = _index(fixture)
    assert index.rebuild().state == "complete"
    if tamper == "immutable-ref":
        immutable_ref = _git(
            fixture.canonical,
            "for-each-ref",
            "--format=%(refname)",
            "refs/aros/observations/run_final/",
        )
        assert immutable_ref
        _git(
            fixture.canonical,
            "update-ref",
            immutable_ref,
            _git(fixture.canonical, "rev-parse", "HEAD^^"),
        )
    else:
        transition = fixture.canonical / "transitions/T-assimilate-run"
        transition.mkdir(parents=True, exist_ok=True)
        if tamper in {"receipt", "audit"}:
            for name in ("proposal.json", "audit.json", "admission.json"):
                (transition / name).write_bytes(
                    _git_bytes(
                        fixture.canonical,
                        "show",
                        f"HEAD:transitions/T-assimilate-run/{name}",
                    )
                )
        if tamper == "receipt":
            receipt = json.loads(
                _git_bytes(
                    fixture.canonical,
                    "show",
                    "HEAD:transitions/T-assimilate-run/admission.json",
                )
            )
            receipt["receiptSHA256"] = "0" * 64
            (transition / "admission.json").write_bytes(canonical_json_bytes(receipt))
        elif tamper == "audit":
            audit = json.loads(
                _git_bytes(
                    fixture.canonical,
                    "show",
                    "HEAD:transitions/T-assimilate-run/audit.json",
                )
            )
            audit["candidate_subject_sha256"] = "0" * 64
            (transition / "audit.json").write_bytes(canonical_json_bytes(audit))
        else:
            unrelated = fixture.canonical / "unrelated-root"
            unrelated.mkdir()
            _git(unrelated, "init", "-q")
            _git(unrelated, "config", "user.email", "index@example.invalid")
            _git(unrelated, "config", "user.name", "Transition Index Test")
            (unrelated / "root.txt").write_text("unrelated\n", encoding="utf-8")
            _git(unrelated, "add", "root.txt")
            _git(unrelated, "commit", "-qm", "unrelated root")
            unrelated_commit = _git(unrelated, "rev-parse", "HEAD")
            _git(
                fixture.canonical,
                "fetch",
                "-q",
                str(unrelated),
                unrelated_commit,
            )
            tree = _git(fixture.canonical, "rev-parse", "HEAD^{tree}")
            commit = subprocess.run(
                [
                    "git",
                    "-C",
                    str(fixture.canonical),
                    "commit-tree",
                    tree,
                    "-p",
                    unrelated_commit,
                    "-m",
                    "wrong first-parent base",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            _git(fixture.canonical, "update-ref", fixture.canonical_ref, commit)
        if tamper in {"receipt", "audit"}:
            _git(fixture.canonical, "add", "transitions/T-assimilate-run")
            _git(fixture.canonical, "commit", "-qm", f"tamper {tamper}")

    state = index.rebuild()

    assert state.state == "index_incomplete"
    assert state.assimilations == {}


def test_cache_has_exact_minimal_nested_record_schema(tmp_path: Path) -> None:
    fixture = _admitted_assimilation(tmp_path)

    assert _index(fixture).rebuild().state == "complete"
    cache = _read_cache(fixture)
    records = cache["assimilations"]
    assert isinstance(records, dict)
    assert set(records) == {fixture.observation_ref}
    item = records[fixture.observation_ref][0]
    assert set(item) == {
        "observation_ref",
        "transition_id",
        "commit",
        "affected_paths",
        "rationale",
        "record_sha256",
    }
    latest = cache["latest_evidence_transition"]
    assert isinstance(latest, dict)
    assert set(latest) == {
        "transition_id",
        "commit",
        "assimilations",
        "evidence_links",
    }
    assert hashlib.sha256(_cache_path(fixture).read_bytes()).hexdigest()
