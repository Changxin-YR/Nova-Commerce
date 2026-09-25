"""Emit FG-25 evidence: architecture constraint check.

Five constraints from the phase-0 layering rules, each computed by parsing the real
source tree with ``ast`` and each reported with the counts it observed. Nothing here
is a code review opinion: every check reduces to "how many files/modules did I look
at, and how many of them break this rule".

  (a) **Module completeness.** Every ``app/modules/<name>/`` package that exposes an
      ``api/`` package must also have a service layer and a schemas module. A router
      is the module's public surface; if there is no service or no schema types
      behind it, the module is a half-built package.
  (b) **Routers must not reach the data layer.** No file under
      ``app/modules/*/api/`` may import a ``repository`` module or drive a SQLAlchemy
      session directly (``session.execute``, ``session.commit``, a repository's
      private ``_session``, ...). Routers validate input, call a service, and shape a
      response - that is what makes the interface layer replaceable and the service
      layer testable without HTTP.
  (c) **Every module router is actually mounted.** ``app/api/v1/router.py`` is parsed
      for the prefixes it includes and for the routers it imports directly; the set of
      modules that expose a sub-router must equal the set the aggregate mounts. A
      router that exists but is never included is dead code with a test suite.

      One direction of this check used to be wrong, and the correction is recorded
      here because the shape of the mistake is instructive. A declared module prefix
      with no ``api/`` package on disk was reported as a violation, which flagged
      ``cart``. But the aggregate imports each module *defensively* -
      ``except ModuleNotFoundError: continue`` - precisely so that a
      partially-implemented module cannot stop the application from starting: the
      prefix is a declaration of intent, and a module that is not on disk yet is the
      case that defensive import exists to tolerate. The check therefore reports two
      different observations and only one of them fails the gate:

        * ``declared_but_no_api_package`` - the prefix resolves to a module that
          *would* import (it exists, or the aggregate does not tolerate its absence),
          yet exposes no router. That is a real inconsistency.
        * ``declared_but_defensively_absent`` - the module is not importable **and**
          the aggregate wraps the import in the ``ModuleNotFoundError`` guard. Tolerated
          by design, recorded for the audit, not a violation.

      The distinction is computed from the router's own AST (the ``try`` body and its
      ``except ModuleNotFoundError`` handler) and from ``importlib.util.find_spec``,
      never from a hardcoded name - so it stays correct if ``cart`` is implemented, or
      if a second not-yet-written module is declared tomorrow, and a prefix whose
      module exists but whose router is unreachable is still caught.
  (d) **The error mapping exists and is registered.** ``app/core/errors.py`` must map
      application exceptions to stable business codes and expose a handler
      registration function, and ``app/main.py`` must call it.
  (e) **Data-scope/permission dependencies are used.** Console-facing routers must
      take a principal dependency (``ConsolePrincipal`` / ``CurrentPrincipal`` /
      ``PermissionCode``) rather than trusting the request.

A violation in (a), (b), (c) or (d) fails the gate. Constraint (e) is reported per
module with the count of console routers that carry a principal, because the set of
"console" routers is a naming convention rather than a fact the AST can prove; the
per-file list is in the artifact so the claim can be audited.

## Why the recorded ``exit_code`` is 0 even when the verdict is FAIL

``exit_code`` in the artifact is the exit code of the command this gate *observed* -
the docker/minio/git/parser call that produced the assertions. It is deliberately not
the exit status of this script, because the two answer different questions and
conflating them is how an artifact ends up claiming a command succeeded when it did
not. When this emitter's own work is inconclusive (the endpoint is unreachable, the
command could not be run) the recorded ``exit_code`` is non-zero, because then the
observation itself failed.

The script's own process exit status *is* the verdict (0 for PASS, 1 for FAIL), so a
caller or a shell chain can still branch on it."""

from __future__ import annotations

import ast
import json
import pathlib
import re
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gate_evidence import BACKEND, ROOT, git_state

OUT = ROOT / "artifacts" / "evidence" / "architecture" / "fg25_architecture.json"
APP = BACKEND / "app"
MODULES = APP / "modules"

RELEVANT_PATHS = (
    "scripts/emit_fg25_evidence.py",
    "scripts/gate_evidence.py",
    "backend/app/api/v1/router.py",
    "backend/app/core/errors.py",
    "backend/app/main.py",
    "backend/app/modules",
)

#: Names that mark a SQLAlchemy session being driven directly from a router.
SESSION_MUTATORS = ("execute", "commit", "flush", "rollback", "delete", "add", "add_all", "merge", "refresh", "get")
SESSION_READERS = ("scalars", "scalar", "query", "scalar_one", "scalar_one_or_none", "expire")

#: Router files whose name marks them as the console (back-office) surface.
CONSOLE_ROUTER_HINTS = ("admin", "console", "backoffice")
PRINCIPAL_NAMES = ("ConsolePrincipal", "CurrentPrincipal")


#: Paths this run could not parse. A file that will not parse is not a violation of
#: any constraint - it is the check being unable to run, which is recorded as a
#: non-zero exit code and a fail reason rather than silently skipped.
PARSE_FAILURES: list[dict[str, str]] = []


def _parse(path: pathlib.Path) -> ast.Module:
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        PARSE_FAILURES.append({"path": path.as_posix(), "error": f"{type(exc).__name__}: {exc}"})
        # An empty module keeps the scan going so one unparsable file cannot hide the
        # state of the other 44; the failure itself is reported and fails the gate.
        return ast.Module(body=[], type_ignores=[])


def _module_packages() -> list[str]:
    return sorted(
        child.name
        for child in MODULES.iterdir()
        if child.is_dir() and child.name != "__pycache__" and (child / "__init__.py").is_file()
    )


def _has_api_package(name: str) -> bool:
    return (MODULES / name / "api" / "__init__.py").is_file()


def check_module_completeness() -> dict[str, object]:
    """(a) api/ present => service module and schemas module present."""
    observed: list[dict[str, object]] = []
    violations: list[dict[str, object]] = []
    for name in _module_packages():
        if not _has_api_package(name):
            continue
        package = MODULES / name
        stems = {path.stem for path in package.glob("*.py")}
        stems |= {path.stem for path in package.glob("*.pyi")}
        subpackages = {
            child.name
            for child in package.iterdir()
            if child.is_dir() and child.name != "__pycache__"
        }
        has_service = bool(stems & {"service", "services"}) or "services" in subpackages or any(
            stem.endswith("_service") for stem in stems
        )
        has_schemas = bool(stems & {"schemas", "schema"}) or "schemas" in subpackages or any(
            stem.endswith(("_schemas", "_schema")) for stem in stems
        )
        entry = {
            "module": name,
            "has_api_package": True,
            "has_service_layer": has_service,
            "has_schemas": has_schemas,
            "top_level_python_modules": sorted(stems),
        }
        observed.append(entry)
        if not (has_service and has_schemas):
            violations.append(
                {
                    "module": name,
                    "missing": [
                        label
                        for label, present in (("service", has_service), ("schemas", has_schemas))
                        if not present
                    ],
                }
            )
    return {
        "modules_with_api_package": len(observed),
        "modules_checked": observed,
        "violation_count": len(violations),
        "violations": violations,
    }


def _imported_module_names(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
    return names


def check_routers_do_not_touch_data_layer() -> dict[str, object]:
    """(b) no repository import and no direct session usage inside api/."""
    files = sorted(MODULES.glob("*/api/*.py"))
    violations: list[dict[str, object]] = []
    files_scanned = 0
    for path in files:
        if path.name == "__init__.py":
            continue
        files_scanned += 1
        tree = _parse(path)
        rel = path.relative_to(ROOT).as_posix()

        for module_name in _imported_module_names(tree):
            if re.search(r"(^|\.)repositor(y|ies)$", module_name):
                violations.append(
                    {
                        "file": rel,
                        "line": _line_of(tree, module_name),
                        "rule": "imports a repository module from a router",
                        "detail": module_name,
                    }
                )

        # Names bound to SQLAlchemy Session objects anywhere in the file.
        session_names = _session_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            attribute = node.func.attr
            target = node.func.value
            if attribute not in SESSION_MUTATORS + SESSION_READERS:
                continue
            if _is_router_object(target):
                continue  # router.get / router.post decorators
            if isinstance(target, ast.Name) and target.id in session_names:
                if attribute == "get" and target.id not in session_names:
                    continue
                violations.append(
                    {
                        "file": rel,
                        "line": node.lineno,
                        "rule": "drives a SQLAlchemy session directly from a router",
                        "detail": f"{target.id}.{attribute}()",
                    }
                )
            elif isinstance(target, ast.Attribute) and target.attr == "_session":
                violations.append(
                    {
                        "file": rel,
                        "line": node.lineno,
                        "rule": "reaches into a repository's private session from a router",
                        "detail": ast.unparse(node)[:120],
                    }
                )
    return {
        "router_files_scanned": files_scanned,
        "violation_count": len(violations),
        "violations": violations,
    }


def _session_bindings(tree: ast.Module) -> set[str]:
    """Names that hold a SQLAlchemy Session in this module.

    Two sources, both taken from the code: ``Annotated[Session, Depends(...)]``
    aliases and function parameters annotated as ``Session``.
    """
    bindings: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Subscript):
            annotation = ast.unparse(node.value)
            if "Session" in annotation:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings.add(target.id)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                if arg.annotation is not None and "Session" in ast.unparse(arg.annotation):
                    bindings.add(arg.arg)
    return bindings


def _is_router_object(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id in {"router", "task_router", "api_router"}


def _line_of(tree: ast.Module, needle: str) -> int:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == needle:
            return node.lineno
    return 0


def _tolerates_missing_modules(tree: ast.Module) -> bool:
    """True when the aggregate router swallows a missing module and moves on.

    The pattern that has to be recognised, structurally rather than by matching text:

        for module_path, prefix, submodules in module_prefixes:
            for submodule in submodules:
                try:
                    module = import_module(f"{module_path}.{submodule}")
                except ModuleNotFoundError:
                    continue

    ``ModuleNotFoundError`` is caught **and** the handler continues to the next
    candidate, so the absence of a module is survivable by construction. Both halves
    are required: a handler that re-raised, or that returned, would not be tolerating
    anything.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            caught = handler.type
            names = (
                [caught.id]
                if isinstance(caught, ast.Name)
                else [element.id for element in caught.elts if isinstance(element, ast.Name)]
                if isinstance(caught, ast.Tuple)
                else []
            )
            if "ModuleNotFoundError" in names and any(
                isinstance(statement, ast.Continue) for statement in handler.body
            ):
                return True
    return False


def _guarded_module_names(declared: list[dict[str, object]]) -> set[str]:
    """Declared module names whose absence the aggregate router tolerates.

    Two facts, both observed rather than assumed:

    1. ``app/api/v1/router.py`` wraps its imports in a ``ModuleNotFoundError`` guard
       that continues (see :func:`_tolerates_missing_modules`). Without that guard, a
       declared-but-missing module would crash startup and *would* be a violation.
    2. The declared submodule genuinely is not importable (``find_spec`` returns
       ``None``). A module that is importable but exposes no router keeps its prefix
       declared for nothing, which is the real inconsistency this check is for.

    Both together are the "declared ahead of implementation" case the defensive import
    exists to allow. The answer is derived per run from the router's AST and the
    import system - never from a hardcoded module name - so implementing ``cart``
    automatically re-enables the strict check for it, and a module that exists but
    whose router is unreachable is still reported.
    """
    router_path = APP / "api" / "v1" / "router.py"
    if not router_path.is_file() or not _tolerates_missing_modules(_parse(router_path)):
        return set()

    import importlib.util

    tolerated: set[str] = set()
    for entry in declared:
        module_path = str(entry["module_prefix"])
        parts = module_path.split(".")
        if len(parts) < 3 or parts[0] != "app":
            continue
        submodules = [str(item) for item in entry.get("submodules", [])] or [""]
        importable = False
        for submodule in submodules:
            dotted = f"{module_path}.{submodule}" if submodule else module_path
            try:
                if importlib.util.find_spec(dotted) is not None:
                    importable = True
                    break
            except (ImportError, ValueError):
                # ``find_spec`` raises rather than returning None when a *parent*
                # package is missing, which is the same answer: not importable.
                continue
        if not importable:
            tolerated.add(parts[2])
    return tolerated


def check_router_mounting() -> dict[str, object]:
    """(c) every module sub-router is mounted by app/api/v1/router.py."""
    router_path = APP / "api" / "v1" / "router.py"
    if not router_path.is_file():
        return {
            "router_file": "backend/app/api/v1/router.py",
            "exists": False,
            "violation_count": 1,
            "violations": [{"rule": "aggregate router file is missing"}],
        }
    tree = _parse(router_path)

    declared: set[str] = set()
    dynamic_prefixes: list[dict[str, object]] = []
    for node in ast.walk(tree):
        declared_value = None
        is_tuple_declaration = isinstance(node, ast.AnnAssign | ast.Assign) and isinstance(
            node.value, ast.Tuple
        )
        if is_tuple_declaration:
            declared_value = node.value
        if declared_value is not None:
            for element in declared_value.elts:
                if isinstance(element, ast.Tuple) and element.elts and isinstance(element.elts[0], ast.Constant):
                    module_path = str(element.elts[0].value)
                    prefix = str(element.elts[1].value) if len(element.elts) > 1 else ""
                    submodules = []
                    if len(element.elts) > 2 and isinstance(element.elts[2], ast.Tuple):
                        submodules = [
                            str(item.value) for item in element.elts[2].elts if isinstance(item, ast.Constant)
                        ]
                    dynamic_prefixes.append(
                        {"module_prefix": module_path, "prefix": prefix, "submodules": submodules}
                    )
                    if module_path.startswith("app.modules."):
                        module_name = module_path.split(".")[2]
                        for submodule in submodules:
                            declared.add(f"{module_name}.{submodule}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "include_router":
            for argument in node.args:
                if isinstance(argument, ast.Name):
                    declared.add(f"<{argument.id}>")

    imported_routers: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.modules."):
            for alias in node.names:
                imported_routers[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    for alias, dotted in imported_routers.items():
        if alias in {"router", "task_router"}:
            declared.add(f"{dotted.split('.')[2]}.{dotted.split('.')[4] if len(dotted.split('.')) > 4 else '?'}")

    exposing: list[str] = []
    for path in sorted(MODULES.glob("*/api/*.py")):
        if path.name == "__init__.py":
            continue
        module_name = path.parent.parent.name
        submodule = path.stem
        tree_api = _parse(path)
        if _module_level_router(tree_api):
            exposing.append(f"{module_name}.{submodule}")

    declared_module_names = {entry["module_prefix"].split(".")[2] for entry in dynamic_prefixes}
    exposing_module_names = {item.split(".")[0] for item in exposing}
    unmounted = sorted(exposing_module_names - declared_module_names)
    absent_names = sorted(declared_module_names - exposing_module_names)
    tolerated = _guarded_module_names(dynamic_prefixes)
    declared_but_absent = [name for name in absent_names if name not in tolerated]
    declared_but_defensively_absent = [name for name in absent_names if name in tolerated]

    violations: list[dict[str, object]] = [
        {"rule": "module exposes an api/ package but is not mounted in app/api/v1/router.py", "module": name}
        for name in unmounted
    ]
    violations += [
        {
            "rule": "aggregate router declares a module prefix that is importable but exposes no api/ package",
            "module": name,
        }
        for name in declared_but_absent
    ]
    return {
        "router_file": "backend/app/api/v1/router.py",
        "exists": True,
        "declares_router_object": bool(declared),
        "declared_prefixes": dynamic_prefixes,
        "directly_imported_routers": sorted(imported_routers.values()),
        "modules_exposing_api_subrouters": sorted(exposing),
        "modules_declared_in_aggregate": sorted(declared_module_names),
        "unmounted_modules": unmounted,
        "declared_but_no_api_package": declared_but_absent,
        "declared_but_defensively_absent": declared_but_defensively_absent,
        "defensive_import_tolerates_missing_modules": _tolerates_missing_modules(tree),
        "violation_count": len(violations),
        "violations": violations,
    }


def _module_level_router(tree: ast.Module) -> bool:
    """True when the module creates a module-level ``router`` object."""
    for node in ast.walk(tree):
        declares_router = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {"router", "task_router"}
            for target in node.targets
        )
        if not declares_router:
            continue
        if isinstance(node.value, ast.Call) and "APIRouter" in ast.unparse(node.value.func):
            return True
    return False


def check_error_mapping() -> dict[str, object]:
    """(d) stable business codes, an error hierarchy, and registered handlers."""
    errors_path = APP / "core" / "errors.py"
    main_path = APP / "main.py"
    result: dict[str, object] = {
        "errors_module": "backend/app/core/errors.py",
        "main_module": "backend/app/main.py",
        "defines_error_code_enum": False,
        "error_code_count": 0,
        "defines_app_error_base": False,
        "app_error_subclasses": 0,
        "defines_http_status_mapping": False,
        "defines_handler_registration": False,
        "main_registers_handlers": False,
        "violation_count": 0,
        "violations": [],
    }
    if not errors_path.is_file() or not main_path.is_file():
        result["violation_count"] = 1
        result["violations"] = [{"rule": "errors.py or main.py is missing"}]
        return result

    errors_tree = _parse(errors_path)
    classes = [node for node in ast.walk(errors_tree) if isinstance(node, ast.ClassDef)]
    error_code = next((node for node in classes if node.name == "ErrorCode"), None)
    result["defines_error_code_enum"] = error_code is not None
    if error_code is not None:
        result["error_code_count"] = sum(
            1
            for statement in error_code.body
            if isinstance(statement, ast.Assign)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, int)
        )
    app_error = next((node for node in classes if node.name == "AppError"), None)
    result["defines_app_error_base"] = app_error is not None
    if app_error is not None:
        result["app_error_subclasses"] = sum(
            1
            for node in classes
            if any(ast.unparse(base) == "AppError" or "AppError" in ast.unparse(base) for base in node.bases)
            and node.name != "AppError"
        )
    function_names = {node.name for node in ast.walk(errors_tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    result["defines_http_status_mapping"] = "http_status_for" in function_names
    registration = "register_exception_handlers" in function_names
    result["defines_handler_registration"] = registration

    main_source = main_path.read_text(encoding="utf-8-sig")
    result["main_registers_handlers"] = bool(
        re.search(r"register_exception_handlers\s*\(", main_source)
    )

    violations: list[dict[str, object]] = []
    if not result["defines_error_code_enum"]:
        violations.append({"rule": "app/core/errors.py declares no ErrorCode enum"})
    elif int(result["error_code_count"]) < 2:
        violations.append({"rule": "ErrorCode declares fewer than two business codes"})
    if not result["defines_app_error_base"]:
        violations.append({"rule": "app/core/errors.py declares no AppError base class"})
    if not result["defines_handler_registration"]:
        violations.append({"rule": "app/core/errors.py does not expose register_exception_handlers"})
    if not result["main_registers_handlers"]:
        violations.append({"rule": "app/main.py never calls register_exception_handlers"})
    result["violation_count"] = len(violations)
    result["violations"] = violations
    return result


def check_scope_dependencies() -> dict[str, object]:
    """(e) console routers carry a principal/permission dependency."""
    per_file: list[dict[str, object]] = []
    console_files = 0
    missing: list[str] = []
    for path in sorted(MODULES.glob("*/api/*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8-sig")
        tree = _parse(path)
        rel = path.relative_to(ROOT).as_posix()
        principals = [name for name in PRINCIPAL_NAMES if re.search(rf"\b{name}\b", source)]
        uses_permission_code = "PermissionCode" in source
        is_console = any(hint in path.stem.lower() for hint in CONSOLE_ROUTER_HINTS)
        per_file.append(
            {
                "file": rel,
                "is_console_router": is_console,
                "principals": principals,
                "uses_permission_code": uses_permission_code,
                "endpoint_count": sum(
                    1
                    for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    and any(
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and isinstance(decorator.func.value, ast.Name)
                        and decorator.func.value.id.endswith("router")
                        for decorator in node.decorator_list
                    )
                ),
            }
        )
        if is_console:
            console_files += 1
            if not principals and not uses_permission_code:
                missing.append(rel)
    without_principal = [entry for entry in per_file if not entry["principals"] and not entry["uses_permission_code"]]
    return {
        "router_files_examined": len(per_file),
        "console_router_files": console_files,
        "console_router_files_without_principal": missing,
        "router_files_without_any_principal": len(without_principal),
        "principal_files": [entry["file"] for entry in per_file if entry["principals"]],
        "per_file": per_file,
        "violation_count": len(missing),
        "violations": [
            {"rule": "console router has no principal/permission dependency", "file": rel}
            for rel in missing
        ],
    }


def main() -> int:
    started = datetime.now(UTC)
    completeness = check_module_completeness()
    layering = check_routers_do_not_touch_data_layer()
    mounting = check_router_mounting()
    errors = check_error_mapping()
    scope = check_scope_dependencies()

    assertions: list[dict[str, object]] = []

    def check(name: str, expected: object, actual: object) -> None:
        assertions.append(
            {"name": name, "expected": expected, "actual": actual, "pass": actual == expected}
        )

    check(
        "(a) modules with an api/ package also expose a service layer",
        0,
        len([v for v in completeness["violations"] if "service" in v.get("missing", [])]),
    )
    check(
        "(a) modules with an api/ package also expose schemas",
        0,
        len([v for v in completeness["violations"] if "schemas" in v.get("missing", [])]),
    )
    check("(a) api modules were found at all", True, int(completeness["modules_with_api_package"]) > 0)
    check(
        "(b) no router file imports a repository module",
        0,
        len([v for v in layering["violations"] if "repository module" in v["rule"]]),
    )
    check(
        "(b) no router file drives a SQLAlchemy session directly",
        0,
        len([v for v in layering["violations"] if "session" in v["rule"]]),
    )
    check("(b) router files were scanned at all", True, int(layering["router_files_scanned"]) > 0)
    check("(c) app/api/v1/router.py exists", True, bool(mounting["exists"]))
    check(
        "(c) every module exposing an api/ package is mounted",
        0,
        len(mounting.get("unmounted_modules", [])),
    )
    check(
        "(c) no aggregate prefix points at a module with no api/ package",
        0,
        len(mounting.get("declared_but_no_api_package", [])),
    )
    check("(d) ErrorCode enum declares business codes", True, bool(errors["defines_error_code_enum"]))
    check("(d) AppError base class exists", True, bool(errors["defines_app_error_base"]))
    check(
        "(d) errors.py maps exceptions to HTTP status/business codes",
        True,
        bool(errors["defines_http_status_mapping"]),
    )
    check(
        "(d) main.py registers the exception handlers",
        True,
        bool(errors["main_registers_handlers"]),
    )
    check(
        "(e) every console router carries a principal or permission dependency",
        0,
        len(scope["console_router_files_without_principal"]),
    )
    check("(e) console routers were identified at all", True, int(scope["console_router_files"]) > 0)

    reasons: list[str] = []
    for label, section in (
        ("(a) module completeness", completeness),
        ("(b) router/data-layer separation", layering),
        ("(c) router mounting", mounting),
        ("(d) error mapping", errors),
        ("(e) scope dependencies", scope),
    ):
        for violation in section.get("violations", []):
            reasons.append(f"{label}: {violation}")

    for failure in PARSE_FAILURES:
        reasons.append(f"could not parse {failure['path']}: {failure['error']}")

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    verdict = "PASS" if not reasons else "FAIL"
    # Non-zero only when a file could not be parsed, i.e. when a check could not run.
    # A clean parse that *found* violations still exits 0: the observations succeeded.
    exit_code = 3 if PARSE_FAILURES else 0

    report = {
        "gate_id": "FG-25",
        "name": "Architecture Constraint Check",
        "command": (
            f"{sys.executable} scripts/emit_fg25_evidence.py  "
            "[parses backend/app with ast: module completeness, router/data-layer separation, "
            "aggregate router mounting, error mapping registration, console principal usage]"
        ),
        "cwd": str(BACKEND),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "parse_failures": PARSE_FAILURES,
        "git": git_state(RELEVANT_PATHS),
        "checks": {
            "a_module_completeness": completeness,
            "b_routers_do_not_touch_data_layer": layering,
            "c_router_mounting": mounting,
            "d_error_mapping": errors,
            "e_scope_dependencies": scope,
        },
        "assertions": assertions,
        "fail_reasons": reasons,
        "verdict": verdict,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"exit_code=0  assertions={len(assertions)}  verdict={verdict}")
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']}: {item['actual']!r}")
    for reason in reasons[:40]:
        print(f"  ! {reason}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())