from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts" / "check_aros_legacy_freeze.py"
FROZEN_FILES = (
    "src/coordinator/main.py",
    "src/executor/main.py",
    "src/run.py",
    "src/review.py",
    "src/cli/commands/run.py",
)
ALLOWED_INTERNAL_PREFIXES = (
    "arbor.aros",
    "arbor.core",
    "arbor.cli.aros_app",
    "arbor.cli.commands.aros_cmd",
    "arbor.cli.user_config",
    "arbor._app",
)
SEMANTIC_LEGACY_PREFIXES = (
    "arbor.coordinator",  # includes the legacy IdeaTree module
    "arbor.executor",
    "arbor.run",
    "arbor.review",
    "arbor.cli.commands.run",
)
BOUNDARY_PATHS = (
    *sorted((REPO_ROOT / "src" / "aros").rglob("*.py")),
    REPO_ROOT / "src" / "cli" / "aros_app.py",
    REPO_ROOT / "src" / "cli" / "commands" / "aros_cmd.py",
)
PROJECT_MODULES = {
    ".".join(
        (
            "arbor",
            *(
                path.parent.relative_to(REPO_ROOT / "src").parts
                if path.name == "__init__.py"
                else path.relative_to(REPO_ROOT / "src").with_suffix("").parts
            ),
        )
    )
    for path in (REPO_ROOT / "src").rglob("*.py")
}
DYNAMIC_IMPORT_CALL = "<dynamic import call>"
DYNAMIC_IMPORT_REFERENCES = {"__import__", "import_module"}
DYNAMIC_EXECUTION_NAMES = {"eval", "exec", "__import__"}
DYNAMIC_IMPORT_STRINGS = {"__import__", "import_module"}
BUILTINS_NAMES = {"builtins", "__builtins__"}
BindingState = tuple[set[str], set[str], set[str], set[str], dict[str, str]]
FROZEN_HELPER = """\
def normalize_legacy(value):
    stripped = value.strip()
    lowered = stripped.lower()
    replaced = lowered.replace("-", " ")
    pieces = replaced.split()
    joined = "_".join(pieces)
    return joined
"""
PADDED_FROZEN_SOURCE = "".join(
    f"legacy_step_{number} = {number}\n" for number in range(20)
)
PADDING = "".join(f"new_step_{number} = {number}\n" for number in range(30))
INDEPENDENT_UTILITY = """\
def summarize_tokens(tokens):
    filtered = [token for token in tokens if token]
    unique = set(filtered)
    pieces = sorted(unique)
    joined = "_".join(pieces)
    width = len(joined)
    if width > 80:
        joined = joined[:80]
    return joined
"""


def test_ci_legacy_freeze_uses_immutable_pull_request_base_sha() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "github.event.pull_request.base.sha" in workflow
    assert 'git fetch --depth=1 --no-tags origin "$BASE_SHA"' in workflow
    assert '--base "$BASE_SHA"' in workflow
    assert "GITHUB_BASE_REF" not in workflow


def test_legacy_freeze_bounds_rename_and_copy_detection() -> None:
    checker = CHECKER.read_text(encoding="utf-8")

    assert '"--find-renames=50%"' in checker
    assert '"--find-copies=50%"' in checker
    assert '"-l1000"' in checker
    assert '"-l0"' not in checker


def test_aros_docs_describe_the_complete_source_growth_policy() -> None:
    for relative_path in ("README.md", "docs/aros/README.md"):
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert "all non-allowlisted legacy source paths under `src/`" in text
        assert "especially frozen" in text
        for allowed in (
            "src/aros/",
            "src/core/",
            "src/cli/aros_app.py",
            "src/cli/commands/aros_cmd.py",
            "src/cli/app.py",
        ):
            assert f"`{allowed}`" in text


def _module_and_package(path: Path) -> tuple[str, str]:
    relative = path.relative_to(REPO_ROOT / "src")
    if path.name == "__init__.py":
        module = ".".join(("arbor", *relative.parent.parts))
        return module, module
    module = ".".join(("arbor", *relative.with_suffix("").parts))
    return module, module.rpartition(".")[0]


def _is_project_module(module: str) -> bool:
    return module in PROJECT_MODULES


def _is_allowed_internal(module: str) -> bool:
    return any(
        module == allowed
        or module.startswith(f"{allowed}.")
        or allowed.startswith(f"{module}.")
        for allowed in ALLOWED_INTERNAL_PREFIXES
    )


def _resolve_import_from(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    relative = "." * node.level + (node.module or "")
    return importlib.util.resolve_name(relative, package)


def _static_imports(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, name.name) for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_import_from(node, package)
            if base:
                imports.append((node.lineno, base))
            for name in node.names:
                candidate = f"{base}.{name.name}" if base else name.name
                if _is_project_module(candidate):
                    imports.append((node.lineno, candidate))
    return imports


def _terminal_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_importlib_metadata(module: str) -> bool:
    return module == "importlib.metadata" or module.startswith("importlib.metadata.")


def _literal_string(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.NamedExpr):
        return _literal_string(node.value, bindings)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left, bindings)
        right = _literal_string(node.right, bindings)
        if left is not None and right is not None:
            return left + right
    return None


class _DynamicImportVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []
        self.dynamic_import_names = set(DYNAMIC_IMPORT_REFERENCES)
        self.execution_names = set(DYNAMIC_EXECUTION_NAMES)
        self.builtins_names = set(BUILTINS_NAMES)
        self.importlib_names = {"importlib"}
        self.string_bindings: dict[str, str] = {}
        self.class_outer_state: BindingState | None = None

    def _mark(self, node: ast.AST) -> None:
        self.violations.append((node.lineno, DYNAMIC_IMPORT_CALL))

    def _snapshot(self) -> BindingState:
        return (
            set(self.dynamic_import_names),
            set(self.execution_names),
            set(self.builtins_names),
            set(self.importlib_names),
            dict(self.string_bindings),
        )

    def _restore(self, state: BindingState) -> None:
        (
            dynamic_import_names,
            execution_names,
            builtins_names,
            importlib_names,
            string_bindings,
        ) = state
        self.dynamic_import_names = set(dynamic_import_names)
        self.execution_names = set(execution_names)
        self.builtins_names = set(builtins_names)
        self.importlib_names = set(importlib_names)
        self.string_bindings = dict(string_bindings)

    def _restore_name(self, name: str, state: BindingState) -> None:
        for names in (
            self.dynamic_import_names,
            self.execution_names,
            self.builtins_names,
            self.importlib_names,
        ):
            names.discard(name)
        self.string_bindings.pop(name, None)
        for current, previous in zip(
            (
                self.dynamic_import_names,
                self.execution_names,
                self.builtins_names,
                self.importlib_names,
            ),
            state[:4],
        ):
            if name in previous:
                current.add(name)
        if name in state[4]:
            self.string_bindings[name] = state[4][name]

    def _clear_binding(self, name: str) -> None:
        for names in (
            self.dynamic_import_names,
            self.execution_names,
            self.builtins_names,
            self.importlib_names,
        ):
            names.discard(name)
        self.string_bindings.pop(name, None)
        if name in DYNAMIC_IMPORT_REFERENCES:
            self.dynamic_import_names.add(name)
        if name in BUILTINS_NAMES:
            self.builtins_names.add(name)
        if name == "importlib":
            self.importlib_names.add(name)

    def _bind(self, name: str, value: ast.AST) -> None:
        source_name = _terminal_name(value)
        literal = _literal_string(value, self.string_bindings)
        is_dynamic_import = source_name in self.dynamic_import_names
        is_execution = isinstance(value, ast.Name) and value.id in self.execution_names
        if isinstance(value, ast.Attribute):
            is_execution = (
                value.attr in DYNAMIC_EXECUTION_NAMES
                and _terminal_name(value.value) in self.builtins_names
            )
        is_builtins = source_name in self.builtins_names
        is_importlib = source_name in self.importlib_names
        self._clear_binding(name)
        if literal is not None:
            self.string_bindings[name] = literal
        if is_dynamic_import:
            self.dynamic_import_names.add(name)
        if is_execution:
            self.execution_names.add(name)
        if is_builtins:
            self.builtins_names.add(name)
        if is_importlib:
            self.importlib_names.add(name)

    def visit_Import(self, node: ast.Import) -> None:
        for imported in node.names:
            bound = imported.asname or imported.name.split(".", 1)[0]
            self._clear_binding(bound)
            if imported.name == "importlib":
                self.importlib_names.add(bound)
                self._mark(node)
            elif imported.name.startswith("importlib."):
                if not _is_importlib_metadata(imported.name):
                    self._mark(node)
                if imported.asname is None:
                    self.importlib_names.add("importlib")
            elif imported.name == "builtins":
                self.builtins_names.add(bound)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for imported in node.names:
            bound = imported.asname or imported.name
            self._clear_binding(bound)
            if node.level == 0 and node.module == "importlib":
                if imported.name == "import_module":
                    self.dynamic_import_names.add(bound)
                    self._mark(node)
                elif imported.name != "metadata":
                    self._mark(node)
            elif (
                node.level == 0
                and node.module is not None
                and node.module.startswith("importlib.")
                and not _is_importlib_metadata(node.module)
            ):
                self._mark(node)
            elif node.level == 0 and node.module == "builtins":
                if imported.name in DYNAMIC_IMPORT_REFERENCES:
                    self.dynamic_import_names.add(bound)
                if imported.name in DYNAMIC_EXECUTION_NAMES:
                    self.execution_names.add(bound)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind_target(target, node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
            if isinstance(node.target, ast.Name):
                self._bind(node.target.id, node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, node.value)

    def _clear_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._clear_binding(target.id)
        elif isinstance(target, (ast.List, ast.Tuple)):
            for item in target.elts:
                self._clear_target(item)

    def _target_names(self, target: ast.AST) -> list[str]:
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.List, ast.Tuple)):
            return [name for item in target.elts for name in self._target_names(item)]
        return []

    def _bind_target(self, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, value)
        elif (
            isinstance(target, (ast.List, ast.Tuple))
            and isinstance(value, (ast.List, ast.Tuple))
            and len(target.elts) == len(value.elts)
        ):
            for target_item, value_item in zip(target.elts, value.elts):
                self._bind_target(target_item, value_item)
        else:
            self._clear_target(target)

    def visit_For(self, node: ast.For) -> None:
        saved = self._snapshot()
        self.visit(node.iter)
        self._clear_target(node.target)
        for statement in node.body:
            self.visit(statement)
        for name in self._target_names(node.target):
            self._restore_name(name, saved)
        for statement in node.orelse:
            self.visit(statement)

    visit_AsyncFor = visit_For

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._clear_target(item.optional_vars)
        for statement in node.body:
            self.visit(statement)

    visit_AsyncWith = visit_With

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        expressions: list[ast.expr],
    ) -> None:
        saved = self._snapshot()
        for generator in generators:
            self.visit(generator.iter)
            if isinstance(generator.iter, (ast.List, ast.Tuple)) and len(generator.iter.elts) == 1:
                self._bind_target(generator.target, generator.iter.elts[0])
            else:
                self._clear_target(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in expressions:
            self.visit(expression)
        self._restore(saved)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    visit_SetComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            self.visit(node.type)
        saved = self._snapshot()
        if node.name is not None:
            self._clear_binding(node.name)
        for statement in node.body:
            self.visit(statement)
        if node.name is not None:
            self._restore_name(node.name, saved)

    def visit_Call(self, node: ast.Call) -> None:
        attribute = (
            _literal_string(node.args[1], self.string_bindings)
            if len(node.args) >= 2
            else None
        )
        namespace = _terminal_name(node.args[0]) if node.args else None
        protected_getattr = _terminal_name(node.func) == "getattr" and (
            (namespace in self.builtins_names and attribute in {"eval", "exec", "__import__"})
            or (namespace in self.importlib_names and attribute == "import_module")
        )
        direct_execution = (
            isinstance(node.func, ast.Name) and node.func.id in self.execution_names
        )
        if protected_getattr or direct_execution:
            self._mark(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in self.dynamic_import_names:
            self._mark(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in self.dynamic_import_names:
            self._mark(node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value in DYNAMIC_IMPORT_STRINGS:
            self._mark(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if _literal_string(node, {}) in DYNAMIC_IMPORT_STRINGS:
            self._mark(node)
        self.generic_visit(node)

    def _visit_scoped(
        self,
        body: list[ast.stmt],
        parameters: list[str],
        *,
        base_state: BindingState | None = None,
    ) -> None:
        saved = self._snapshot()
        if base_state is not None:
            self._restore(base_state)
        for parameter in parameters:
            self._clear_binding(parameter)
        for statement in body:
            self.visit(statement)
        self._restore(saved)

    def _visit_scoped_expression(
        self,
        expression: ast.expr,
        parameters: list[str],
        *,
        base_state: BindingState | None = None,
    ) -> None:
        saved = self._snapshot()
        if base_state is not None:
            self._restore(base_state)
        for parameter in parameters:
            self._clear_binding(parameter)
        self.visit(expression)
        self._restore(saved)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        parameters = [argument.arg for argument in arguments]
        is_method = self.class_outer_state is not None
        body_base = self.class_outer_state or self._snapshot()
        class_outer_state = self.class_outer_state
        self.class_outer_state = None
        self._visit_scoped(
            node.body,
            parameters if is_method else [*parameters, node.name],
            base_state=body_base,
        )
        self.class_outer_state = class_outer_state
        self._clear_binding(node.name)

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        parameters = [
            argument.arg
            for argument in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
            )
        ]
        if node.args.vararg is not None:
            parameters.append(node.args.vararg.arg)
        if node.args.kwarg is not None:
            parameters.append(node.args.kwarg.arg)
        body_base = self.class_outer_state or self._snapshot()
        class_outer_state = self.class_outer_state
        self.class_outer_state = None
        self._visit_scoped_expression(
            node.body,
            parameters,
            base_state=body_base,
        )
        self.class_outer_state = class_outer_state

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        method_outer_state = self.class_outer_state or self._snapshot()
        class_outer_state = self.class_outer_state
        self.class_outer_state = method_outer_state
        self._visit_scoped(node.body, [])
        self.class_outer_state = class_outer_state
        self._clear_binding(node.name)


def _dynamic_imports(tree: ast.AST) -> list[tuple[int, str]]:
    visitor = _DynamicImportVisitor()
    visitor.visit(tree)
    return visitor.violations


def _forbidden_imports(tree: ast.AST, package: str) -> list[tuple[int, str]]:
    return [
        (line, module)
        for line, module in (*_static_imports(tree, package), *_dynamic_imports(tree))
        if module == DYNAMIC_IMPORT_CALL
        or (_is_project_module(module) and not _is_allowed_internal(module))
    ]


def test_aros_imports_only_use_the_one_way_internal_boundary() -> None:
    """Enforce reviewable architecture discipline; this is not a sandbox."""
    violations: list[str] = []

    for path in BOUNDARY_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        _, package = _module_and_package(path)
        for line, module in _forbidden_imports(tree, package):
            violations.append(f"{path.relative_to(REPO_ROOT)}:{line}: {module}")

    assert not violations, "AROS imports cross the one-way boundary:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("from .. import run", {"arbor.run"}),
        ("from arbor import executor", {"arbor.executor"}),
        ("import importlib as loader\nloader.import_module('arbor.review')", {DYNAMIC_IMPORT_CALL}),
        ("import importlib.util\nimportlib.import_module('arbor.review')", {DYNAMIC_IMPORT_CALL}),
        ("from importlib import import_module as load\nload('arbor.coordinator.main')", {DYNAMIC_IMPORT_CALL}),
        ("__import__('arbor.mcp.server')", {DYNAMIC_IMPORT_CALL}),
        ("module = 'arbor.review'\n__import__(module)", {DYNAMIC_IMPORT_CALL}),
        ("__import__('run', globals(), locals(), (), 2)", {DYNAMIC_IMPORT_CALL}),
        ("import builtins as b\nb.__import__('arbor.review')", {DYNAMIC_IMPORT_CALL}),
        ("import importlib\nimportlib.import_module('arbor.aros.runs')", {DYNAMIC_IMPORT_CALL}),
        ("target = 'arbor.review'\nimport_module(target)", {DYNAMIC_IMPORT_CALL}),
        ("target = 'arbor.review'\nloader.import_module(target)", {DYNAMIC_IMPORT_CALL}),
        ("from importlib import import_module as load\ndef later(target):\n    return load(target)", {DYNAMIC_IMPORT_CALL}),
        ("import importlib\nload = importlib.import_module\ndef later():\n    return load('arbor.review')", {DYNAMIC_IMPORT_CALL}),
        ("import importlib\nload = getattr(importlib, 'import_module')", {DYNAMIC_IMPORT_CALL}),
        ("import importlib", {DYNAMIC_IMPORT_CALL}),
        ("import importlib.util as util", {DYNAMIC_IMPORT_CALL}),
        ("from importlib.metadata import version", set()),
        ("import importlib.metadata", set()),
        ("import importlib.metadata_loader", {DYNAMIC_IMPORT_CALL}),
        ("from importlib import metadata", set()),
        ("from importlib import util", {DYNAMIC_IMPORT_CALL}),
        ("from importlib import import_module as load", {DYNAMIC_IMPORT_CALL}),
        ("load = __import__", {DYNAMIC_IMPORT_CALL}),
        ("name = '__import__'", {DYNAMIC_IMPORT_CALL}),
        ("name = 'import_module'", {DYNAMIC_IMPORT_CALL}),
        ("import builtins\ngetattr(builtins, 'exec')", {DYNAMIC_IMPORT_CALL}),
        ("name = 'eval'", set()),
        ("model.eval()", set()),
        ("runner = model.eval\nrunner()", set()),
        ("eval(source)", {DYNAMIC_IMPORT_CALL}),
        ("exec(source)", {DYNAMIC_IMPORT_CALL}),
        ("import builtins", set()),
        ("from builtins import open", set()),
        ("def load(builtins):\n    return getattr(builtins, '__im' + 'port__')", {DYNAMIC_IMPORT_CALL}),
        ("reference = __builtins__", set()),
        ("reference = runtime.__builtins__", set()),
        ("getattr(runtime.builtins, attribute)", set()),
        ("getattr(builtins, 'open')", set()),
        ("name: str = 'ev' + 'al'\ngetattr(builtins, name)", {DYNAMIC_IMPORT_CALL}),
        ("getattr(builtins, (name := 'eval'))", {DYNAMIC_IMPORT_CALL}),
        ("b: object = builtins\ngetattr(b, 'eval')", {DYNAMIC_IMPORT_CALL}),
        ("name = 'eval'\ngetattr(builtins, name)\nname = 'open'", {DYNAMIC_IMPORT_CALL}),
        ("name = 'open'\ngetattr(builtins, name)\nname = 'eval'", set()),
        ("name = 'open'\ndef nested():\n    name = 'eval'\ngetattr(builtins, name)", set()),
        ("def annotated(value: exec(source)) -> eval(source):\n    pass", {DYNAMIC_IMPORT_CALL}),
        ("class C(metaclass=eval(source)):\n    pass", {DYNAMIC_IMPORT_CALL}),
        ("lambda eval: eval(source)", set()),
        ("def eval(source):\n    return source\neval(source)", set()),
        ("class C:\n    eval = lambda value: value\n    def method(self):\n        return eval(source)", {DYNAMIC_IMPORT_CALL}),
        ("class C:\n    def eval(self, source):\n        return eval(source)", {DYNAMIC_IMPORT_CALL}),
        ("class C:\n    def eval(self, source):\n        return self.eval(source)", set()),
        ("for eval in functions:\n    eval(source)", set()),
        ("try:\n    operation()\nexcept Exception as eval:\n    eval(source)", set()),
        ("[eval(source) for eval in callbacks]", set()),
        ("(eval(source) for eval in callbacks)", set()),
        ("with callback() as eval:\n    eval(source)", set()),
        ("eval, other = callbacks\neval(source)", set()),
        ("for eval in []:\n    pass\neval(source)", {DYNAMIC_IMPORT_CALL}),
        ("[getattr(builtins, name) for name in ('eval',)]", {DYNAMIC_IMPORT_CALL}),
        ("b, = (builtins,)\ngetattr(b, 'eval')", {DYNAMIC_IMPORT_CALL}),
        ("from .builtins import helper", set()),
        ("from .importlib import helper", set()),
        ("from ..core import config", set()),
    ),
)
def test_aros_boundary_resolves_relative_imports_and_rejects_dynamic_imports(
    source: str,
    expected: set[str],
) -> None:
    tree = ast.parse(source)

    assert {module for _, module in _forbidden_imports(tree, "arbor.aros")} == expected


@pytest.mark.parametrize("name", ("__import__", "import_module"))
def test_aros_boundary_rejects_dynamic_import_references(name: str) -> None:
    tree = ast.parse(f"reflection = {name}")

    assert _forbidden_imports(tree, "arbor.aros")


@pytest.mark.parametrize(
    "name",
    ("vars", "globals", "locals", "getattr", "eval", "exec"),
)
def test_aros_boundary_permits_ordinary_reflection_names(name: str) -> None:
    tree = ast.parse(f"reflection = {name}")

    assert not _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_permits_harmless_standard_imports() -> None:
    tree = ast.parse(
        "import json\n"
        "import os\n"
        "from pathlib import Path\n"
        "directory_flag = getattr(os, 'O_DIRECTORY', 0)"
    )

    assert not _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_permits_normal_getattr() -> None:
    tree = ast.parse(
        "directory_flag = getattr(os, 'O_DIRECTORY', 0)\n"
        "value = getattr(subject, attribute, None)"
    )

    assert not _forbidden_imports(tree, "arbor.aros")


def test_aros_boundary_gate_is_documented_as_architecture_discipline() -> None:
    documentation = test_aros_imports_only_use_the_one_way_internal_boundary.__doc__ or ""

    assert "architecture discipline" in documentation
    assert "not a sandbox" in documentation


def test_config_substrate_is_in_the_canonical_allowlist() -> None:
    assert {"arbor._app", "arbor.cli.user_config"} <= set(ALLOWED_INTERNAL_PREFIXES)


def test_direct_aros_import_loads_no_forbidden_project_modules() -> None:
    script = """
import importlib
import importlib.util
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "arbor",
    root / "src" / "__init__.py",
    submodule_search_locations=[str(root / "src")],
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["arbor"] = module
spec.loader.exec_module(module)
importlib.import_module("arbor.cli.aros_app")
print(json.dumps(sorted(sys.modules)))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    forbidden = sorted(
        module
        for module in loaded
        if _is_project_module(module) and not _is_allowed_internal(module)
    )
    assert not forbidden, f"direct AROS import loaded forbidden modules: {forbidden}"


def test_start_config_path_loads_config_helpers_but_not_semantic_legacy(
    tmp_path: Path,
) -> None:
    script = """
import importlib
import importlib.util
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "arbor",
    root / "src" / "__init__.py",
    submodule_search_locations=[str(root / "src")],
)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["arbor"] = module
spec.loader.exec_module(module)
commands = importlib.import_module("arbor.cli.commands.aros_cmd")
commands.llm_defaults()
print(json.dumps(sorted(sys.modules)))
"""
    environment = dict(os.environ)
    environment["HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert {"arbor._app", "arbor.cli.user_config"} <= set(loaded)
    forbidden = sorted(
        module
        for module in loaded
        if any(
            module == prefix or module.startswith(f"{prefix}.")
            for prefix in SEMANTIC_LEGACY_PREFIXES
        )
    )
    assert not forbidden, f"start config path loaded semantic legacy: {forbidden}"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def legacy_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "aros-tests@example.invalid")
    _git(repo, "config", "user.name", "AROS Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    for relative_path in FROZEN_FILES:
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("legacy one\nlegacy two\n", encoding="utf-8")
    (repo / "src" / "coordinator" / "state.bin").write_bytes(b"\x00legacy")
    (repo / "src" / "coordinator" / "empty").touch()
    (repo / "src" / "coordinator" / "helper.py").write_text(
        FROZEN_HELPER, encoding="utf-8"
    )
    (repo / "src" / "coordinator" / "padded_source.py").write_text(
        PADDED_FROZEN_SOURCE, encoding="utf-8"
    )
    (repo / "src" / "legacy.py").write_text(
        "legacy one\nlegacy two\n", encoding="utf-8"
    )
    (repo / "legacy_control.py").write_text(
        "legacy control\n", encoding="utf-8"
    )
    (repo / ".gitignore").write_text(
        "src/coordinator/ignored.py\n"
        "src/coordinator/ignored.bin\n"
        "src/coordinator/ignored.empty\n"
        "__pycache__/\n"
        "build/\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("outside\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, base


@pytest.fixture
def source_growth_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "aros-tests@example.invalid")
    _git(repo, "config", "user.name", "AROS Tests")
    _git(repo, "config", "commit.gpgsign", "false")
    existing = repo / "src" / "existing.py"
    existing.parent.mkdir()
    existing.write_text("existing source\n", encoding="utf-8")
    (repo / "src" / "existing.bin").write_bytes(b"\0existing binary")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, base


def _run_checker(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--repo",
            str(repo),
            f"--base={base}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("relative_path", FROZEN_FILES)
def test_legacy_freeze_rejects_added_lines(
    legacy_repo: tuple[Path, str], relative_path: str
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.write_text(path.read_text(encoding="utf-8") + "new behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert relative_path in result.stdout + result.stderr


def test_legacy_freeze_rejects_binary_changes(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "coordinator" / "state.bin"
    path.write_bytes(b"\x00changed")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/coordinator/state.bin" in result.stdout + result.stderr


def test_legacy_freeze_rejects_binary_change_despite_text_attribute(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    (repo / ".gitattributes").write_text(
        "src/coordinator/state.bin diff\n", encoding="utf-8"
    )
    _git(repo, "add", ".gitattributes")
    (repo / "src" / "coordinator" / "state.bin").write_bytes(b"")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/coordinator/state.bin" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("src/coordinator/added.bin", b"\x00added"),
        ("src/coordinator/added.empty", b""),
    ),
)
def test_legacy_freeze_rejects_tracked_binary_and_empty_additions(
    legacy_repo: tuple[Path, str], relative_path: str, content: bytes
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.write_bytes(content)
    _git(repo, "add", relative_path)

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert relative_path in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("src/coordinator/untracked.py", b"new behavior\n"),
        ("src/coordinator/untracked.bin", b"\x00new"),
        ("src/coordinator/untracked.empty", b""),
    ),
)
def test_legacy_freeze_rejects_untracked_files(
    legacy_repo: tuple[Path, str], relative_path: str, content: bytes
) -> None:
    repo, base = legacy_repo
    (repo / relative_path).write_bytes(content)

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert relative_path in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("relative_path", "content"),
    (
        ("src/coordinator/ignored.py", b"ignored behavior\n"),
        ("src/coordinator/ignored.bin", b"\x00ignored"),
        ("src/coordinator/ignored.empty", b""),
        ("src/coordinator/__pycache__/helper.pyc", b"\x00cache"),
        ("src/executor/build/generated.py", b"generated\n"),
    ),
)
def test_legacy_freeze_permits_ignored_untracked_files(
    legacy_repo: tuple[Path, str], relative_path: str, content: bytes
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_fails_closed_when_copy_detection_limit_is_hit(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, _ = legacy_repo
    sources = repo / "bulk-sources"
    sources.mkdir()
    for number in range(1001):
        (sources / f"source-{number}.txt").write_text(
            f"source {number}\n", encoding="utf-8"
        )
    _git(repo, "add", "bulk-sources")
    _git(repo, "commit", "-qm", "add copy candidates")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    destination = repo / "copied" / "helper.py"
    destination.parent.mkdir()
    destination.write_text(FROZEN_HELPER + "# new behavior\n", encoding="utf-8")
    candidates = repo / "bulk-destinations"
    candidates.mkdir()
    for number in range(1001):
        (candidates / f"destination-{number}.txt").write_text(
            f"destination {number}\n", encoding="utf-8"
        )
    _git(repo, "add", "copied", "bulk-destinations")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "too many files" in result.stdout + result.stderr


@pytest.mark.parametrize("delete_frozen_helper", (False, True))
def test_legacy_freeze_permits_independent_low_similarity_utility(
    legacy_repo: tuple[Path, str], delete_frozen_helper: bool
) -> None:
    repo, base = legacy_repo
    utility_path = repo / "independent_utility.py"
    utility_path.write_text(INDEPENDENT_UTILITY, encoding="utf-8")
    _git(repo, "add", str(utility_path.relative_to(repo)))
    if delete_frozen_helper:
        helper_path = repo / "src" / "coordinator" / "helper.py"
        helper_path.unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_rejects_padded_copy_outside_growth_allowlist(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    destination = repo / "src" / "control_v2.py"
    destination.write_text(PADDED_FROZEN_SOURCE + PADDING, encoding="utf-8")
    _git(repo, "add", "src/control_v2.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/control_v2.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_growth_in_existing_legacy_source(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "legacy.py"
    path.write_text(path.read_text(encoding="utf-8") + "new behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy.py" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_rejects_new_source_symlinks(
    legacy_repo: tuple[Path, str], staged: bool
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "legacy_link.py"
    path.symlink_to("legacy.py")
    if staged:
        _git(repo, "add", "src/legacy_link.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy_link.py" in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("content", "staged"),
    (
        (b"", False),
        (b"", True),
        (b"\0legacy binary", False),
        (b"\0legacy binary", True),
    ),
)
def test_legacy_freeze_rejects_new_empty_and_binary_source_paths(
    source_growth_repo: tuple[Path, str], content: bytes, staged: bool
) -> None:
    repo, base = source_growth_repo
    path = repo / "src" / "legacy_payload.py"
    path.write_bytes(content)
    if staged:
        _git(repo, "add", "src/legacy_payload.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy_payload.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_new_gitlink_under_source(
    source_growth_repo: tuple[Path, str],
) -> None:
    repo, base = source_growth_repo
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{base},src/legacy-submodule",
    )
    staged = _git(repo, "ls-files", "--stage", "src/legacy-submodule").stdout
    assert staged.startswith("160000 ")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy-submodule" in result.stdout + result.stderr


def test_legacy_freeze_rejects_gitlink_type_change_under_source(
    source_growth_repo: tuple[Path, str],
) -> None:
    repo, base = source_growth_repo
    _git(
        repo,
        "update-index",
        "--cacheinfo",
        f"160000,{base},src/existing.py",
    )
    cached = _git(
        repo,
        "diff",
        "--cached",
        "--raw",
        base,
        "--",
        "src/existing.py",
    ).stdout
    assert " T\tsrc/existing.py" in cached

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/existing.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_gitlink_update_under_source(
    source_growth_repo: tuple[Path, str],
) -> None:
    repo, original_base = source_growth_repo
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{original_base},src/existing-submodule",
    )
    _git(repo, "commit", "-qm", "add gitlink")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(
        repo,
        "update-index",
        "--cacheinfo",
        f"160000,{base},src/existing-submodule",
    )
    cached = _git(
        repo,
        "diff",
        "--cached",
        "--raw",
        base,
        "--",
        "src/existing-submodule",
    ).stdout
    assert cached.startswith(":160000 160000 ")
    assert " M\tsrc/existing-submodule" in cached

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/existing-submodule" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_rejects_tracked_binary_replacement(
    source_growth_repo: tuple[Path, str], staged: bool
) -> None:
    repo, base = source_growth_repo
    path = repo / "src" / "existing.bin"
    path.write_bytes(b"\0replacement binary")
    if staged:
        _git(repo, "add", "src/existing.bin")
    cached = ("--cached",) if staged else ()
    numstat = _git(
        repo,
        "diff",
        *cached,
        "--numstat",
        base,
        "--",
        "src/existing.bin",
    ).stdout
    assert numstat.startswith("-\t-\t")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/existing.bin" in result.stdout + result.stderr


def test_legacy_freeze_rejects_r100_move_into_non_allowlisted_source(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    _git(repo, "mv", "legacy_control.py", "src/control_v2.py")
    raw = _git(
        repo,
        "diff",
        "--raw",
        "--find-renames=50%",
        base,
        "--",
    ).stdout
    assert "R100" in raw

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/control_v2.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_staged_symlink_hidden_by_unstaged_delete(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "legacy_link.py"
    path.symlink_to("legacy.py")
    _git(repo, "add", "src/legacy_link.py")
    path.unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/legacy_link.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_staged_text_growth_hidden_by_unstaged_delete(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "new_legacy.py"
    path.write_text("staged legacy behavior\n", encoding="utf-8")
    _git(repo, "add", "src/new_legacy.py")
    path.unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/new_legacy.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_staged_r100_hidden_by_unstaged_delete(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    destination = repo / "src" / "control_v2.py"
    _git(repo, "mv", "legacy_control.py", "src/control_v2.py")
    destination.unlink()
    cached = _git(
        repo,
        "diff",
        "--cached",
        "--raw",
        "--find-renames=50%",
        base,
        "--",
    ).stdout
    assert "R100" in cached

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/control_v2.py" in result.stdout + result.stderr


def test_legacy_freeze_rejects_staged_c100_hidden_by_unstaged_delete(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    source = repo / "legacy_control.py"
    destination = repo / "src" / "control_copy.py"
    destination.write_bytes(source.read_bytes())
    _git(repo, "add", "src/control_copy.py")
    destination.unlink()
    cached = _git(
        repo,
        "diff",
        "--cached",
        "--raw",
        "--find-copies=50%",
        "--find-copies-harder",
        base,
        "--",
    ).stdout
    assert "C100" in cached

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/control_copy.py" in result.stdout + result.stderr


@pytest.mark.parametrize(
    "relative_path",
    (
        "src/aros/new_behavior.py",
        "src/core/new_behavior.py",
        "src/cli/aros_app.py",
        "src/cli/commands/aros_cmd.py",
        "src/cli/app.py",
    ),
)
def test_legacy_freeze_permits_explicit_aros_growth(
    legacy_repo: tuple[Path, str], relative_path: str
) -> None:
    repo, base = legacy_repo
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new AROS behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_file_allowlist_does_not_allow_descendants(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "cli" / "app.py" / "escape.py"
    path.parent.mkdir(parents=True)
    path.write_text("new legacy behavior\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/cli/app.py/escape.py" in result.stdout + result.stderr


@pytest.mark.parametrize("staged", (False, True))
def test_legacy_freeze_permits_pure_deletion_in_non_allowlisted_source(
    legacy_repo: tuple[Path, str], staged: bool
) -> None:
    repo, base = legacy_repo
    (repo / "src" / "legacy.py").write_text("legacy one\n", encoding="utf-8")
    if staged:
        _git(repo, "add", "src/legacy.py")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_permits_crlf_text_deletion(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, _ = legacy_repo
    (repo / ".gitattributes").write_text(
        "src/legacy.py text eol=crlf\n", encoding="utf-8"
    )
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "declare CRLF checkout")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    path = repo / "src" / "legacy.py"
    path.write_bytes(b"legacy one\r\n")
    assert b"\r\n" in path.read_bytes()
    numstat = _git(repo, "diff", "--numstat", base, "--", "src/legacy.py").stdout
    assert numstat.startswith("0\t1\t")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_rejects_rename_from_frozen_root(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    destination = repo / "moved" / "main.py"
    destination.parent.mkdir()
    _git(repo, "mv", "src/coordinator/main.py", "moved/main.py")
    destination.write_text(
        destination.read_text(encoding="utf-8") + "new behavior\n",
        encoding="utf-8",
    )

    result = _run_checker(repo, base)

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "src/coordinator/main.py" in output
    assert "moved/main.py" in output


def test_legacy_freeze_rejects_rename_when_git_rename_limit_is_low(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    _git(repo, "config", "diff.renameLimit", "1")
    destination = repo / "moved" / "main.py"
    destination.parent.mkdir()
    _git(repo, "mv", "src/coordinator/main.py", "moved/main.py")
    destination.write_text(
        destination.read_text(encoding="utf-8") + "new behavior\n",
        encoding="utf-8",
    )
    for number in range(3):
        candidate = repo / "moved" / f"candidate-{number}.py"
        candidate.write_text(f"candidate {number}\n", encoding="utf-8")
        _git(repo, "add", str(candidate.relative_to(repo)))

    result = _run_checker(repo, base)

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "src/coordinator/main.py" in output
    assert "moved/main.py" in output


def test_legacy_freeze_rejects_copy_from_frozen_root(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    source = repo / "src" / "coordinator" / "main.py"
    destination = repo / "copied" / "main.py"
    destination.parent.mkdir()
    destination.write_bytes(source.read_bytes())
    _git(repo, "add", "copied/main.py")

    result = _run_checker(repo, base)

    assert result.returncode == 2
    output = result.stdout + result.stderr
    assert "src/coordinator/main.py" in output
    assert "copied/main.py" in output


def test_legacy_freeze_permits_deletion_only(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    (repo / "src" / "run.py").write_text("legacy one\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_rejects_deletion_combined_with_mode_change(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    path = repo / "src" / "run.py"
    path.write_text("legacy one\n", encoding="utf-8")
    path.chmod(0o755)

    result = _run_checker(repo, base)

    assert result.returncode == 2
    assert "src/run.py" in result.stdout + result.stderr


def test_legacy_freeze_permits_text_deletion_despite_binary_attribute(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    (repo / ".gitattributes").write_text("src/run.py -diff\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    (repo / "src" / "run.py").write_text("legacy one\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "relative_path",
    ("src/coordinator/state.bin", "src/coordinator/empty"),
)
def test_legacy_freeze_permits_tracked_binary_and_empty_deletions(
    legacy_repo: tuple[Path, str], relative_path: str
) -> None:
    repo, base = legacy_repo
    (repo / relative_path).unlink()

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_permits_changes_outside_frozen_roots(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    (repo / "README.md").write_text("outside\nnew docs\n", encoding="utf-8")

    result = _run_checker(repo, base)

    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_freeze_fails_closed_for_repo_subdirectory(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, base = legacy_repo
    subdirectory = repo / "nested"
    subdirectory.mkdir()

    result = _run_checker(subdirectory, base)

    assert result.returncode == 2
    assert "top level" in result.stdout + result.stderr


def test_legacy_freeze_fails_closed_for_invalid_base(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, _ = legacy_repo

    result = _run_checker(repo, "not-a-valid-base")

    assert result.returncode == 2
    assert "not-a-valid-base" in result.stdout + result.stderr


def test_legacy_freeze_fails_closed_for_option_shaped_base(
    legacy_repo: tuple[Path, str],
) -> None:
    repo, _ = legacy_repo

    result = _run_checker(repo, "--quiet")

    assert result.returncode == 2
    assert "--quiet" in result.stdout + result.stderr
