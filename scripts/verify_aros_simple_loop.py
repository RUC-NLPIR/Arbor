from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^TASK-[0-9]{8}-[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_RUN_ID = re.compile(r"^RUN-[A-Za-z0-9][A-Za-z0-9-]*$")
_EVAL_ID = re.compile(r"^EVAL-[0-9a-f]{64}$")
_SEMANTIC_PATHS = [
    "ideas/I-E2E.md",
    "knowledge/claims/C-0001.md",
    "memory/NOW.md",
    "model/CURRENT.md",
    "questions/Q-0001/question.md",
]
_ENVIRONMENT_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "VIRTUAL_ENV",
    "VIRTUAL_ENV_PROMPT",
)
_PREREGISTRATION_WRITES = {
    "model/CURRENT.md": "# Current Model\n\nThe candidate should emit `success`.\n",
    "ideas/I-E2E.md": (
        "---\nid: I-E2E\n---\n# Idea\n\n"
        "Test the exact candidate with evaluator v1.\n"
    ),
}


class VerificationError(ValueError):
    pass


def _controlled_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in _ENVIRONMENT_KEYS if key in os.environ}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def _json(path: Path) -> dict[str, object]:
    return _strict_object(path.read_bytes(), str(path))


def _strict_object(raw: bytes, description: str) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in items:
            if key in value:
                raise VerificationError(f"duplicate JSON key in {description}: {key}")
            value[key] = item
        return value

    def invalid_constant(value: str) -> object:
        raise VerificationError(f"invalid JSON constant in {description}: {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
    except UnicodeError as error:
        raise VerificationError(f"invalid UTF-8 JSON: {description}") from error
    if not isinstance(value, dict):
        raise VerificationError(f"object required: {description}")
    return value


def _git(project: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(project), *args],
        capture_output=True,
        check=False,
        env=_controlled_environment(),
    )
    if result.returncode != 0:
        raise VerificationError(f"Git failed: {' '.join(args)}")
    return result.stdout


def _git_text(project: Path, *args: str) -> str:
    return _git(project, *args).decode("utf-8").strip()


def _record_hash(value: dict[str, object], field: str) -> str:
    observed = value.get(field)
    if not isinstance(observed, str) or _SHA256.fullmatch(observed) is None:
        raise VerificationError(f"invalid {field}")
    payload = {key: item for key, item in value.items() if key != field}
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if hashlib.sha256(raw).hexdigest() != observed:
        raise VerificationError(f"{field} mismatch")
    return observed


def _json_hash(value: dict[str, object]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _file_tree(root: Path) -> tuple[list[dict[str, object]], str]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise VerificationError(f"package tree contains a symlink: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise VerificationError(f"package tree entry is not a plain file: {path}")
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": metadata.st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return entries, hashlib.sha256(raw).hexdigest()


def _hash_field(payload: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
    return "sha256=" + digest.decode("ascii")


def _metadata_fields(payload: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in payload.decode("utf-8").splitlines():
        if ": " in line:
            key, value = line.split(": ", 1)
            fields.setdefault(key, value)
    return fields


def _console_scripts(payload: bytes) -> dict[str, str]:
    scripts: dict[str, str] = {}
    section = ""
    for line in payload.decode("utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
        elif section == "console_scripts" and "=" in stripped:
            name, target = stripped.split("=", 1)
            command = name.strip()
            if command in scripts:
                raise VerificationError("wheel console script is duplicated")
            scripts[command] = target.strip()
    if scripts.get("aros") != "arbor.cli.aros_app:main":
        raise VerificationError("wheel aros console script entrypoint differs")
    return scripts


def _wheel_inventory(
    path: Path,
) -> tuple[
    list[dict[str, object]],
    dict[str, bytes],
    dict[str, int],
    str,
    dict[str, str],
    str,
]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)):
            raise VerificationError("wheel contains duplicate entries")
        payloads: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        for item in infos:
            name = item.filename
            canonical = PurePosixPath(name)
            mode = item.external_attr >> 16
            if (
                item.is_dir()
                or not name
                or "\\" in name
                or canonical.is_absolute()
                or canonical.as_posix() != name
                or any(part in {"", ".", ".."} for part in canonical.parts)
                or "__pycache__" in canonical.parts
                or name.endswith((".pyc", ".pyo", ".pth"))
                or canonical.name in {"sitecustomize.py", "usercustomize.py"}
                or not stat.S_ISREG(mode)
                or stat.S_IMODE(mode) not in {0o644, 0o755}
            ):
                raise VerificationError(f"wheel member is unsafe: {name}")
            payloads[name] = archive.read(item)
            modes[name] = stat.S_IMODE(mode)
    dist_roots = {
        name.split("/", 1)[0]
        for name in payloads
        if name.split("/", 1)[0].endswith(".dist-info")
    }
    if len(dist_roots) != 1:
        raise VerificationError("wheel dist-info root differs")
    dist_root = next(iter(dist_roots))
    required_dist = {
        f"{dist_root}/METADATA",
        f"{dist_root}/WHEEL",
        f"{dist_root}/RECORD",
        f"{dist_root}/entry_points.txt",
        f"{dist_root}/top_level.txt",
        f"{dist_root}/licenses/LICENSE",
    }
    if set(payloads) - {name for name in payloads if name.startswith("arbor/")} != required_dist:
        raise VerificationError("wheel contains an unexpected non-arbor member")
    if any(modes[name] != 0o644 for name in required_dist):
        raise VerificationError("wheel dist-info member mode differs")
    record_name = f"{dist_root}/RECORD"
    rows = list(csv.reader(io.StringIO(payloads[record_name].decode("utf-8"))))
    if any(len(row) != 3 for row in rows) or len(rows) != len(payloads):
        raise VerificationError("wheel RECORD shape differs")
    recorded = {row[0]: row[1:] for row in rows}
    if len(recorded) != len(rows) or set(recorded) != set(payloads):
        raise VerificationError("wheel RECORD inventory differs")
    for name, payload in payloads.items():
        hash_value, size_value = recorded[name]
        if name == record_name:
            if hash_value or size_value:
                raise VerificationError("wheel RECORD self entry differs")
        elif hash_value != _hash_field(payload) or size_value != str(len(payload)):
            raise VerificationError(f"wheel RECORD hash or size differs: {name}")
    metadata = _metadata_fields(payloads[f"{dist_root}/METADATA"])
    if metadata.get("Name", "").lower().replace("_", "-") != "arbor-agent":
        raise VerificationError("wheel distribution metadata differs")
    version = metadata.get("Version")
    if not version:
        raise VerificationError("wheel distribution version is missing")
    console = _console_scripts(payloads[f"{dist_root}/entry_points.txt"])
    entries = [
        {
            "path": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(payloads.items())
    ]
    return entries, payloads, modes, version, console, dist_root


def _runtime_environment_identity() -> tuple[Path, Path]:
    python = Path(sys.executable).absolute()
    return python.parent.parent.resolve(strict=True), python


def _probe_filesystem_modes(root: Path) -> bool:
    directory = Path(tempfile.mkdtemp(prefix=".aros-mode-probe-", dir=root))
    probe = directory / "probe"
    try:
        probe.write_bytes(b"")
        directory.chmod(0o700)
        probe.chmod(0o600)
        return (
            stat.S_IMODE(directory.stat().st_mode) == 0o700
            and stat.S_IMODE(probe.stat().st_mode) == 0o600
        )
    finally:
        try:
            probe.unlink()
        finally:
            directory.rmdir()


def _installed_inventory(
    purelib: Path,
    dist_root: str,
    wheel_modes: dict[str, int],
    permissions_enforced: bool,
    bin_dir: Path,
    console_scripts: dict[str, str],
    retained_wheel: Path,
) -> tuple[list[dict[str, object]], dict[str, bytes], Path, Path, str, str]:
    distribution = purelib / "arbor"
    dist_info = purelib / dist_root
    allowed_dist = {
        "METADATA",
        "WHEEL",
        "RECORD",
        "entry_points.txt",
        "top_level.txt",
        "licenses/LICENSE",
        "INSTALLER",
        "REQUESTED",
        "direct_url.json",
    }
    for root in (distribution, dist_info):
        if any(
            "__pycache__" in path.relative_to(root).parts
            or path.suffix in {".pyc", ".pyo"}
            for path in root.rglob("*")
        ):
            raise VerificationError("installed distribution contains bytecode cache files")
    expected_arbor_dirs = {
        parent.as_posix()
        for name in wheel_modes
        if name.startswith("arbor/")
        for parent in PurePosixPath(name.removeprefix("arbor/")).parents
        if parent.as_posix() != "."
    }
    expected_dist_dirs = {
        parent.as_posix()
        for name in allowed_dist
        for parent in PurePosixPath(name).parents
        if parent.as_posix() != "."
    }
    for root, expected in (
        (distribution, expected_arbor_dirs),
        (dist_info, expected_dist_dirs),
    ):
        actual = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        }
        if actual != expected:
            raise VerificationError("installed distribution directory inventory differs")
    arbor_entries, arbor_hash = _file_tree(distribution)
    dist_entries, _dist_hash = _file_tree(dist_info)
    if {str(item["path"]) for item in dist_entries} != allowed_dist:
        raise VerificationError("installed dist-info inventory differs")
    payloads = {
        **{
            "arbor/" + str(item["path"]): distribution.joinpath(
                str(item["path"])
            ).read_bytes()
            for item in arbor_entries
        },
        **{
            f"{dist_root}/" + str(item["path"]): dist_info.joinpath(
                str(item["path"])
            ).read_bytes()
            for item in dist_entries
        },
    }
    for command in console_scripts:
        script = bin_dir / command
        if not script.is_file():
            raise VerificationError(f"installed console script is missing: {command}")
        payloads[f"../../../bin/{command}"] = script.read_bytes()
    record_name = f"{dist_root}/RECORD"
    rows = list(csv.reader(io.StringIO(payloads[record_name].decode("utf-8"))))
    if any(len(row) != 3 for row in rows):
        raise VerificationError("installed RECORD shape differs")
    recorded = {row[0]: row[1:] for row in rows}
    if len(recorded) != len(rows) or any(
        "__pycache__" in PurePosixPath(name).parts or name.endswith((".pyc", ".pyo"))
        for name in recorded
    ):
        raise VerificationError("installed RECORD contains bytecode or duplicates")
    if set(recorded) != set(payloads):
        raise VerificationError("installed RECORD row set differs")
    for name, payload in payloads.items():
        if name not in recorded:
            raise VerificationError(f"installed RECORD omits file: {name}")
        hash_value, size_value = recorded[name]
        if name == record_name:
            if hash_value or size_value:
                raise VerificationError("installed RECORD self entry differs")
        elif hash_value != _hash_field(payload) or size_value != str(len(payload)):
            raise VerificationError(f"installed RECORD hash or size differs: {name}")
    if payloads[f"{dist_root}/INSTALLER"] != b"pip\n":
        raise VerificationError("installed INSTALLER receipt differs")
    if payloads[f"{dist_root}/REQUESTED"] != b"":
        raise VerificationError("installed REQUESTED receipt differs")
    try:
        direct = json.loads(payloads[f"{dist_root}/direct_url.json"])
        archive_info = direct["archive_info"]
        url = urllib.parse.urlparse(direct["url"])
        expected_sha = hashlib.sha256(retained_wheel.read_bytes()).hexdigest()
        if (
            url.scheme != "file"
            or Path(urllib.parse.unquote(url.path)).name != retained_wheel.name
            or archive_info.get("hash") != f"sha256={expected_sha}"
            or archive_info.get("hashes") != {"sha256": expected_sha}
        ):
            raise ValueError("direct_url binding differs")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise VerificationError("installed direct_url receipt differs") from error
    if permissions_enforced:
        for name, expected_mode in wheel_modes.items():
            installed = purelib / name
            if name.endswith("/RECORD"):
                installed = dist_info / "RECORD"
            try:
                observed = stat.S_IMODE(installed.stat().st_mode)
            except OSError as error:
                raise VerificationError(
                    f"installed distribution file is missing: {name}"
                ) from error
            if bool(observed & 0o111) != bool(expected_mode & 0o111):
                raise VerificationError(f"installed file executable mode differs: {name}")
        if any(
            stat.S_IMODE((dist_info / name).stat().st_mode) & 0o111
            for name in {"INSTALLER", "REQUESTED", "direct_url.json"}
        ):
            raise VerificationError("installed generated dist-info file is executable")
    entries = [
        {
            "path": name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(payloads.items())
    ]
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return entries, payloads, distribution, dist_info, arbor_hash, hashlib.sha256(raw).hexdigest()


def _canonical_aros_wrapper(interpreter: Path) -> bytes:
    return (
        f"#!{interpreter}\n"
        "# -*- coding: utf-8 -*-\n"
        "import re\n"
        "import sys\n"
        "from arbor.cli.aros_app import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(main())\n"
    ).encode()


def _committed_object(project: Path, commit: str, ref: str) -> dict[str, object]:
    return _strict_object(_git(project, "show", f"{commit}:{ref}"), ref)


def _verify_product(
    evidence: dict[str, object],
    evidence_path: Path,
) -> Path:
    product = evidence.get("product")
    required = {
        "source_commit",
        "source_repository",
        "distribution",
        "distribution_version",
        "wheel_ref",
        "wheel_sha256",
        "package_root",
        "package_tree_sha256",
        "distribution_root",
        "distribution_tree_sha256",
        "aros_executable",
        "aros_executable_sha256",
        "console_script",
        "python_executable",
        "environment_root",
        "dist_info_root",
        "wheel_inventory_sha256",
        "installed_inventory_sha256",
        "filesystem_permissions_enforced",
    }
    if isinstance(product, dict) and "source_repository" not in product:
        raise VerificationError("source repository evidence is missing")
    if not isinstance(product, dict) or set(product) != required:
        raise VerificationError("product evidence is incomplete")
    source_commit = product.get("source_commit")
    version = product.get("distribution_version")
    if not isinstance(source_commit, str) or _COMMIT.fullmatch(source_commit) is None:
        raise VerificationError("source commit is invalid")
    if product.get("distribution") != "arbor-agent" or not isinstance(version, str):
        raise VerificationError("distribution identity is invalid")
    suffix = re.search(r"\+g([0-9a-f]+)", version)
    if suffix is None:
        raise VerificationError("distribution version lacks a source commit suffix")

    source_value = product.get("source_repository")
    if not isinstance(source_value, str):
        raise VerificationError("source repository is missing")
    supplied_source = Path(source_value).absolute()
    source_repository = supplied_source.resolve(strict=True)
    if (
        supplied_source != source_repository
        or not source_repository.is_dir()
        or stat.S_ISLNK(source_repository.lstat().st_mode)
        or _git_text(source_repository, "rev-parse", "--show-toplevel")
        != str(source_repository)
    ):
        raise VerificationError("source repository is invalid")
    try:
        resolved_source = _git_text(
            source_repository,
            "rev-parse",
            "--verify",
            f"{source_commit}^{{commit}}",
        )
        resolved_suffix = _git_text(
            source_repository,
            "rev-parse",
            "--verify",
            f"{suffix.group(1)}^{{commit}}",
        )
    except VerificationError as error:
        raise VerificationError("source commit is unavailable") from error
    if resolved_source != source_commit or resolved_suffix != source_commit:
        raise VerificationError("source commit differs from distribution version")

    wheel_ref = product.get("wheel_ref")
    if not isinstance(wheel_ref, str):
        raise VerificationError("wheel ref is invalid")
    wheel_relative = Path(wheel_ref)
    if (
        wheel_relative.is_absolute()
        or wheel_relative.as_posix() != wheel_ref
        or wheel_relative.parts[:1] != ("artifacts",)
        or any(part in {"", ".", ".."} for part in wheel_relative.parts)
    ):
        raise VerificationError("wheel ref is invalid")
    wheel = evidence_path.parent / wheel_relative
    if wheel.absolute() != wheel or wheel.resolve(strict=True) != wheel:
        raise VerificationError("wheel evidence path is not physical")
    wheel_metadata = wheel.lstat()
    if not stat.S_ISREG(wheel_metadata.st_mode) or wheel_metadata.st_nlink != 1:
        raise VerificationError("wheel evidence is not a plain file")
    wheel_hash = product.get("wheel_sha256")
    if (
        not isinstance(wheel_hash, str)
        or _SHA256.fullmatch(wheel_hash) is None
        or hashlib.sha256(wheel.read_bytes()).hexdigest() != wheel_hash
    ):
        raise VerificationError("wheel hash differs")
    wheel_entries, wheel_payloads, wheel_modes, wheel_version, console_scripts, dist_root = (
        _wheel_inventory(wheel)
    )
    if wheel_version != version:
        raise VerificationError("wheel distribution version differs")
    wheel_inventory_hash = hashlib.sha256(
        json.dumps(wheel_entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if product.get("wheel_inventory_sha256") != wheel_inventory_hash:
        raise VerificationError("wheel inventory hash differs")

    distribution_value = product.get("distribution_root")
    if not isinstance(distribution_value, str):
        raise VerificationError("installed distribution root is missing")
    supplied_distribution = Path(distribution_value).absolute()
    distribution_root = supplied_distribution.resolve(strict=True)
    environment_root, runtime_python = _runtime_environment_identity()
    purelib = distribution_root.parent
    environment_value = product.get("environment_root")
    python_value = product.get("python_executable")
    dist_info_value = product.get("dist_info_root")
    permissions_enforced = product.get("filesystem_permissions_enforced")
    if type(permissions_enforced) is not bool:
        raise VerificationError("filesystem permission enforcement evidence differs")
    observed_permissions = _probe_filesystem_modes(environment_root)
    if permissions_enforced != observed_permissions:
        raise VerificationError("filesystem permission probe differs from evidence")
    if (
        not isinstance(environment_value, str)
        or Path(environment_value).absolute().resolve(strict=True) != environment_root
        or not isinstance(python_value, str)
        or Path(python_value).absolute() != runtime_python
        or runtime_python.parent != environment_root / "bin"
        or supplied_distribution != distribution_root
        or distribution_root != purelib / "arbor"
        or not distribution_root.is_relative_to(environment_root)
        or "site-packages" not in distribution_root.parts
        or stat.S_ISLNK(distribution_root.lstat().st_mode)
        or not distribution_root.is_dir()
    ):
        raise VerificationError("installed distribution root is outside the environment")
    (
        _installed_entries,
        installed_payloads,
        installed_distribution,
        dist_info_root,
        distribution_tree_sha256,
        installed_inventory_sha256,
    ) = _installed_inventory(
        purelib,
        dist_root,
        wheel_modes,
        observed_permissions,
        environment_root / "bin",
        console_scripts,
        wheel,
    )
    if (
        installed_distribution != distribution_root
        or not isinstance(dist_info_value, str)
        or Path(dist_info_value).absolute().resolve(strict=True) != dist_info_root
        or product.get("installed_inventory_sha256") != installed_inventory_sha256
    ):
        raise VerificationError("installed distribution inventory identity differs")
    recorded_distribution_tree = product.get("distribution_tree_sha256")
    if (
        not isinstance(recorded_distribution_tree, str)
        or _SHA256.fullmatch(recorded_distribution_tree) is None
        or recorded_distribution_tree != distribution_tree_sha256
    ):
        raise VerificationError("installed distribution tree hash differs")

    package_value = product.get("package_root")
    if evidence.get("package_root") != package_value or not isinstance(package_value, str):
        raise VerificationError("package root evidence differs")
    supplied_package = Path(package_value).absolute()
    package = supplied_package.resolve(strict=True)
    if (
        supplied_package != package
        or package != distribution_root / "aros"
        or "site-packages" not in package.parts
        or stat.S_ISLNK(package.lstat().st_mode)
        or not package.is_dir()
    ):
        raise VerificationError("package root is outside the executing environment")
    package_entries, package_tree_sha256 = _file_tree(package)
    recorded_tree = product.get("package_tree_sha256")
    if (
        not isinstance(recorded_tree, str)
        or _SHA256.fullmatch(recorded_tree) is None
        or recorded_tree != package_tree_sha256
    ):
        raise VerificationError("package tree hash differs")
    if product.get("console_script") != console_scripts["aros"]:
        raise VerificationError("recorded aros console script differs")
    wheel_arbor = {
        name: payload for name, payload in wheel_payloads.items() if name.startswith("arbor/")
    }
    installed_arbor = {
        name: payload
        for name, payload in installed_payloads.items()
        if name.startswith("arbor/")
    }
    if wheel_arbor != installed_arbor:
        raise VerificationError("wheel and installed arbor trees differ")
    wheel_dist_files = {
        name: payload
        for name, payload in wheel_payloads.items()
        if name.startswith(f"{dist_root}/") and not name.endswith("/RECORD")
    }
    if any(installed_payloads.get(name) != payload for name, payload in wheel_dist_files.items()):
        raise VerificationError("wheel and installed dist-info files differ")
    source_rows = _git_text(
        source_repository,
        "ls-tree",
        "-r",
        source_commit,
        "--",
        "src",
        "skills",
    ).splitlines()
    source_files = {
        (
            "skills_suite/" + row.split("\t", 1)[1].removeprefix("skills/")
            if row.split("\t", 1)[1].startswith("skills/")
            else row.split("\t", 1)[1].removeprefix("src/")
        ): (
            row.split("\t", 1)[1],
            row.split(" ", 1)[0],
        )
        for row in source_rows
        if "\t" in row and row.split("\t", 1)[1].startswith(("src/", "skills/"))
    }
    source_payloads = {
        name.removeprefix("arbor/"): payload for name, payload in wheel_arbor.items()
    }
    if set(source_payloads) != set(source_files):
        difference = sorted(set(source_payloads) ^ set(source_files))
        raise VerificationError(f"wheel/source distribution file set differs: {difference}")
    for relative, (source_path, source_mode) in source_files.items():
        if source_mode not in {"100644", "100755"}:
            raise VerificationError(f"source file mode is unsupported: {relative}")
        wheel_mode = wheel_modes[f"arbor/{relative}"]
        expected_mode = 0o755 if source_mode == "100755" else 0o644
        if wheel_mode != expected_mode:
            raise VerificationError(f"wheel differs from source mode: {relative}")
        if source_payloads[relative] != _git(
            source_repository,
            "show",
            f"{source_commit}:{source_path}",
        ):
            raise VerificationError(f"wheel differs from source blob: {relative}")

    executable_value = product.get("aros_executable")
    executable_hash = product.get("aros_executable_sha256")
    if not isinstance(executable_value, str):
        raise VerificationError("aros executable evidence is missing")
    supplied_executable = Path(executable_value).absolute()
    executable = supplied_executable.resolve(strict=True)
    try:
        executable_bytes = executable.read_bytes()
        shebang = executable_bytes.splitlines()[0].decode("utf-8").removeprefix("#!")
        shebang_path = Path(shebang)
    except (IndexError, OSError, UnicodeError) as error:
        raise VerificationError("aros executable shebang is invalid") from error
    if (
        supplied_executable != executable
        or executable.name != "aros"
        or executable.parent != environment_root / "bin"
        or not stat.S_ISREG(executable.lstat().st_mode)
        or executable.lstat().st_nlink != 1
        or not os.access(executable, os.X_OK)
        or not isinstance(executable_hash, str)
        or hashlib.sha256(executable_bytes).hexdigest() != executable_hash
        or not shebang_path.is_absolute()
        or shebang_path.parent != environment_root / "bin"
        or shebang_path.resolve(strict=True) != runtime_python.resolve(strict=True)
        or executable_bytes != _canonical_aros_wrapper(shebang_path)
    ):
        raise VerificationError("aros executable wrapper or environment differs")
    return package


def _tools(section: dict[str, object]) -> list[dict[str, object]]:
    value = section.get("tool_uses")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise VerificationError("tool uses are invalid")
    return value  # type: ignore[return-value]


def _tool(item: dict[str, object]) -> tuple[str, dict[str, object]]:
    name = item.get("name")
    tool_input = item.get("input")
    if not isinstance(name, str) or not isinstance(tool_input, dict):
        raise VerificationError("tool use is invalid")
    return name, tool_input


def _semantic_writes(
    task_id: str,
    candidate_commit: str,
    collected_ref: str,
    eval_id: str,
    receipt_ref: str,
) -> dict[str, str]:
    return {
        "questions/Q-0001/question.md": (
            "---\nid: Q-0001\nstatus: resolved\n---\n# Question\n\n"
            "Does the deterministic candidate produce the expected valid measurement?\n\n"
            "## Current best answer\n\nYes under the fixed commissioning apparatus.\n\n"
            "## Current uncertainty\n\nExternal validity is not tested.\n\n"
            "## Resolution criterion\n\nOne valid metric of 1.0.\n\n"
            "## Stop / pivot criterion\n\nStop after the declared evaluator succeeds.\n\n"
            "## Expected information gain\n\nThe commissioned question is resolved.\n"
        ),
        "model/CURRENT.md": (
            "# Current Model\n\nThe candidate emitted `success` and evaluator "
            f"`{eval_id}` measured 1.0.\n\n## Current uncertainty\n\n"
            "The fixture does not establish external validity.\n"
        ),
        "ideas/I-E2E.md": (
            "---\nid: I-E2E\n---\n# Idea\n\n## Result\n\n"
            f"Task `{task_id}` produced candidate `{candidate_commit}`; "
            f"evaluation `{eval_id}` returned a valid metric of 1.0.\n"
        ),
        "knowledge/claims/C-0001.md": (
            "---\nid: C-0001\n---\n# Claim\n\n## Statement and scope\n\n"
            "The deterministic candidate passes the fixed simple-loop evaluator.\n\n"
            f"## Evidence\n\n- `{receipt_ref}` — valid metric 1.0 for "
            f"`{candidate_commit}`.\n\n## Counterevidence\n\n"
            "None within this fixture.\n"
        ),
        "memory/NOW.md": (
            "# Current State\n\n## Result\n\n"
            f"Task `{task_id}` return `{collected_ref}` and evaluation "
            f"`{receipt_ref}` resolved Q-0001 within fixture scope.\n\n"
            "## Current uncertainty\n\nExternal validity remains unknown.\n"
        ),
    }


def _primary_writes(
    tools: list[dict[str, object]],
    *,
    task_id: str,
    candidate_commit: str,
    eval_id: str,
    collected_ref: str,
    receipt_ref: str,
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    cursor = 0
    preregistration_writes: dict[str, bytes] = {}
    semantic_writes = _semantic_writes(
        task_id,
        candidate_commit,
        collected_ref,
        eval_id,
        receipt_ref,
    )
    writes: dict[str, bytes] = {}

    def require(name: str, action: str | None = None) -> dict[str, object]:
        nonlocal cursor
        if cursor >= len(tools):
            raise VerificationError(f"missing tool {name}")
        actual_name, tool_input = _tool(tools[cursor])
        cursor += 1
        if actual_name != name or (
            action is not None and tool_input.get("action") != action
        ):
            raise VerificationError(f"expected {name}/{action}, got {actual_name}")
        return tool_input

    if require("Attention") != {}:
        raise VerificationError("initial Attention input differs")
    for path in ("model/CURRENT.md", "ideas/I-E2E.md"):
        value = require("Write")
        expected = {"file_path": path, "content": _PREREGISTRATION_WRITES[path]}
        if value != expected:
            raise VerificationError("preregistration Write differs")
        preregistration_writes[path] = _PREREGISTRATION_WRITES[path].encode()
    prereg = require("Checkpoint")
    if prereg != {
        "message": "Preregister deterministic mechanism and test.",
        "paths": ["ideas/I-E2E.md", "model/CURRENT.md"],
    }:
        raise VerificationError("preregistration checkpoint input differs")
    task_create = require("Task", "create")
    if task_create != {
        "action": "create",
        "objective": "Produce the deterministic success candidate.",
        "mode": "write",
        "adapter_argv": ["python3", "commissioning/simple_loop/task_adapter.py"],
        "capabilities": {"network": False, "shell": True},
        "deliverables": ["candidate-mode.txt"],
        "acceptance": ["candidate-mode.txt equals success"],
        "timeout_seconds": 120,
        "idempotency_key": "simple-loop-task",
    }:
        raise VerificationError("Task.create input differs")
    for action in ("start", "status"):
        if require("Task", action) != {"action": action, "task_id": task_id}:
            raise VerificationError(f"Task.{action} identity differs")
    while cursor < len(tools):
        name, value = _tool(tools[cursor])
        if name != "Task" or value.get("action") != "status":
            break
        cursor += 1
        if value != {"action": "status", "task_id": task_id}:
            raise VerificationError("Task.status identity differs")
    if require("Task", "collect") != {"action": "collect", "task_id": task_id}:
        raise VerificationError("Task.collect identity differs")
    if require("Eval", "run") != {
        "action": "run",
        "evaluator_id": "simple-loop",
        "version": "1",
        "candidate_commit": candidate_commit,
        "idempotency_key": "simple-loop-eval",
    }:
        raise VerificationError("Eval.run input differs")
    if require("Attention") != {}:
        raise VerificationError("post-evidence Attention input differs")
    for path in (
        "questions/Q-0001/question.md",
        "model/CURRENT.md",
        "ideas/I-E2E.md",
        "knowledge/claims/C-0001.md",
        "memory/NOW.md",
    ):
        value = require("Write")
        expected_content = semantic_writes[path]
        if value != {"file_path": path, "content": expected_content}:
            raise VerificationError("final semantic Write differs")
        writes[path] = expected_content.encode()
    final = require("Checkpoint")
    if final != {
        "message": "Interpret deterministic Task return and measurement.",
        "paths": _SEMANTIC_PATHS,
    }:
        raise VerificationError("final checkpoint input differs")
    if cursor != len(tools):
        raise VerificationError("unexpected extra tool use")
    return preregistration_writes, writes


def verify(evidence_path: Path) -> dict[str, object]:
    evidence = _json(evidence_path)
    if evidence.get("schema_version") != 1:
        raise VerificationError("evidence version differs")
    if evidence.get("enforcement_class") != "cooperative":
        raise VerificationError("boundary must be cooperative")
    package = _verify_product(evidence, evidence_path.absolute())
    project_value = evidence.get("project")
    if not isinstance(project_value, str):
        raise VerificationError("project is missing")
    project = Path(project_value).resolve(strict=True)
    task = evidence.get("task")
    evaluation = evidence.get("eval")
    checkpoint = evidence.get("checkpoint")
    agent = evidence.get("agent")
    restart = evidence.get("restart")
    if not all(isinstance(item, dict) for item in (task, evaluation, checkpoint, agent, restart)):
        raise VerificationError("evidence sections are incomplete")
    assert isinstance(task, dict)
    assert isinstance(evaluation, dict)
    assert isinstance(checkpoint, dict)
    assert isinstance(agent, dict)
    assert isinstance(restart, dict)
    task_run_fields = {
        "run_id",
        "run_manifest_ref",
        "run_manifest_sha256",
        "run_final_ref",
        "run_final_sha256",
    }
    missing_task_run = sorted(task_run_fields - set(task))
    if missing_task_run:
        raise VerificationError(
            "Task Run evidence is incomplete: " + ", ".join(missing_task_run)
        )
    task_id = task.get("task_id")
    run_id = task.get("run_id")
    eval_id = evaluation.get("eval_id")
    eval_run_id = evaluation.get("run_id")
    if not isinstance(task_id, str) or _TASK_ID.fullmatch(task_id) is None:
        raise VerificationError("Task identity is invalid")
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise VerificationError("Task Run evidence run_id is invalid")
    if not isinstance(eval_id, str) or _EVAL_ID.fullmatch(eval_id) is None:
        raise VerificationError("Eval identity is invalid")
    if not isinstance(eval_run_id, str) or _RUN_ID.fullmatch(eval_run_id) is None:
        raise VerificationError("Eval Run identity is invalid")
    collected_ref = task.get("collected_ref")
    manifest_ref = task.get("run_manifest_ref")
    final_ref = task.get("run_final_ref")
    receipt_ref = evaluation.get("receipt_ref")
    expected_collected_ref = f"tasks/{task_id}/collected.json"
    expected_manifest_ref = f"runs/{run_id}/manifest.json"
    expected_final_ref = f"runs/{run_id}/final.json"
    expected_receipt_ref = f"eval/evaluations/{eval_id}/receipt.json"
    if collected_ref != expected_collected_ref or receipt_ref != expected_receipt_ref:
        raise VerificationError("return refs are invalid")
    if manifest_ref != expected_manifest_ref or final_ref != expected_final_ref:
        raise VerificationError("Task Run identity refs are invalid")
    candidate_commit = task.get("child_commit")
    if not isinstance(candidate_commit, str) or _COMMIT.fullmatch(candidate_commit) is None:
        raise VerificationError("Task candidate identity is invalid")
    assert isinstance(collected_ref, str)
    assert isinstance(receipt_ref, str)
    if agent.get("class") != "arbor.core.agent.Agent":
        raise VerificationError("primary was not the native Agent")
    if agent.get("destroyed_before_restart") is not True:
        raise VerificationError("primary was not destroyed before restart")
    if agent.get("stop_reason") != "finished" or restart.get("stop_reason") != "finished":
        raise VerificationError("an Agent did not finish")
    if restart.get("initial_message_count") != 0:
        raise VerificationError("restart reused messages")
    if agent.get("instance") == restart.get("agent_instance"):
        raise VerificationError("restart reused Agent identity")
    if agent.get("provider_instance") == restart.get("provider_instance"):
        raise VerificationError("restart reused provider identity")
    preregistration_writes, writes = _primary_writes(
        _tools(agent),
        task_id=task_id,
        candidate_commit=candidate_commit,
        eval_id=eval_id,
        collected_ref=collected_ref,
        receipt_ref=receipt_ref,
    )
    restart_tools = [_tool(item) for item in _tools(restart)]
    if restart_tools != [("Attention", {})]:
        raise VerificationError("restart did not perform exactly one Attention")

    final_commit = checkpoint.get("final_commit")
    final_parent = checkpoint.get("final_parent")
    prereg_commit = checkpoint.get("preregistration_commit")
    if any(not isinstance(item, str) or _COMMIT.fullmatch(item) is None for item in (final_commit, final_parent, prereg_commit)):
        raise VerificationError("checkpoint identity is invalid")
    assert isinstance(final_commit, str)
    assert isinstance(final_parent, str)
    assert isinstance(prereg_commit, str)
    if _git_text(project, "rev-parse", "HEAD") != final_commit:
        raise VerificationError("HEAD differs from final checkpoint")
    if _git_text(project, "rev-parse", "HEAD^") != final_parent:
        raise VerificationError("final parent differs")
    if _git_text(project, "status", "--porcelain=v1", "--untracked-files=all"):
        raise VerificationError("project is dirty after final checkpoint")
    if sorted(_git_text(project, "diff-tree", "--no-commit-id", "--name-only", "-r", final_commit).splitlines()) != _SEMANTIC_PATHS:
        raise VerificationError("final changed paths differ")
    if sorted(_git_text(project, "diff-tree", "--no-commit-id", "--name-only", "-r", prereg_commit).splitlines()) != ["ideas/I-E2E.md", "model/CURRENT.md"]:
        raise VerificationError("preregistration changed paths differ")
    for path, expected_blob in preregistration_writes.items():
        if _git(project, "show", f"{prereg_commit}:{path}") != expected_blob:
            raise VerificationError(f"preregistration commit blob differs: {path}")
    for path, expected_blob in writes.items():
        if _git(project, "show", f"{final_commit}:{path}") != expected_blob:
            raise VerificationError(f"final Git blob differs: {path}")

    assert isinstance(collected_ref, str)
    assert isinstance(manifest_ref, str)
    assert isinstance(final_ref, str)
    assert isinstance(receipt_ref, str)
    collected = _committed_object(project, final_commit, collected_ref)
    manifest = _committed_object(project, final_commit, manifest_ref)
    final = _committed_object(project, final_commit, final_ref)
    receipt = _committed_object(project, final_commit, receipt_ref)
    if _record_hash(collected, "collected_sha256") != task.get("collected_sha256"):
        raise VerificationError("Task hash differs")
    manifest_sha256 = _record_hash(manifest, "manifest_sha256")
    if manifest_sha256 != task.get("run_manifest_sha256"):
        raise VerificationError("Task Run manifest_sha256 differs")
    final_sha256 = _json_hash(final)
    if final_sha256 != task.get("run_final_sha256"):
        raise VerificationError("Task Run final hash differs")
    if _record_hash(receipt, "receipt_sha256") != evaluation.get("receipt_sha256"):
        raise VerificationError("Eval hash differs")
    receipt_eval_id = receipt.get("eval_id")
    if (
        not isinstance(receipt_eval_id, str)
        or _EVAL_ID.fullmatch(receipt_eval_id) is None
        or receipt_eval_id != eval_id
        or receipt.get("run_id") != eval_run_id
    ):
        raise VerificationError("Eval receipt identity differs")
    for field, expected in (
        ("task_id", task_id),
        ("run_id", run_id),
        ("run_manifest_ref", manifest_ref),
        ("run_manifest_sha256", manifest_sha256),
        ("run_final_ref", final_ref),
        ("run_final_sha256", final_sha256),
        ("child_commit", task.get("child_commit")),
        ("return_commit", task.get("return_commit")),
    ):
        if collected.get(field) != expected:
            raise VerificationError(f"Task collection {field} differs")
    if manifest.get("run_id") != run_id:
        raise VerificationError("Task Run manifest identity differs")
    argv = manifest.get("argv")
    expected_argv_tail = [
        "-B",
        "-m",
        "arbor.aros.task_adapter",
        "--workspace",
        str(project),
        "--task-id",
        task_id,
    ]
    if (
        not isinstance(argv, list)
        or len(argv) != len(expected_argv_tail) + 1
        or not isinstance(argv[0], str)
        or not argv[0]
        or argv[1:] != expected_argv_tail
    ):
        raise VerificationError("Task Run argv differs")
    if manifest.get("security_profile") != "trusted-local":
        raise VerificationError("Task Run profile is not trusted-local")
    if (
        final.get("run_id") != run_id
        or final.get("manifest_sha256") != manifest_sha256
    ):
        raise VerificationError("Task Run final manifest lineage differs")
    if final.get("state") != "completed" or collected.get("final_state") != "completed":
        raise VerificationError("Task Run final is not completed")
    base_commit = collected.get("base_commit")
    child_commit = collected.get("child_commit")
    return_commit = collected.get("return_commit")
    if any(
        not isinstance(item, str) or _COMMIT.fullmatch(item) is None
        for item in (base_commit, child_commit, return_commit)
    ):
        raise VerificationError("Task B-C-R lineage identity is invalid")
    assert isinstance(base_commit, str)
    assert isinstance(child_commit, str)
    assert isinstance(return_commit, str)
    if (
        _git_text(project, "rev-parse", f"{child_commit}^") != base_commit
        or _git_text(project, "rev-parse", f"{return_commit}^") != child_commit
    ):
        raise VerificationError("Task B-C-R lineage differs")
    if (
        collected.get("child_commit") != receipt.get("candidate_commit")
        or evaluation.get("candidate_commit") != receipt.get("candidate_commit")
    ):
        raise VerificationError("Task and Eval candidates differ")
    if receipt.get("measurement_state") != "valid" or receipt.get("metric") != 1.0:
        raise VerificationError("measurement differs")
    for ref in (manifest_ref, final_ref, collected_ref, receipt_ref):
        if _git(project, "show", f"{final_parent}:{ref}") != _git(
            project,
            "show",
            f"{final_commit}:{ref}",
        ):
            raise VerificationError(f"record was not committed before final checkpoint: {ref}")

    message = _git_text(project, "log", "-1", "--format=%B", final_commit)
    trailers = sorted(
        line.split(": ", 1)[1]
        for line in message.splitlines()
        if line.startswith("AROS-Observed: ")
    )
    expected_refs = sorted([collected_ref, final_ref, receipt_ref])
    if trailers != expected_refs:
        raise VerificationError("automatic observed trailers differ")
    packet = restart.get("packet")
    if not isinstance(packet, dict) or packet.get("unread_returns") != []:
        raise VerificationError("restart has unread returns")
    recent = packet.get("recent_evidence_delta")
    if not isinstance(recent, list) or not recent or not isinstance(recent[0], dict):
        raise VerificationError("restart lacks recent checkpoint")
    if recent[0].get("commit") != final_commit:
        raise VerificationError("restart recent commit differs")
    if recent[0].get("observed_refs") != expected_refs:
        raise VerificationError("restart observed refs differ")
    if recent[0].get("paths") != _SEMANTIC_PATHS:
        raise VerificationError("restart recent paths differ")

    tree_paths = _git_text(project, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    forbidden_files = {"proposal" + ".json", "admission" + ".json"}
    if any(path.startswith("transitions/") or Path(path).name in forbidden_files for path in tree_paths):
        raise VerificationError("removed research-control artifact exists")
    if len(list((project / "tasks").glob("TASK-*/collected.json"))) != 1:
        raise VerificationError("Task count differs")
    if len(list((project / "eval/evaluations").glob("EVAL-*/receipt.json"))) != 1:
        raise VerificationError("Eval count differs")
    removed_modules = [
        "transitions.py",
        "transition_" + "index.py",
        "checkpoint_" + "bridge.py",
        "operational.py",
        "research_" + "tool.py",
    ]
    if any((package / name).exists() for name in removed_modules):
        raise VerificationError("removed module exists in installed package")
    if (package / "task_runner.py").exists():
        raise VerificationError("removed task_runner.py exists in installed package")
    if not (package / "task_adapter.py").is_file():
        raise VerificationError("task_adapter.py is missing from installed package")
    if not (package / "task_run.py").is_file():
        raise VerificationError("task_run.py is missing from installed package")
    commands = evidence.get("commands")
    if not isinstance(commands, list) or not commands:
        raise VerificationError("command receipts are missing")
    if any(not isinstance(item, dict) or item.get("returncode") != 0 for item in commands):
        raise VerificationError("a commissioning command failed")
    return {
        "schema_version": 1,
        "state": "verified",
        "enforcement_class": "cooperative",
        "commit": final_commit,
        "task_id": task.get("task_id"),
        "eval_id": evaluation.get("eval_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.evidence)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
