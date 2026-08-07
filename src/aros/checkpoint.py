from __future__ import annotations

import stat
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

from .observed import ObservedRefError, validate_observed_ref


class CheckpointError(ValueError):
    pass


class GitCheckpoint:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        top = self._git("rev-parse", "--show-toplevel", check=False)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != self.root:
            raise CheckpointError("checkpoint root must be a Git repository root")

    def commit(
        self,
        *,
        paths: Sequence[str],
        message: str,
        observed_refs: Iterable[str] = (),
    ) -> dict[str, object]:
        selected = self._paths(paths)
        if not isinstance(message, str) or not message.strip():
            raise CheckpointError("checkpoint message must be non-empty")
        observations = self._observed(observed_refs)
        if self._git("symbolic-ref", "--quiet", "HEAD", check=False).returncode:
            raise CheckpointError("checkpoint requires an attached Git branch")
        cached = self._git("diff", "--cached", "--quiet", "--exit-code", check=False)
        if cached.returncode != 0:
            raise CheckpointError("ordinary Git index must be clean")

        parent = self._git("rev-parse", "HEAD").stdout.strip()
        snapshots = {path: self._snapshot(path) for path in selected}
        added = self._git("add", "-A", "--", *selected, check=False)
        if added.returncode != 0:
            raise CheckpointError(self._detail(added, "unable to stage checkpoint paths"))
        if self._git("diff", "--cached", "--quiet", "--exit-code", check=False).returncode == 0:
            raise CheckpointError("checkpoint paths contain no changes")

        full_message = message.strip()
        if observations:
            full_message += "\n\n" + "\n".join(
                f"AROS-Observed: {ref}" for ref in observations
            )
        command = ["commit", "--only", "--no-verify", "-m", full_message, "--", *selected]
        if not self._identity_available():
            command = [
                "-c",
                "user.name=AROS Principal",
                "-c",
                "user.email=aros-principal@local.invalid",
                *command,
            ]
        committed = self._git(*command, check=False)
        if committed.returncode != 0:
            raise CheckpointError(self._detail(committed, "Git commit failed"))

        commit = self._git("rev-parse", "HEAD").stdout.strip()
        if commit == parent or self._git("rev-parse", "HEAD^").stdout.strip() != parent:
            raise CheckpointError("checkpoint did not advance HEAD exactly once")
        changed = tuple(
            sorted(
                self._git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines()
            )
        )
        if changed != selected:
            raise CheckpointError("checkpoint commit paths differ from selected paths")
        for path, before in snapshots.items():
            current = self._git("show", f"HEAD:{path}", check=False)
            if before is None:
                if current.returncode == 0:
                    raise CheckpointError(f"tracked deletion was not committed: {path}")
            elif current.returncode != 0 or current.stdout.encode("utf-8", errors="surrogateescape") != before:
                raw = subprocess.run(
                    ["git", "-C", str(self.root), "show", f"HEAD:{path}"],
                    capture_output=True,
                    check=False,
                )
                if raw.returncode != 0 or raw.stdout != before:
                    raise CheckpointError(f"checkpoint blob differs: {path}")
        return {
            "commit": commit,
            "parent": parent,
            "paths": list(selected),
            "observed_refs": list(observations),
            "enforcement_class": "cooperative",
        }

    def commit_paths(
        self,
        paths: Sequence[str],
        message: str,
    ) -> dict[str, object]:
        selected = self._paths(paths)
        if not isinstance(message, str) or not message.strip():
            raise CheckpointError("checkpoint message must be non-empty")
        cached = self._git("diff", "--cached", "--quiet", "--exit-code", check=False)
        if cached.returncode != 0:
            raise CheckpointError("ordinary Git index must be clean")
        status = self._git(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            *selected,
        )
        if not status.stdout:
            return {
                "commit": self._git("rev-parse", "HEAD").stdout.strip(),
                "paths": list(selected),
                "reused": True,
                "enforcement_class": "cooperative",
            }
        return self.commit(paths=paths, message=message)

    def _paths(self, paths: Sequence[str]) -> tuple[str, ...]:
        if isinstance(paths, (str, bytes)) or not paths:
            raise CheckpointError("checkpoint paths must be a non-empty list")
        if len(set(paths)) != len(paths):
            raise CheckpointError("checkpoint paths must be unique")
        selected: list[str] = []
        for value in paths:
            if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
                raise CheckpointError("checkpoint path is invalid")
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or path.as_posix() != value
                or any(part in {"", ".", ".."} for part in path.parts)
                or path.parts[0] in {".git", ".aros", ".worktree"}
            ):
                raise CheckpointError(f"checkpoint path is unsafe: {value}")
            target = self.root / value
            try:
                metadata = target.lstat()
            except FileNotFoundError:
                if self._git("ls-files", "--error-unmatch", "--", value, check=False).returncode:
                    raise CheckpointError(f"checkpoint path is absent and untracked: {value}")
            else:
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise CheckpointError(f"checkpoint path must be a plain file: {value}")
            selected.append(value)
        return tuple(sorted(selected))

    def _observed(self, refs: Iterable[str]) -> tuple[str, ...]:
        try:
            return tuple(sorted({validate_observed_ref(ref) for ref in refs}))
        except (ObservedRefError, TypeError) as error:
            raise CheckpointError("observed refs are invalid") from error

    def _snapshot(self, path: str) -> bytes | None:
        target = self.root / path
        return target.read_bytes() if target.exists() else None

    def _identity_available(self) -> bool:
        return all(
            self._git("config", "--get", key, check=False).returncode == 0
            for key in ("user.name", "user.email")
        )

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
        )
        if check and result.returncode != 0:
            raise CheckpointError(self._detail(result, f"git {args[0]} failed"))
        return result

    @staticmethod
    def _detail(result: subprocess.CompletedProcess[str], fallback: str) -> str:
        return (result.stderr or result.stdout).strip() or fallback


__all__ = ["CheckpointError", "GitCheckpoint"]
