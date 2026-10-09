"""Executable dependency rules for the boundaries described in the architecture ADR."""

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "lead_parser"
IO_MODULES = {
    "io",
    "os",
    "pathlib",
    "shutil",
    "sqlite3",
    "subprocess",
    "tempfile",
    "socket",
    "http",
    "urllib",
}


def imports_in(path):
    module = ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    package = module.rsplit(".", 1)[0]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield importlib.util.resolve_name("." * node.level + (node.module or ""), package)
            else:
                yield node.module or ""


@pytest.mark.parametrize("layer", ["core", "application"])
def test_business_layers_depend_only_on_stdlib_and_inner_layers(layer):
    allowed = {"lead_parser.core"}
    if layer == "application":
        allowed.add("lead_parser.application")
    modules = list((PACKAGE / layer).rglob("*.py"))
    assert modules, f"Missing {layer} layer"
    violations = []
    for path in modules:
        for imported in imports_in(path):
            if imported.split(".")[0] in IO_MODULES:
                violations.append(f"{path.relative_to(PACKAGE)} performs I/O through {imported}")
                continue
            if imported.split(".")[0] in sys.stdlib_module_names:
                continue
            if any(imported == prefix or imported.startswith(prefix + ".") for prefix in allowed):
                continue
            violations.append(f"{path.relative_to(PACKAGE)} imports {imported}")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in {"open", "input", "print", "__import__"}:
                    violations.append(f"{path.relative_to(PACKAGE)} calls {node.func.id}")
    assert not violations, "\n".join(violations)


def test_integrations_do_not_depend_on_interfaces_or_composition_root():
    violations = []
    for path in (PACKAGE / "infrastructure").rglob("*.py"):
        for imported in imports_in(path):
            if imported == "lead_parser.bootstrap" or imported.startswith("lead_parser.interfaces"):
                violations.append(f"{path.relative_to(PACKAGE)} imports {imported}")
    assert not violations, "\n".join(violations)


def test_business_layers_import_without_any_installed_dependencies():
    modules = [
        ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts)
        for layer in ("core", "application")
        for path in (PACKAGE / layer).rglob("*.py")
    ]
    code = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(PACKAGE.parent)!r})\n"
        f"for name in {json.dumps(modules)}: importlib.import_module(name)\n"
        "assert not any(name.startswith('lead_parser.infrastructure') for name in sys.modules)\n"
    )
    process = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True, check=False)
    assert process.returncode == 0, process.stderr
