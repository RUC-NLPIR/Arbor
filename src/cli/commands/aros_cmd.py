"""Native Principal commands exposed by the direct ``aros`` entry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import typer
from typer.core import TyperCommand

from ...aros.attention import AttentionAuthorityContext
from ...aros.checkpoint import (
    CheckpointService,
    _decode_human_direct_admission_receipt,
)
from ...aros.eval import EvalService, ExistingEvaluation
from ...aros.intake import initialize_knowledge_bank
from ...aros.principal import (
    AROS_DEFAULT_MODEL,
    AROS_DEFAULT_PROVIDER,
    AROS_DEFAULT_REASONING_EFFORT,
    build_principal_agent,
    run_principal,
)
from ...aros.runs import RunService
from ...aros.store import canonical_json_bytes
from ...aros.tasks import TaskService
from ...aros.transition_index import TransitionIndex, transition_index_state_json
from ...aros.transitions import TransitionAuditService
from ...aros.worktrees import (
    RepositoryBinding,
    bind_repository,
    read_repository_snapshot,
)
from ...aros.workspace import (
    DEFAULT_BOOT_MAX_CHARS,
    boot_packet,
    boot_workspace,
    render_boot_packet,
    status_workspace,
)
from ...core import AgentConfig, create_provider
from ..aros_start import (
    collect_start_intake,
    render_start_transition,
)


_TASK_TRUST_BOUNDARY = (
    "Task adapters are trusted-local and application-scoped, not a security sandbox. "
    "Network and shell capability flags are audit declarations and are not enforced. "
    "Secrets and untrusted adapters are unsupported. Daemonizing or new-session "
    "descendants that do not drain fail closed as lost with no terminal receipt."
)
_EVAL_MEASUREMENT_BOUNDARY = (
    "Visible evaluation apparatus produces factual measurements; the Principal "
    "interprets them. Lost evaluations are never retried."
)


aros_app = typer.Typer(
    name="aros",
    help="Operate the native Agent-principal research workspace.",
    no_args_is_help=True,
)
run_app = typer.Typer(
    name="run",
    help="Manage durable background experiments.",
    no_args_is_help=True,
)
task_app = typer.Typer(
    name="task",
    help="Manage durable child tasks. " + _TASK_TRUST_BOUNDARY,
    no_args_is_help=True,
)
eval_app = typer.Typer(
    name="eval",
    help=_EVAL_MEASUREMENT_BOUNDARY,
    no_args_is_help=True,
)
transition_app = typer.Typer(
    name="transition",
    help="Audit explicit research transition proposals.",
    no_args_is_help=True,
)
aros_app.add_typer(run_app, name="run")
aros_app.add_typer(task_app, name="task")
aros_app.add_typer(eval_app, name="eval")
aros_app.add_typer(transition_app, name="transition")


def _root(cwd: Path) -> Path:
    return cwd.expanduser().resolve()


def _attached_repository(root: Path) -> tuple[RepositoryBinding, str]:
    repository = bind_repository(root)
    snapshot = read_repository_snapshot(repository)
    canonical_ref = snapshot.get("ref")
    if not isinstance(snapshot.get("head"), str) or not isinstance(
        canonical_ref,
        str,
    ):
        raise ValueError("operation requires an attached canonical Git branch")
    return repository, canonical_ref


def _print_json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _fail(exc: Exception) -> None:
    typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=2) from exc


class HumanDirectGateway:
    """Issue only explicit cooperative human-direct checkpoint receipts."""

    def __init__(self, *, clock: Callable[[], int] | None = None):
        selected_clock = clock or (lambda: time.time_ns() // 1_000_000)
        if not callable(selected_clock):
            raise TypeError("human-direct clock must be callable")
        self._clock = selected_clock

    def admit_transition(
        self,
        *,
        candidate_subject_sha256: str,
        audit_payload_sha256: str,
        audit_testimony: Mapping[str, object],
    ) -> bytes:
        del audit_testimony
        payload: dict[str, object] = {
            "schema_version": 1,
            "receipt_kind": "human_direct",
            "decision": "allow",
            "candidate_subject_sha256": candidate_subject_sha256,
            "audit_payload_sha256": audit_payload_sha256,
            "enforcement_class": "cooperative",
            "issuer": "human-direct",
            "issued_at": self._clock(),
        }
        receipt = canonical_json_bytes(
            {
                **payload,
                "receipt_sha256": hashlib.sha256(
                    canonical_json_bytes(payload)
                ).hexdigest(),
            }
        )
        _decode_human_direct_admission_receipt(receipt)
        return receipt

    def revalidate_transition(self, receipt: bytes) -> bytes:
        _decode_human_direct_admission_receipt(receipt)
        return receipt


class _RequireCommandSeparator(TyperCommand):
    """Keep command argv separate from AROS's own CLI options."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if "--help" not in args and "-h" not in args and "--" not in args:
            ctx.fail("command argv must follow --")
        return super().parse_args(ctx, args)


@aros_app.command("boot")
def boot_command(
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    max_chars: int = typer.Option(
        DEFAULT_BOOT_MAX_CHARS,
        "--max-chars",
        min=1,
        help="Maximum characters in the derived boot context.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the exact packet as JSON."),
) -> None:
    """Render the compact, provider-independent Principal boot context."""
    try:
        packet = boot_packet(_root(cwd), max_chars=max_chars)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    if as_json:
        typer.echo(render_boot_packet(packet))
        return
    typer.echo(render_boot_packet(packet))


@aros_app.command("status")
def status_command(
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Inspect workspace initialization and operational Git state."""
    try:
        status = status_workspace(_root(cwd))
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)

    if as_json:
        _print_json(status)
        return

    typer.echo(f"workspace: {status.get('root', _root(cwd))}")
    typer.echo(f"initialized: {'yes' if status.get('initialized') else 'no'}")
    git = status.get("git")
    if isinstance(git, dict):
        branch = git.get("branch") or "(detached/unavailable)"
        head = git.get("head") or "unknown"
        dirty = "dirty" if git.get("dirty") else "clean"
        typer.echo(f"git: {branch} @ {head} ({dirty})")
    missing = status.get("missing")
    if missing:
        typer.echo("missing: " + ", ".join(str(item) for item in missing))


@aros_app.command("checkpoint")
def checkpoint_command(
    proposal_ref: str = typer.Option(
        ...,
        "--proposal",
        help="Tracked transitions/T-*/proposal.json to checkpoint.",
    ),
    message: str = typer.Option(
        ...,
        "--message",
        help="Exact Git commit message.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    cooperative_human_direct: bool = typer.Option(
        False,
        "--cooperative-human-direct",
        help=(
            "Explicitly use cooperative human-direct admission for same-UID "
            "writable Git."
        ),
    ),
) -> None:
    """Create one explicitly cooperative human-direct checkpoint."""
    if not cooperative_human_direct:
        _fail(
            ValueError(
                "checkpoint requires explicit --cooperative-human-direct; "
                "same-UID writable Git is cooperative"
            )
        )
    try:
        root = _root(cwd)
        repository = bind_repository(root)
        canonical_ref = read_repository_snapshot(repository).get("ref")
        if not isinstance(canonical_ref, str):
            raise ValueError("checkpoint requires an attached canonical Git branch")
        result = CheckpointService(
            root,
            canonical_repository=repository,
            canonical_ref=canonical_ref,
            gateway=HumanDirectGateway(),
        ).checkpoint(proposal_ref, message)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(
        {
            **result,
            "checkpoint_authority": "human-direct",
            "enforcement_class": "cooperative",
        }
    )


@transition_app.command("audit")
def transition_audit_command(
    proposal_ref: str = typer.Argument(
        ...,
        metavar="PROPOSAL",
        help="Tracked transitions/T-*/proposal.json to audit.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Emit deterministic read-only testimony for one transition proposal."""
    try:
        root = _root(cwd)
        _repository, canonical_ref = _attached_repository(root)
        result = TransitionAuditService(
            root,
            canonical_ref=canonical_ref,
        ).audit(proposal_ref)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(result)


@aros_app.command("audit")
def audit_command(
    rebuild_index: bool = typer.Option(
        False,
        "--rebuild-index",
        help="Explicitly rebuild the full disposable transition index.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Run an explicitly selected repository audit operation."""
    if not rebuild_index:
        _fail(ValueError("audit requires explicit --rebuild-index"))
    try:
        repository, _canonical_ref = _attached_repository(_root(cwd))
        state = TransitionIndex(repository, repository).rebuild()
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(transition_index_state_json(state))


@run_app.command("start", cls=_RequireCommandSeparator)
def run_start_command(
    command: list[str] = typer.Argument(
        ...,
        metavar="-- COMMAND [ARGS]...",
        help="Exact command argv. Place it after -- so options are not parsed by AROS.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    run_cwd: str = typer.Option(
        ".",
        "--run-cwd",
        help="Command working directory, relative to the workspace root.",
    ),
    timeout_seconds: int = typer.Option(
        3600,
        "--timeout-seconds",
        min=1,
        help="Hard run timeout in seconds.",
    ),
    idempotency_key: str = typer.Option(
        ...,
        "--idempotency-key",
        help="Stable key preventing duplicate process launch.",
    ),
    security_profile: str = typer.Option(
        "isolated-linux",
        "--security-profile",
        help="Run isolation profile; trusted-local must be selected explicitly.",
    ),
    writable_paths: list[str] | None = typer.Option(
        None,
        "--writable-path",
        help="Workspace-relative writable path; repeat for multiple paths.",
    ),
    actor: str = typer.Option("human", "--actor", help="Launch authority."),
    label: str | None = typer.Option(None, "--label", help="Optional display label."),
) -> None:
    """Prepare and launch one durable run."""
    try:
        service = RunService(_root(cwd))
        manifest = service.prepare(
            list(command),
            cwd=run_cwd,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
            actor=actor,
            label=label,
            security_profile=security_profile,
            writable_paths=writable_paths or [],
        )
        run_id = manifest.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise RuntimeError("prepared run manifest has no run_id")
        status = service.start(run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(status)


@run_app.command("status")
def run_status_command(
    run_id: str = typer.Argument(..., help="Stable run identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Reconcile and inspect one durable run."""
    try:
        status = RunService(_root(cwd)).status(run_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(status)


@run_app.command("list")
def run_list_command(
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """List durable runs after reconciling their process state."""
    try:
        runs = RunService(_root(cwd)).list()
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(runs)


@run_app.command("tail")
def run_tail_command(
    run_id: str = typer.Argument(..., help="Stable run identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    stream: str = typer.Option(
        "stdout",
        "--stream",
        help="Log stream: stdout or stderr.",
    ),
    max_bytes: int = typer.Option(
        65_536,
        "--max-bytes",
        min=1,
        help="Maximum number of trailing bytes to print.",
    ),
) -> None:
    """Print the current tail of a run log without JSON escaping it."""
    try:
        output = RunService(_root(cwd)).tail(
            run_id,
            stream=stream,
            max_bytes=max_bytes,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    typer.echo(output, nl=False)


@run_app.command("stop")
def run_stop_command(
    run_id: str = typer.Argument(..., help="Stable run identifier."),
    reason: str = typer.Option(..., "--reason", help="Required audit reason."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    actor: str = typer.Option("human", "--actor", help="Stop authority."),
    signal_name: str = typer.Option(
        "TERM",
        "--signal",
        help="Signal name recorded and sent by the run service.",
    ),
) -> None:
    """Stop a durable run and record an attributed receipt."""
    try:
        receipt = RunService(_root(cwd)).stop(
            run_id,
            actor=actor,
            reason=reason,
            signal_name=signal_name,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(receipt)


@eval_app.command("register")
def eval_register_command(
    manifest: str = typer.Option(
        ...,
        "--manifest",
        help="Tracked visible evaluator manifest.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    actor: str = typer.Option("human", "--actor", help="Registration authority."),
) -> None:
    """Register one tracked visible evaluator apparatus."""
    try:
        descriptor = EvalService(_root(cwd)).register(manifest, actor=actor)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(descriptor)


@eval_app.command("run")
def eval_run_command(
    evaluator_id: str = typer.Argument(..., help="Registered evaluator ID."),
    version: str = typer.Argument(..., help="Registered evaluator version."),
    candidate_commit: str = typer.Argument(..., help="Exact candidate Git commit."),
    idempotency_key: str = typer.Option(
        ...,
        "--idempotency-key",
        help="One-attempt evaluation request key.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    actor: str = typer.Option("human", "--actor", help="Evaluation authority."),
) -> None:
    """Run one visible evaluation through the registered apparatus."""
    try:
        result = EvalService(_root(cwd)).run(
            evaluator_id,
            version,
            candidate_commit,
            actor=actor,
            idempotency_key=idempotency_key,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    if isinstance(result, ExistingEvaluation):
        result = result.status
    _print_json(result)


@eval_app.command("status")
def eval_status_command(
    eval_id: str = typer.Argument(..., help="Stable evaluation identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Inspect factual evaluation and referenced process state."""
    try:
        status = EvalService(_root(cwd)).status(eval_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(status)


@eval_app.command("observe")
def eval_observe_command(
    eval_id: str = typer.Argument(..., help="Stable evaluation identifier."),
    stream: str = typer.Option(
        "stdout",
        "--stream",
        help="Visible stream: stdout or stderr.",
    ),
    max_bytes: int = typer.Option(
        65_536,
        "--max-bytes",
        min=1,
        max=65_536,
        help="Maximum visible stream bytes to print.",
    ),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Print one bounded visible evaluation stream verbatim."""
    try:
        output = EvalService(_root(cwd)).observe(
            eval_id,
            stream=stream,
            max_bytes=max_bytes,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    typer.echo(output, nl=False)


@eval_app.command("audit")
def eval_audit_command(
    eval_id: str = typer.Argument(..., help="Stable evaluation identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Validate visible evaluation lineage without repair or interpretation."""
    try:
        audit = EvalService(_root(cwd)).audit(eval_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(audit)


@task_app.command(
    "create",
    cls=_RequireCommandSeparator,
    help="Freeze one immutable task brief without starting it. " + _TASK_TRUST_BOUNDARY,
    short_help="Freeze one immutable task brief without starting it.",
)
def task_create_command(
    adapter_argv: list[str] = typer.Argument(
        ...,
        metavar="-- ADAPTER [ARGS]...",
        help="Exact adapter argv. Place it after -- so AROS does not parse it.",
    ),
    objective: str = typer.Option(
        ...,
        "--objective",
        help="Bounded objective frozen into the task brief.",
    ),
    mode: str = typer.Option(
        ...,
        "--mode",
        help="Task access mode: read_only or write.",
    ),
    idempotency_key: str = typer.Option(
        ...,
        "--idempotency-key",
        help="Stable key preventing duplicate task creation.",
    ),
    timeout_seconds: int = typer.Option(
        3600,
        "--timeout-seconds",
        min=1,
        help="Hard task timeout in seconds.",
    ),
    network: bool = typer.Option(
        False,
        "--network",
        help="Network audit declaration; not enforced.",
    ),
    shell: bool = typer.Option(
        False,
        "--shell",
        help="Shell audit declaration; not enforced.",
    ),
    deliverables: list[str] | None = typer.Option(
        None,
        "--deliverable",
        help="Required deliverable; repeat for multiple values.",
    ),
    acceptance: list[str] | None = typer.Option(
        None,
        "--acceptance",
        help="Acceptance check; repeat for multiple values.",
    ),
    actor: str = typer.Option("human", "--actor", help="Creation authority."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Freeze one immutable task brief without starting it."""
    try:
        brief = TaskService(_root(cwd)).create(
            objective,
            actor=actor,
            mode=mode,
            adapter_argv=list(adapter_argv),
            capabilities={"network": network, "shell": shell},
            deliverables=deliverables or [],
            acceptance=acceptance or [],
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(brief)


@task_app.command("start")
def task_start_command(
    task_id: str = typer.Argument(..., help="Stable task identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    actor: str = typer.Option("human", "--actor", help="Launch authority."),
) -> None:
    """Launch one previously created task."""
    try:
        status = TaskService(_root(cwd)).start(task_id, actor=actor)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(status)


@task_app.command("status")
def task_status_command(
    task_id: str = typer.Argument(..., help="Stable task identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Inspect one durable task."""
    try:
        status = TaskService(_root(cwd)).status(task_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(status)


@task_app.command("list")
def task_list_command(
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """List durable tasks in stable task-ID order."""
    try:
        tasks = TaskService(_root(cwd)).list()
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(tasks)


@task_app.command("message")
def task_message_command(
    task_id: str = typer.Argument(..., help="Stable task identifier."),
    message: str = typer.Option(..., "--message", help="Mailbox message text."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    actor: str = typer.Option("human", "--actor", help="Message authority."),
) -> None:
    """Append one attributed task mailbox message."""
    try:
        record = TaskService(_root(cwd)).message(task_id, message, actor)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(record)


@task_app.command("stop")
def task_stop_command(
    task_id: str = typer.Argument(..., help="Stable task identifier."),
    reason: str = typer.Option(..., "--reason", help="Required audit reason."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
    actor: str = typer.Option("human", "--actor", help="Stop authority."),
) -> None:
    """Request task termination with the implicit TERM signal."""
    try:
        receipt = TaskService(_root(cwd)).stop(
            task_id,
            actor=actor,
            reason=reason,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(receipt)


@task_app.command("collect")
def task_collect_command(
    task_id: str = typer.Argument(..., help="Stable task identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Record reviewed child return pointers without assimilation."""
    try:
        result = TaskService(_root(cwd)).collect(task_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(result)


@task_app.command("preserve")
def task_preserve_command(
    task_id: str = typer.Argument(..., help="Stable task identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Inspect and preserve one owned child worktree."""
    try:
        result = TaskService(_root(cwd)).preserve(task_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(result)


@task_app.command("prune")
def task_prune_command(
    task_id: str = typer.Argument(..., help="Stable task identifier."),
    cwd: Path = typer.Option(Path("."), "--cwd", help="AROS workspace root."),
) -> None:
    """Remove one clean collected child worktree."""
    try:
        result = TaskService(_root(cwd)).prune(task_id)
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)
    _print_json(result)


@aros_app.command("start")
def start_command(
    instruction: str | None = typer.Argument(
        None,
        help="Current request. Omit to continue from the durable workspace state.",
    ),
    cwd: Path | None = typer.Option(None, "--cwd", help="AROS workspace root."),
    question: str | None = typer.Option(
        None,
        "--question",
        help="Key Research Question for a new workspace.",
    ),
    material: list[Path] | None = typer.Option(
        None,
        "--material",
        help="Local PDF or Markdown for a new workspace; repeatable.",
    ),
    max_turns: int = typer.Option(100, "--max-turns", min=1),
    provider: str | None = typer.Option(None, "--provider"),
    model: str | None = typer.Option(None, "--model"),
    reasoning_effort: str | None = typer.Option(
        None,
        "--reasoning-effort",
        help="OpenAI reasoning effort; AROS default is max.",
    ),
    allow_shell: bool = typer.Option(
        False,
        "--allow-shell",
        help="Enable bounded foreground shell commands for this trusted-local session.",
    ),
    cooperative_human_direct: bool = typer.Option(
        False,
        "--cooperative-human-direct",
        help=(
            "Allow explicitly cooperative same-UID checkpoints for this local "
            "Principal session; this is not protected authority."
        ),
    ),
) -> None:
    """Start a fresh native Principal from workspace state, never transcript replay."""
    requested_root = _root(cwd or Path("."))
    config_values: dict[str, object] = {
        "provider": AROS_DEFAULT_PROVIDER,
        "model": AROS_DEFAULT_MODEL,
        "reasoning_effort": AROS_DEFAULT_REASONING_EFFORT,
    }
    if provider is not None:
        config_values["provider"] = provider
    if model is not None:
        config_values["model"] = model
    if reasoning_effort is not None:
        config_values["reasoning_effort"] = reasoning_effort

    try:
        gateway = HumanDirectGateway() if cooperative_human_direct else None
        attention_context = (
            AttentionAuthorityContext(
                authority={
                    "state": "available",
                    "enforcement_class": "cooperative",
                    "issuer": "human-direct",
                },
                remaining_budget={
                    "state": "not_configured",
                    "enforcement_class": "cooperative",
                },
                institutional_obligations=(),
            )
            if cooperative_human_direct
            else None
        )
        status = status_workspace(requested_root)
        initialized = status.get("initialized") is True
        if initialized:
            if question is not None or material:
                raise ValueError(
                    "workspace is already initialized; intake arguments are invalid"
                )
            root = requested_root
        else:
            interactive = bool(
                getattr(sys.stdin, "isatty", lambda: False)()
                and getattr(sys.stdout, "isatty", lambda: False)()
            )
            intake = collect_start_intake(
                workspace=cwd,
                question=question,
                materials=material,
                interactive=interactive,
            )
            initialize_knowledge_bank(
                intake.workspace,
                intake.question,
                intake.materials,
            )
            root = _root(intake.workspace)
            if interactive:
                render_start_transition(
                    intake,
                    authority_class=(
                        "cooperative" if cooperative_human_direct else "unavailable"
                    ),
                    max_turns=max_turns,
                    allow_shell=allow_shell,
                )
        config = AgentConfig(
            **config_values,
            cwd=str(root),
            max_turns=max_turns,
            auto_git=False,
        )
        provider_obj = create_provider(config)
        boot_context = boot_workspace(root, context=attention_context)
        request = instruction or (
            "Continue the research mission from the current workspace state."
        )
        agent = build_principal_agent(
            provider_obj,
            root,
            boot_context,
            max_turns=max_turns,
            allow_shell=allow_shell,
            admission_gateway=gateway,
            attention_context=attention_context,
        )
        asyncio.run(run_principal(agent, request))
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except (OSError, RuntimeError, ValueError) as exc:
        _fail(exc)

__all__ = ["aros_app"]
