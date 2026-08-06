# AROS Native Start and Local Research Intake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `aros start` the sole native entry that initializes a canonical Question-centered KnowledgeBank from a local Git workspace and local PDF/Markdown, then starts the existing Principal Agent.

**Architecture:** Add one pure `arbor.aros.intake` service for local source validation and exact Git bootstrap, plus one `arbor.cli.aros_start` presentation module using existing Typer/Rich/prompt-toolkit patterns. Compose them in the current start command, then delete public `aros init` and the `arbor aros` forwarding shim; retain existing Agent/Research/Task/Run/Eval services unchanged.

**Tech Stack:** Python 3.11+, Typer, Rich, prompt-toolkit, pypdf, Git CLI, pytest, existing Arbor Agent and AROS kernel.

---

## File map

- Create `src/aros/intake.py`: local material parsing, strict workspace bootstrap, selected-path Git commit, factual receipt.
- Create `src/cli/aros_start.py`: TTY/non-TTY input collection and Rich boot/boundary rendering only.
- Modify `src/cli/commands/aros_cmd.py`: compose intake before canonical Attention and Principal creation; remove `init`.
- Modify `src/aros/workspace.py`: remove obsolete initializer and point uninitialized boot errors to `aros start`.
- Modify `src/cli/app.py`: remove `arbor aros` mount/warning/known command.
- Modify `scripts/check_aros_legacy_freeze.py` and `tests/test_aros_architecture_boundary.py`: register the one new direct CLI adapter and delete the obsolete sunset exception.
- Create `tests/test_aros_intake.py` and `tests/test_aros_start_ui.py`; modify existing workspace/CLI/public-entry tests.
- Create `commissioning/native_start/provider.py`, `scripts/commission_aros_native_start.py`, `scripts/verify_aros_native_start_commissioning.py`, and `tests/test_aros_native_start_commissioning_scripts.py`.
- Modify `scripts/commission_aros_principal_loop.py` to use the permanent intake service instead of removed `aros init`.
- Modify current AROS docs and receipts after clean-wheel commissioning.

### Task 1: Strict local PDF/Markdown observations

**Files:**
- Create: `src/aros/intake.py`
- Create: `tests/test_aros_intake.py`

- [ ] **Step 1: Write RED tests for exact local sources**

Create tests for one Markdown and one generated PDF using pypdf's maintained writer/reader API. The desired public type and function are:

```python
from arbor.aros.intake import LocalMaterial, inspect_local_materials


def test_inspect_markdown_preserves_exact_bytes_and_provenance(tmp_path: Path) -> None:
    source = tmp_path / "paper.md"
    raw = b"# Finding\n\nObserved mechanism.\n"
    source.write_bytes(raw)

    materials = inspect_local_materials([source])

    assert materials == (
        LocalMaterial(
            source_id="SRC-" + hashlib.sha256(raw).hexdigest()[:16],
            kind="markdown",
            original_name="paper.md",
            content_sha256=hashlib.sha256(raw).hexdigest(),
            content=raw,
            extracted="# Finding\n\nObserved mechanism.\n",
            provided_path=str(source),
        ),
    )
```

Additional RED cases require:

- identical bytes at two paths deduplicate to one material;
- unsupported suffix, symlink, non-file, invalid UTF-8 Markdown, empty PDF
  extraction, and file size over `MAX_LOCAL_MATERIAL_BYTES` raise `IntakeError`;
- all inputs are inspected before the caller mutates a workspace.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_intake.py -k material
```

Expected: collection/import failure because `arbor.aros.intake` does not exist.

- [ ] **Step 3: Implement the minimal immutable observation**

Implement only:

```python
MAX_LOCAL_MATERIAL_BYTES = 64 * 1024 * 1024


class IntakeError(ValueError):
    pass


@dataclass(frozen=True)
class LocalMaterial:
    source_id: str
    kind: str
    original_name: str
    content_sha256: str
    content: bytes
    extracted: str
    provided_path: str


def inspect_local_materials(paths: Sequence[str | Path]) -> tuple[LocalMaterial, ...]:
    materials: list[LocalMaterial] = []
    seen: set[str] = set()
    for supplied in paths:
        path = Path(supplied).expanduser()
        metadata = path.lstat()
        if path.is_symlink() or not path.is_file():
            raise IntakeError(f"material must be a plain file: {path}")
        if metadata.st_size > MAX_LOCAL_MATERIAL_BYTES:
            raise IntakeError(f"material exceeds byte limit: {path}")
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen:
            continue
        suffix = path.suffix.lower()
        if suffix == ".md":
            try:
                extracted = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise IntakeError(f"Markdown is not UTF-8: {path}") from error
            kind = "markdown"
        elif suffix == ".pdf":
            try:
                reader = PdfReader(io.BytesIO(content))
                extracted = "\n\n".join(
                    page.extract_text() or "" for page in reader.pages
                ).strip()
            except Exception as error:
                raise IntakeError(f"PDF extraction failed: {path}") from error
            if not extracted:
                raise IntakeError(f"PDF contains no extracted text: {path}")
            kind = "pdf"
        else:
            raise IntakeError(f"unsupported material type: {path}")
        seen.add(digest)
        materials.append(
            LocalMaterial(
                source_id=f"SRC-{digest[:16]}",
                kind=kind,
                original_name=path.name,
                content_sha256=digest,
                content=content,
                extracted=extracted,
                provided_path=str(path),
            )
        )
    return tuple(materials)
```

The implementation resolves a plain non-symlink file, checks size before read,
reads once, hashes exact bytes, decodes Markdown strictly, and extracts PDF text
with `PdfReader(io.BytesIO(raw))`. It catches pypdf parse/encryption errors and
raises one factual `IntakeError`; it does not OCR, download, or fall back.
Deduplication preserves first-input order.

- [ ] **Step 4: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_intake.py -k material
/workspace/Arbor/.venv/bin/ruff check src/aros/intake.py tests/test_aros_intake.py
git diff --check
git add src/aros/intake.py tests/test_aros_intake.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'feat(aros): inspect local research materials'
```

### Task 2: Canonical Question-centered KnowledgeBank bootstrap

**Files:**
- Modify: `src/aros/intake.py`
- Modify: `tests/test_aros_intake.py`

- [ ] **Step 1: Write RED tests for existing and new workspaces**

The desired function is:

```python
receipt = initialize_knowledge_bank(
    workspace=tmp_path / "topic_KB",
    question="What mechanism explains the observed result?",
    material_paths=[paper],
)
```

Assert:

- absent workspace is created as `main` Git repository;
- existing workspace must be a clean attached Git root;
- Q-0001 contains the exact question and every required heading;
- FRONTIER focuses Q-0001;
- AROS.md, NOW, and CURRENT state no host-invented answer/model/idea;
- source original bytes, extracted.md, metadata.json, and manifest hashes match;
- HEAD contains exactly the selected bootstrap additions with commit author
  `AROS Intake <aros-intake@local.invalid>`;
- an existing AGENTS.md remains byte-identical and unmodified;
- an existing plain `.gitignore` only gains missing `/.aros/` and
  `/.worktree/` lines;
- any AROS-owned collision, dirty/staged repository, detached HEAD, nested Git
  path, symlinked root, invalid question, or invalid material fails before any
  AROS path appears.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_intake.py -k knowledge_bank
```

Expected: failure because `initialize_knowledge_bank` is absent.

- [ ] **Step 3: Implement selected-path bootstrap without a new framework**

Add:

```python
def initialize_knowledge_bank(
    workspace: str | Path,
    question: str,
    material_paths: Sequence[str | Path] = (),
) -> dict[str, object]:
    materials = inspect_local_materials(material_paths)
    # validate or create exact Git root
    # require AROS-owned paths absent and clean ordinary index
    # write exact templates and source records
    # git add -- only created/updated paths
    # git -c user.name=AROS Intake -c user.email=aros-intake@local.invalid commit
    # return factual receipt
```

Use one private `_git` subprocess helper with argument lists and captured output.
No shell string, reset, clean, checkout, merge, or all-files add is allowed.
Create `AGENTS.md` only when absent. Append ignore entries without reformatting
existing content. Metadata and manifest use canonical sorted UTF-8 JSON.

- [ ] **Step 4: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_intake.py
/workspace/Arbor/.venv/bin/ruff check src/aros/intake.py tests/test_aros_intake.py
git diff --check
git add src/aros/intake.py tests/test_aros_intake.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'feat(aros): bootstrap question centered knowledge bank'
```

### Task 3: Native `aros start` intake and boot transition

**Files:**
- Create: `src/cli/aros_start.py`
- Create: `tests/test_aros_start_ui.py`
- Modify: `src/cli/commands/aros_cmd.py`
- Modify: `tests/test_aros_cli.py`
- Modify: `tests/test_aros_public_entry.py`

- [ ] **Step 1: Write RED UI tests**

Define one small presentation input:

```python
@dataclass(frozen=True)
class StartIntake:
    workspace: Path
    question: str
    materials: tuple[Path, ...]
```

Test `collect_start_intake` with injected `interactive` and `prompt` callables:

- non-TTY with missing question raises `ValueError("--question is required")`;
- supplied flags never prompt;
- TTY asks Workspace, Key Research Question, then comma-separated local
  materials in that order;
- empty question or workspace fails;
- `render_start_transition` uses the existing shared Rich Console and contains
  `AROS`, Question, workspace, authority, max turns, and shell boundary without
  sleeping or reading state.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q tests/test_aros_start_ui.py
```

Expected: import failure because `arbor.cli.aros_start` is absent.

- [ ] **Step 3: Implement the presentation module**

Use only Typer prompt semantics and Rich `Console`, `Panel`, `Table`, and
`Spinner/Live` patterns already used under `src/cli`. The module imports no
AROS workspace service, Git helper, provider, Agent, Task, Run, Eval, MCP, or
Skill registry.

- [ ] **Step 4: Write RED composition tests for `start`**

Patch `status_workspace`, `collect_start_intake`,
`initialize_knowledge_bank`, `boot_workspace`, provider creation, and Principal
construction. Assert:

```text
uninitialized -> collect -> initialize -> boot(context) -> provider -> Agent.run
initialized   -> boot(context) -> provider -> Agent.run (no intake)
```

Add `--question` and repeatable `--material` CLI assertions. Supplying intake
arguments to an initialized workspace must fail rather than merge state.
Require cooperative authority context to be constructed before boot and passed
to both boot and ResearchTool.

- [ ] **Step 5: Run composition RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_cli.py tests/test_aros_public_entry.py -k start
```

Expected: failure because current start boots uninitialized state directly and
has no question/material options.

- [ ] **Step 6: Implement the minimal composition root**

In `start_command`:

1. build host authority/budget context;
2. call `status_workspace(root)`;
3. when uninitialized, collect exact input and call
   `initialize_knowledge_bank`;
4. render the transition only in interactive mode;
5. call `boot_workspace(root, context=attention_context)`;
6. create provider and native Principal exactly as today.

Do not add a Start service class, configuration object, alternate provider
path, or second Agent loop.

- [ ] **Step 7: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_start_ui.py tests/test_aros_cli.py tests/test_aros_public_entry.py \
  tests/test_aros_principal.py tests/test_aros_research_tool.py
/workspace/Arbor/.venv/bin/ruff check \
  src/cli/aros_start.py src/cli/commands/aros_cmd.py \
  tests/test_aros_start_ui.py tests/test_aros_cli.py tests/test_aros_public_entry.py
git diff --check
git add src/cli/aros_start.py src/cli/commands/aros_cmd.py \
  tests/test_aros_start_ui.py tests/test_aros_cli.py tests/test_aros_public_entry.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'feat(aros): start from native research intake'
```

### Task 4: Delete replaced public entry paths and admit one CLI module

**Files:**
- Modify: `src/cli/commands/aros_cmd.py`
- Modify: `src/aros/workspace.py`
- Modify: `src/cli/app.py`
- Modify: `scripts/check_aros_legacy_freeze.py`
- Modify: `tests/test_aros_cli.py`
- Modify: `tests/test_aros_public_entry.py`
- Modify: `tests/test_aros_workspace.py`
- Modify: `tests/test_aros_architecture_boundary.py`

- [ ] **Step 1: Write RED deletion tests**

Require:

- direct `aros --help` contains `start` but not `init`;
- invoking `aros init` exits as unknown command;
- `arbor --help` and `_KNOWN_COMMANDS` contain no `aros`;
- `arbor aros --help` is unknown and emits no forwarding warning;
- `boot_workspace` tells uninitialized users to run `aros start`;
- `init_workspace` has no public CLI caller or direct-entry documentation;
- architecture boundary includes `arbor.cli.aros_start` as the single new
  direct adapter;
- legacy freeze checker has no `AROS_RETIREMENT_GATE_E4*` constants or special
  blob path and still rejects unrelated growth in `src/cli/app.py`;
- the approved change to legacy `src/cli/app.py` is deletion-only.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_public_entry.py tests/test_aros_cli.py \
  tests/test_aros_workspace.py tests/test_aros_architecture_boundary.py
```

Expected: failures for still-present `init`, forwarding mount, warning, and
sunset exception.

- [ ] **Step 3: Delete obsolete code directly**

Remove:

- `init_command` and its `init_workspace` import from the public CLI;
- `aros_app` import/mount, `"aros"` known command, `_warn_aros_forward`, and its
  call from legacy `src/cli/app.py`;
- the E4 constants and special-case hash allowance.

Add `src/cli/aros_start.py` to `GROWTH_FILES`, architecture module allowlist,
boundary paths, dynamic imports, and package/wheel assertions. Do not allow the
whole `src/cli/` tree to grow.

- [ ] **Step 4: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_public_entry.py tests/test_aros_cli.py \
  tests/test_aros_workspace.py tests/test_aros_architecture_boundary.py
/workspace/Arbor/.venv/bin/ruff check \
  src/aros/workspace.py src/cli/app.py src/cli/aros_start.py \
  src/cli/commands/aros_cmd.py scripts/check_aros_legacy_freeze.py \
  tests/test_aros_public_entry.py tests/test_aros_cli.py \
  tests/test_aros_workspace.py tests/test_aros_architecture_boundary.py
git diff --check
git add src/aros/workspace.py src/cli/app.py src/cli/aros_start.py \
  src/cli/commands/aros_cmd.py scripts/check_aros_legacy_freeze.py \
  tests/test_aros_public_entry.py tests/test_aros_cli.py \
  tests/test_aros_workspace.py tests/test_aros_architecture_boundary.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'refactor(aros): remove replaced initialization routes'
```

### Task 5: Replace commissioning setup and prove clean-wheel native start

**Files:**
- Create: `commissioning/native_start/provider.py`
- Create: `scripts/commission_aros_native_start.py`
- Create: `scripts/verify_aros_native_start_commissioning.py`
- Create: `tests/test_aros_native_start_commissioning_scripts.py`
- Modify: `scripts/commission_aros_principal_loop.py`
- Modify: `tests/test_aros_principal_loop_commissioning_scripts.py`

- [ ] **Step 1: Write RED fixture/verifier tests**

The commissioning provider is reality-blind like the existing Principal-loop
fixture: it imports only `arbor.core.llm.base`, emits `Read` for
`questions/Q-0001/question.md` and the extracted source, then final text. Tests
reject filesystem, process, AROS service, or network imports.

The independent verifier must reject:

- missing initialization commit or wrong author;
- Question bytes not equal the supplied human text;
- source original/hash/extracted/metadata mismatch;
- host-invented Idea or Claim in the bootstrap commit;
- Agent trace without both Reads;
- nonzero initial restart messages;
- present `aros init` or `arbor aros` command surfaces;
- a driver-authored fake Agent trace.

- [ ] **Step 2: Run RED**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_native_start_commissioning_scripts.py
```

Expected: failure because fixture, driver, and verifier are absent.

- [ ] **Step 3: Implement clean-wheel command composition**

The driver runs under the built wheel interpreter, imports the wheel's Typer
`aros_app`, patches only the provider factory to the deterministic
`LLMProvider`, wraps the real `build_principal_agent` to capture the Agent, and
invokes the exact public command through `typer.testing.CliRunner`:

```text
start --cwd NEW --question EXACT --material LOCAL_MD --cooperative-human-direct
```

It records command arguments, intake receipt, bootstrap commit, Agent class,
tool uses, message hash, source records, and a second zero-message initialized
restart. It never calls intake or writes KB semantic paths directly.

- [ ] **Step 4: Replace the old Principal-loop setup**

In `commission_aros_principal_loop.py`, replace the removed `aros init`
subprocess plus manual initial commit with one call to the permanent
`initialize_knowledge_bank` service. Keep the Task/Eval/assimilation Agent loop
and independent verifier unchanged.

- [ ] **Step 5: Verify GREEN and commit**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_native_start_commissioning_scripts.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
/workspace/Arbor/.venv/bin/ruff check \
  commissioning/native_start scripts/commission_aros_native_start.py \
  scripts/verify_aros_native_start_commissioning.py \
  scripts/commission_aros_principal_loop.py \
  tests/test_aros_native_start_commissioning_scripts.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git diff --check
git add commissioning/native_start scripts/commission_aros_native_start.py \
  scripts/verify_aros_native_start_commissioning.py \
  scripts/commission_aros_principal_loop.py \
  tests/test_aros_native_start_commissioning_scripts.py \
  tests/test_aros_principal_loop_commissioning_scripts.py
git -c user.name='AROS Agent' -c user.email='aros-agent@example.invalid' \
  commit -m 'test(aros): commission native research start'
```

### Task 6: Recommission, document, and run full gates

**Files:**
- Create: `docs/analysis/aros-native-start-smoke.md`
- Modify: `docs/analysis/aros-principal-loop-core-smoke.md`
- Modify: `docs/aros/README.md`
- Modify: `docs/architecture/aros-implementation-baseline.md`
- Modify: `docs/document_registry.json`
- Modify: `memory/NOW.md`

- [ ] **Step 1: Build and normally install a clean wheel**

Use an absent `.worktree/commissioning/aros-native-start-build` root, `pip
wheel --no-deps`, a normal venv, then dependency-resolving `pip install <wheel>`.
Never use `uv`, editable install, `.pth`, or PYTHONPATH fallback.

- [ ] **Step 2: Run both retained commissioning commands**

Run native-start commissioning at a new absent runtime, then rerun the updated
Principal-loop commissioning at another absent runtime. Invoke both independent
verifiers separately. Expected: `state=verified`, direct `aros` only, and
explicit `enforcement_class=cooperative` where authority applies.

- [ ] **Step 3: Replace current documentation**

Record exact source/wheel hashes, Question/source bytes, initialization commit,
Agent tool sequence, restart packet/message state, deleted commands, Task/Eval
lineage, and limitations. Register the new smoke as current and promote this
design to current. Remove claims of `aros init`, `arbor aros`, migration
adapters, or compatibility transition. Delete superseded local commissioning
roots only after both new verifiers pass.

- [ ] **Step 4: Run focused gates**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q \
  tests/test_aros_intake.py tests/test_aros_start_ui.py \
  tests/test_aros_native_start_commissioning_scripts.py \
  tests/test_aros_principal_loop_commissioning_scripts.py \
  tests/test_aros_public_entry.py tests/test_aros_cli.py \
  tests/test_aros_workspace.py tests/test_aros_architecture_boundary.py \
  tests/test_document_registry.py
```

- [ ] **Step 5: Run full repository verification**

```bash
/workspace/Arbor/.venv/bin/python -m pytest -q
/workspace/Arbor/.venv/bin/ruff check \
  src/aros src/cli/aros_app.py src/cli/aros_start.py \
  src/cli/commands/aros_cmd.py scripts/check_aros_legacy_freeze.py \
  commissioning/native_start scripts/commission_aros_native_start.py \
  scripts/verify_aros_native_start_commissioning.py \
  scripts/commission_aros_principal_loop.py \
  scripts/verify_aros_principal_loop_commissioning.py \
  tests/test_aros_intake.py tests/test_aros_start_ui.py \
  tests/test_aros_native_start_commissioning_scripts.py
git diff --check
git status --short --branch
```

Expected: all tests and Ruff exit 0, diff check has no output, worktree is clean.

- [ ] **Step 6: Audit every design acceptance item**

Check Section 12 of
`docs/superpowers/specs/2026-08-06-aros-native-start-intake-design.md` against
the retained receipts and exact Git objects. Keep the overall AROS goal active:
this slice does not prove the next real scientific child-agent loop, Source
Gateway, Skills/MCP parity, or protected authority.
