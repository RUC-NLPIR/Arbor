"""`arbor local` - WSL-native Arbor skill-suite adapter.

This command group is separate from the native Arbor runtime. The native
`arbor run` path uses configured LLM providers. `arbor local` assumes the Arbor
repository and target project both live inside WSL and delegates the workflow to
the installed Claude Code or Codex CLI.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import typer


VALID_AGENTS = {"auto", "claude", "codex"}
VALID_INSTALL_AGENTS = {"both", "claude", "codex"}
VALID_INSTALL_SCOPES = {"user", "project"}


local_app = typer.Typer(
    name="local",
    help="Run the local Arbor skill suite through WSL Claude Code or Codex CLI.",
    no_args_is_help=True,
)


@dataclass(frozen=True)
class CliProbe:
    name: str
    path: str | None
    runnable: bool
    version: str | None = None
    error: str | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _is_wsl() -> bool:
    if os.name == "nt":
        return False
    try:
        text = Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False
    return "microsoft" in text or "wsl" in text


def _is_windows_mount(path: Path) -> bool:
    resolved = path.expanduser().resolve()
    text = resolved.as_posix()
    return text == "/mnt" or text.startswith("/mnt/")


def _require_wsl_native_path(path: Path, *, label: str) -> Path:
    if not _is_wsl():
        raise typer.BadParameter(
            "`arbor local` must be run inside WSL. Clone Arbor into the Linux "
            "filesystem, for example `/home/<user>/agent-workspace/Arbor`."
        )
    resolved = path.expanduser().resolve()
    if _is_windows_mount(resolved):
        raise typer.BadParameter(
            f"{label} is under /mnt, which is a mounted Windows filesystem: {resolved}. "
            "Use a WSL-native clone under /home instead."
        )
    return resolved


def _default_skills_src() -> Path:
    return _repo_root() / "skills"


def _resolve_skills_src(skills_src: Path | None) -> Path:
    return skills_src.expanduser() if skills_src is not None else _default_skills_src()


def _skill_dirs(skills_src: Path) -> list[Path]:
    root = skills_src.expanduser().resolve()
    if not root.is_dir():
        raise typer.BadParameter(f"skills directory does not exist: {root}")
    skill_dirs = sorted(
        path for path in root.glob("arbor-*")
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    if not skill_dirs:
        raise typer.BadParameter(f"no arbor-* skills with SKILL.md found under {root}")
    if not (root / "arbor-research-agent" / "SKILL.md").is_file():
        raise typer.BadParameter(
            f"missing public entrypoint: {root / 'arbor-research-agent' / 'SKILL.md'}"
        )
    return skill_dirs


def _probe_cli(name: str) -> CliProbe:
    path = shutil.which(name)
    if not path:
        return CliProbe(name=name, path=None, runnable=False, error="not found on PATH")
    try:
        result = subprocess.run(
            [name, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return CliProbe(name=name, path=path, runnable=False, error=str(exc))
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return CliProbe(
            name=name,
            path=path,
            runnable=False,
            version=output or None,
            error=f"`{name} --version` exited {result.returncode}",
        )
    return CliProbe(name=name, path=path, runnable=True, version=output or None)


def _select_agent(agent: str) -> str:
    if agent not in VALID_AGENTS:
        raise typer.BadParameter(
            f"--agent must be one of {', '.join(sorted(VALID_AGENTS))}"
        )
    if agent != "auto":
        probe = _probe_cli(agent)
        if not probe.runnable:
            raise typer.BadParameter(
                f"{agent} CLI is not runnable: {probe.error or 'unknown error'}"
            )
        return agent

    probes = [_probe_cli("claude"), _probe_cli("codex")]
    for probe in probes:
        if probe.runnable:
            return probe.name
    details = "; ".join(
        f"{probe.name}: {probe.error or 'not runnable'}" for probe in probes
    )
    raise typer.BadParameter(f"no supported local agent CLI is runnable ({details})")


def _build_prompt(*, skills_src: Path, task: str, agent: str) -> str:
    skills_src = skills_src.expanduser().resolve()
    entrypoint = skills_src / "arbor-research-agent" / "SKILL.md"
    agent_label = "Claude Code" if agent == "claude" else "Codex"
    return f"""Use the local Arbor skill suite at:
{skills_src}

Read this public entrypoint and follow it:
{entrypoint}

Load internal arbor-* phase skills from sibling directories under the same
local skills directory whenever the entrypoint or orchestrator instructs you to.

Constraints for this adaptation:
- Run entirely inside WSL from WSL-native paths.
- Use this local Arbor checkout; do not clone Arbor from GitHub.
- Do not use the native Arbor provider runtime, Arbor API setup, or hosted
  provider configuration directly.
- Execute the workflow with the current {agent_label} CLI and local filesystem
  tools.
- Keep Arbor guardrails: clarify ambiguous target, metric, eval, permissions,
  and budget before real optimization; protect B_test; ask before package
  installs, downloads, long or GPU jobs, merge attempts, or final test use
  unless the user request explicitly allows them.

User request:
{task}
"""


def _build_command(
    *,
    agent: str,
    cwd: Path,
    skills_src: Path,
    task: str,
    claude_permission_mode: str | None = None,
) -> list[str]:
    prompt = _build_prompt(skills_src=skills_src, task=task, agent=agent)
    arbor_root = skills_src.expanduser().resolve().parent
    if agent == "claude":
        command = [
            "claude",
            "--print",
            "--output-format",
            "text",
            "--add-dir",
            str(arbor_root),
        ]
        if claude_permission_mode:
            command.extend(["--permission-mode", claude_permission_mode])
        command.append(prompt)
        return command
    if agent == "codex":
        return [
            "codex",
            "exec",
            "--add-dir",
            str(arbor_root),
            "-C",
            str(cwd.expanduser().resolve()),
            prompt,
        ]
    raise ValueError(f"unknown agent: {agent}")


def _codex_user_skills_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "skills"


def _claude_skills_dir(scope: str, target_repo: Path | None) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "skills"
    if target_repo is None:
        raise typer.BadParameter("--target-repo is required for project-scope Claude install")
    return target_repo.expanduser().resolve() / ".claude" / "skills"


def _install_suite(*, skills_src: Path, dest_root: Path, force: bool) -> list[Path]:
    copied: list[Path] = []
    dest_root = dest_root.expanduser().resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    for src in _skill_dirs(skills_src):
        dest = dest_root / src.name
        if force and dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, dirs_exist_ok=True)
        copied.append(dest)
    return copied


@local_app.command("doctor")
def doctor_command(
    skills_src: Path | None = typer.Option(
        None,
        "--skills-src",
        help="Path to this WSL-local Arbor checkout's skills directory.",
        exists=False,
        file_okay=False,
    ),
) -> None:
    """Check WSL-local skill suite and Claude/Codex CLI availability."""
    typer.secho("\narbor local doctor\n", fg=typer.colors.CYAN, bold=True)
    problems = 0

    try:
        arbor_root = _require_wsl_native_path(_repo_root(), label="Arbor checkout")
        typer.secho(f"  OK WSL checkout: {arbor_root}", fg=typer.colors.GREEN)
    except typer.BadParameter as exc:
        typer.secho(f"  FAIL WSL checkout: {exc}", fg=typer.colors.RED)
        problems += 1

    try:
        root = _require_wsl_native_path(
            _resolve_skills_src(skills_src),
            label="skills directory",
        )
        skills = _skill_dirs(root)
        typer.secho(f"  OK skills: {root} ({len(skills)} arbor-* dirs)", fg=typer.colors.GREEN)
    except typer.BadParameter as exc:
        typer.secho(f"  FAIL skills: {exc}", fg=typer.colors.RED)
        problems += 1

    runnable_agents = 0
    for name in ("claude", "codex"):
        probe = _probe_cli(name)
        if probe.runnable:
            runnable_agents += 1
            suffix = f" - {probe.version}" if probe.version else ""
            typer.secho(f"  OK {name}: {probe.path}{suffix}", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"  WARN {name}: {probe.error or 'not runnable'}"
                + (f" ({probe.path})" if probe.path else ""),
                fg=typer.colors.YELLOW,
            )
    if runnable_agents == 0:
        typer.secho("  FAIL agents: neither claude nor codex is runnable in WSL", fg=typer.colors.RED)
        problems += 1

    raise typer.Exit(code=1 if problems else 0)


@local_app.command("install")
def install_command(
    agent: str = typer.Option(
        "both",
        "--agent",
        help="Install for claude, codex, or both.",
    ),
    scope: str = typer.Option(
        "user",
        "--scope",
        help="Install to user skill dir or project skill dir. Codex supports user scope.",
    ),
    target_repo: Path | None = typer.Option(
        None,
        "--target-repo",
        help="Target WSL project for project-scope Claude installation.",
        exists=False,
        file_okay=False,
    ),
    skills_src: Path | None = typer.Option(
        None,
        "--skills-src",
        help="Path to this WSL-local Arbor checkout's skills directory.",
        exists=False,
        file_okay=False,
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace existing arbor-* skill directories before copying.",
    ),
) -> None:
    """Install the WSL-local Arbor skill suite for Claude Code and/or Codex."""
    if agent not in VALID_INSTALL_AGENTS:
        raise typer.BadParameter(
            f"--agent must be one of {', '.join(sorted(VALID_INSTALL_AGENTS))}"
        )
    if scope not in VALID_INSTALL_SCOPES:
        raise typer.BadParameter(
            f"--scope must be one of {', '.join(sorted(VALID_INSTALL_SCOPES))}"
        )

    _require_wsl_native_path(_repo_root(), label="Arbor checkout")
    src = _require_wsl_native_path(_resolve_skills_src(skills_src), label="skills directory")
    targets: list[tuple[str, Path]] = []
    if agent in ("both", "claude"):
        targets.append(("claude", _claude_skills_dir(scope, target_repo)))
    if agent in ("both", "codex"):
        if scope != "user":
            raise typer.BadParameter("Codex skill installation currently supports --scope user only")
        targets.append(("codex", _codex_user_skills_dir()))

    for name, dest in targets:
        dest = _require_wsl_native_path(dest, label=f"{name} skills destination")
        copied = _install_suite(skills_src=src, dest_root=dest, force=force)
        typer.secho(
            f"{name}: copied {len(copied)} skills to {dest}",
            fg=typer.colors.GREEN,
        )
    typer.echo("Restart Claude Code or Codex inside WSL before invoking installed skills.")


@local_app.command("run")
def run_command(
    task: str = typer.Argument(..., help="Arbor-style research or optimization request."),
    agent: str = typer.Option(
        "auto",
        "--agent",
        help="Local agent CLI to use: auto, claude, or codex.",
    ),
    cwd: Path = typer.Option(
        Path("."),
        "--cwd",
        "-C",
        help="WSL-native target project directory.",
        exists=False,
        file_okay=False,
    ),
    skills_src: Path | None = typer.Option(
        None,
        "--skills-src",
        help="Path to this WSL-local Arbor checkout's skills directory.",
        exists=False,
        file_okay=False,
    ),
    claude_permission_mode: str | None = typer.Option(
        None,
        "--claude-permission-mode",
        help="Optional Claude Code permission mode, e.g. default, acceptEdits, dontAsk, plan.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the command instead of running it.",
    ),
) -> None:
    """Launch an Arbor skill-suite session through a WSL-local agent CLI."""
    _require_wsl_native_path(_repo_root(), label="Arbor checkout")
    target = _require_wsl_native_path(cwd, label="target cwd")
    if not target.is_dir():
        raise typer.BadParameter(f"target cwd does not exist: {target}")
    src = _require_wsl_native_path(_resolve_skills_src(skills_src), label="skills directory")
    _skill_dirs(src)
    selected = _select_agent(agent)
    command = _build_command(
        agent=selected,
        cwd=target,
        skills_src=src,
        task=task,
        claude_permission_mode=claude_permission_mode,
    )

    if dry_run:
        import shlex

        typer.echo(shlex.join(command))
        return

    typer.secho(f"launching Arbor skill suite with WSL {selected}...", fg=typer.colors.CYAN)
    result = subprocess.run(command, cwd=str(target), check=False)
    raise typer.Exit(code=result.returncode)
