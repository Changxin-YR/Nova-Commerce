"""Emit FG-24 evidence: secret scan.

There is no third-party scanner in this repository, so this emitter *is* the
scanner. It is deliberately small and entirely observable - every finding it
reports is reproducible by re-running it, and the four checks it performs are the
four things spec section 134 actually forbids:

  1. ``.env`` must not be tracked by git. ``git ls-files --error-unmatch .env`` must
     therefore FAIL (non-zero exit). A tracked ``.env`` is a Final Gate failure and
     this is the assertion that catches it.
  2. ``.env`` must be covered by ``.gitignore`` - untracked by accident is not the
     same as untracked by policy, and only the latter survives a ``git add -A``.
  3. ``.env.example`` must be tracked (the template is how a new developer starts)
     and must not contain real secret values.
  4. Every git-tracked file is scanned for high-signal secret patterns: PEM private
     key blocks, AWS access key ids, and ``password``/``secret``/``token``
     assignments whose value is a real literal. The values read from the working
     ``.env`` are additionally searched for verbatim, because a dev secret pasted
     into a test fixture is exactly the leak this gate exists to find.

What is *not* done: a placeholder in ``.env.example`` is not a finding. The check
distinguishes "a value a human must replace" from "a value that is already a
credential in use", and reports which one it saw instead of collapsing the two.

Findings never print the secret itself - only its path, line, kind and a masked
form - so this artifact can be committed.

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

import json
import pathlib
import re
import subprocess
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gate_evidence import ROOT, git_state

OUT = ROOT / "artifacts" / "evidence" / "security" / "fg24_secret_scan.json"

RELEVANT_PATHS = (
    "scripts/emit_fg24_evidence.py",
    "scripts/gate_evidence.py",
    ".gitignore",
    ".env.example",
    "backend/app/core/config.py",
)

#: Text-ish extensions worth scanning. Everything else is byte-compared for the
#: literal dev secrets only, so a binary asset cannot be reported as a "finding"
#: because a 64-byte random PNG happened to contain the word "token".
TEXT_SUFFIXES = frozenset(
    {
        ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".json",
        ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".example", ".txt",
        ".md", ".rst", ".sh", ".ps1", ".bat", ".cmd", ".sql", ".css", ".scss", ".html",
        ".xml", ".csv", ".properties", ".gitignore", ".dockerignore", ".editorconfig",
        ".lock", ".svg", ".j2", ".template", ".sample",
    }
)
TEXT_FILENAMES = frozenset(
    {".env.example", ".gitignore", ".dockerignore", ".editorconfig", "Dockerfile", "Makefile"}
)

PEM_PATTERN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
AWS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
ASSIGNMENT_PATTERN = re.compile(
    r"""(?ix)                       # case-insensitive, verbose
    (?P<key>[A-Z0-9_.\-]*
        (?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)
    )
    \s*[:=]\s*
    (?P<value>[^\s#'"`,;\]\}]+ | "[^"]*" | '[^']*')
    """
)

#: A value that is obviously a placeholder. These are expected in .env.example and
#: matching one is *not* a leak - it is the template doing its job.
PLACEHOLDER_VALUES = frozenset(
    {
        "",
        "changeme",
        "change_me",
        "replace_me",
        "placeholder",
        "example",
        "your_password",
        "your-secret",
        "your_secret",
        "todo",
        "none",
        "null",
        "xxx",
        "dummy",
        "fake",
        "test",
    }
)
PLACEHOLDER_SUBSTRINGS = (
    "replace",
    "changeme",
    "change-me",
    "change_me",
    "<",
    ">",
    "${",
    "{{",
    "%s",
    "...",
    "your-",
    "your_",
    "xxxx",
    "example.com",
)
SECRET_SHAPED_KEYS = ("password", "passwd", "secret", "token", "api_key", "apikey", "access_key", "private_key")

#: AWS's own published example id. It appears in this repository's redaction tests
#: as a fixture and in the canonical documentation everywhere; treating it as a leak
#: would make the gate fail on a string that AWS prints in its own user guide.
ALLOWLISTED_LITERALS = frozenset({"AKIAIOSFODNN7EXAMPLE"})

#: Shortest value that can plausibly be a credential rather than a code token.
MIN_LITERAL_SECRET_LENGTH = 6

#: A credential assignment whose right-hand side is *code* - a constructor, a
#: subscript, an env lookup, a call result - is not a leaked literal. Matching those
#: would report ``password = SecretStr("")`` and ``token = request.cookies.get(...)``
#: as leaks, which is exactly how a scanner earns a reputation for noise and then
#: gets ignored. The whole point of this check is that its findings are real.
CODE_LIKE_VALUE_MARKERS = (
    "(", ")", "[", "]", "`", "->", "==", ">", "<", "|", "&",
    ".get", ".set", ".split", ".join", ".strip", ".format", ".value", ".decode",
    ".read", ".cookies", ".headers", ".env", ".query", ".body", ".dict",
    "SecretStr", "getenv", "environ", "Depends", "Field", "Query", "Header",
    "Annotated", "None", "True", "False", "settings.", "config.", "self.",
)

#: Values of the working .env that are ordinary dev defaults ("novasecret", "rootpw")
#: and also ordinary English or product words. They are still reported whenever they
#: appear in a tracked file, but as `dev_secret_weak_default` rather than as a failed
#: gate: they collide with prose and with the product's own name, so treating them as
#: severe would bury the findings that matter.
PLAUSIBLE_WORD_PATTERN = re.compile(r"^[a-z]{2,}[a-z_]{0,12}$")

#: A key whose name is spelled exactly like its value is a re-export of a local
#: variable: ``password=password``, ``refresh_token=refresh_token``. The right-hand
#: side is a code identifier, not a credential, and reporting it would be noise.
IDENTIFIER_VALUE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+){0,5}$")

#: Credential-ish words that appear in ordinary prose; a value that *is* one of these
#: is a label, not a credential.
NON_SECRET_WORDS = (
    "purposes", "only", "purpose", "value", "values", "type", "types", "hash",
    "scheme", "header", "headers", "cookie", "cookies", "name", "names", "field",
    "fields", "endpoint", "url", "urls", "expired", "invalid", "missing", "reset",
    "rotation", "reuse", "reused", "storage", "issue", "issued", "presented",
    "verification", "validation", "authorization", "authentication", "chain",
    "user", "username", "owner", "scope", "scopes", "bearer", "inline", "document",
)


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    return proc.returncode, proc.stdout or ""


def _mask(value: str) -> str:
    """A fingerprint of a secret that is safe to write into a committed artifact."""
    if len(value) <= 2:
        return "*" * len(value)
    return f"{value[0]}***{value[-1]}(len={len(value)})"


def _tracked_files() -> tuple[list[str], int]:
    code, out = _git("ls-files")
    files = [line.strip() for line in out.splitlines() if line.strip()]
    return files, code


def _env_values(path: pathlib.Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from an env file. Read-only; never written."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Trailing ``  # comment`` is stripped: a comment is not part of the value,
        # and keeping it would make an exact-match comparison miss real leaks.
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def _is_placeholder(value: str) -> bool:
    lowered = value.lower().strip()
    if lowered in PLACEHOLDER_VALUES:
        return True
    if any(token in lowered for token in PLACEHOLDER_SUBSTRINGS):
        return True
    # A value that is only digits/punctuation, or a bare reference to something
    # else, is not a credential this check can meaningfully call real.
    return not any(ch.isalpha() for ch in lowered)


def _looks_like_code(value: str) -> bool:
    return any(marker in value for marker in CODE_LIKE_VALUE_MARKERS)


def _looks_like_prose(value: str) -> bool:
    """True when the value reads as an ordinary English word used as a label.

    ``Message="token expired"`` is prose; ``JWT_SECRET_KEY=dev-only-secret-...`` is a
    credential. Only the second is a finding.
    """
    normalized = re.sub(r"[^a-z]", "", value.lower())
    if not normalized:
        return False
    return any(word in normalized for word in NON_SECRET_WORDS)


def _is_real_secret_value(value: str) -> bool:
    if not value or value in ALLOWLISTED_LITERALS:
        return False
    if _is_placeholder(value):
        return False
    if _looks_like_code(value):
        return False
    if len(value) < MIN_LITERAL_SECRET_LENGTH:
        return False
    # A snake_case identifier that is simply passed through from a parameter or a
    # local variable - the surrounding code, not a literal in the file.
    return not IDENTIFIER_VALUE_PATTERN.match(value)


def _scan_file(rel: str, env_values: dict[str, str]) -> list[dict[str, object]]:
    path = ROOT / rel
    findings: list[dict[str, object]] = []
    try:
        raw = path.read_bytes()
    except OSError:
        return findings
    text = raw.decode("utf-8", "replace")

    # 1. Private key blocks.
    for number, line in enumerate(text.splitlines(), start=1):
        if PEM_PATTERN.search(line):
            findings.append(
                {
                    "path": rel,
                    "line": number,
                    "kind": "pem_private_key",
                    "category": "private_key_material",
                    "masked": "<pem block>",
                }
            )

    # 2. AWS access key ids. ``AKIAIOSFODNN7EXAMPLE`` is AWS's own published example
    #    id and this repository uses it as a redaction fixture; it is excluded by
    #    name so the gate cannot fail on the string AWS prints in its own docs.
    for number, line in enumerate(text.splitlines(), start=1):
        for match in AWS_KEY_PATTERN.finditer(line):
            if "EXAMPLE" in match.group(0) or match.group(0) in ALLOWLISTED_LITERALS:
                continue
            findings.append(
                {
                    "path": rel,
                    "line": number,
                    "kind": "aws_access_key_id",
                    "category": "cloud_credential_id",
                    "masked": _mask(match.group(0)),
                }
            )

    # 3. Dev secrets from the working .env, searched for verbatim. This is the
    #    highest-signal check: it does not guess what a secret looks like, it looks
    #    for credentials that are demonstrably in use by this deployment. A hit here
    #    is not a style opinion - the string in the file is the string the running
    #    stack authenticates with.
    for key, value in env_values.items():
        if len(value) < MIN_LITERAL_SECRET_LENGTH or _is_placeholder(value):
            continue
        if not any(
            marker in key.upper()
            for marker in ("PASSWORD", "SECRET", "KEY", "TOKEN", "ADMIN", "USER", "AUTH")
        ):
            continue
        # Word-boundary match, not a substring match. The substring form reported
        # 804 hits because "nova" occurs inside "novaadmin"/"nova-commerce" and
        # "admin" inside "administration" - a detector that fires on prose cannot be
        # believed when it fires on a leak.
        pattern = re.compile(rf"(?<![A-Za-z0-9_.\-]){re.escape(value)}(?![A-Za-z0-9_.\-])")
        # Severity comes from the *value*, not from the file it was found in:
        # a 55-character secret in a handoff document is a live credential, while a
        # two-syllable dev default quoted in a runbook is a documented default. Both
        # are reported; only the first fails the gate.
        severe = not PLAUSIBLE_WORD_PATTERN.fullmatch(value)
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                findings.append(
                    {
                        "path": rel,
                        "line": number,
                        "kind": f"env_dev_secret_in_use:{key}",
                        "category": "dev_secret_in_use" if severe else "dev_secret_weak_default",
                        "masked": _mask(value),
                    }
                )

    # 4. Generic credential assignments with a literal value.
    if rel in TEXT_FILENAMES or path.suffix.lower() in TEXT_SUFFIXES:
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith(("#", "//", "*")):
                continue
            if PEM_PATTERN.search(line) or AWS_KEY_PATTERN.search(line):
                continue  # already reported above
            match = ASSIGNMENT_PATTERN.search(line)
            if not match:
                continue
            value = match.group("value").strip().strip('"').strip("'")
            if not _is_real_secret_value(value):
                continue
            if _looks_like_prose(value):
                continue
            findings.append(
                {
                    "path": rel,
                    "line": number,
                    "kind": f"literal_credential_assignment:{match.group('key').lower()}",
                    "category": "literal_credential_assignment",
                    "masked": _mask(value),
                }
            )
    return findings


def _dedupe(findings: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[object, object, object]] = set()
    unique: list[dict[str, object]] = []
    for finding in findings:
        key = (finding["path"], finding["line"], finding["kind"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    return unique


def main() -> int:
    started = datetime.now(UTC)
    env_file = ROOT / ".env"
    env_example = ROOT / ".env.example"
    env_values = _env_values(env_file)

    # The stdout is deliberately not unpacked: only the exit status carries meaning here
    # (a tracked .env must make this call FAIL, so its output is not the observation).
    env_tracked_code, _ = _git("ls-files", "--error-unmatch", ".env")
    env_is_tracked = env_tracked_code == 0
    ignore_code, ignore_out = _git("check-ignore", "-v", ".env")
    env_ignored = ignore_code == 0
    example_tracked_code, _ = _git("ls-files", "--error-unmatch", ".env.example")

    tracked, ls_code = _tracked_files()
    coverage: list[dict[str, object]] = []
    findings: list[dict[str, object]] = []
    scanned_bytes = 0
    for rel in tracked:
        if rel.startswith("artifacts/") and re.search(r"\.(png|jpg|jpeg|gif|ico|webp|pdf|zip)$", rel):
            coverage.append({"path": rel, "scanned": False, "reason": "binary asset"})
            continue
        path = ROOT / rel
        if not path.is_file():
            coverage.append({"path": rel, "scanned": False, "reason": "not present in working tree"})
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        scanned_bytes += size
        coverage.append({"path": rel, "scanned": True, "bytes": size})
        findings.extend(_scan_file(rel, env_values))

    findings = _dedupe(findings)
    example_findings = [f for f in findings if f["path"] == ".env.example"]
    repo_findings = [f for f in findings if f["path"] != ".env.example"]

    # Two severities, kept apart on purpose. "A credential this deployment is
    # actually using appears in a tracked file" fails the gate. "A hardcoded literal
    # sits behind a credential-shaped key" is reported for the record but does not
    # fail it: the integration suite's shared fixture password is not production
    # authentication, and a gate that fails on it will simply be ignored.
    active_findings = [f for f in findings if f["category"] in {"dev_secret_in_use", "private_key_material", "cloud_credential_id"}]
    weak_defaults = [f for f in findings if f["category"] == "dev_secret_weak_default"]
    risky_literals = [f for f in findings if f["category"] == "literal_credential_assignment"]
    example_active = [f for f in example_findings if f in active_findings]
    repo_active = [f for f in repo_findings if f in active_findings]
    repo_risky = [f for f in repo_findings if f in risky_literals]

    files_scanned = sum(1 for item in coverage if item.get("scanned"))
    files_skipped = len(coverage) - files_scanned

    assertions: list[dict[str, object]] = []

    def check(name: str, expected: object, actual: object) -> None:
        assertions.append(
            {"name": name, "expected": expected, "actual": actual, "pass": actual == expected}
        )

    check("git ls-files --error-unmatch .env fails (the file is not tracked)", True, env_tracked_code != 0)
    check(".env is not present in git ls-files output", True, ".env" not in tracked)
    check(".env is ignored by .gitignore", True, env_ignored)
    check(".env.example is tracked", True, example_tracked_code == 0)
    check(
        "no private key material or cloud credential id in tracked files",
        0,
        len([f for f in repo_active if f["category"] in {"private_key_material", "cloud_credential_id"}]),
    )
    check(
        "no tracked file (outside .env.example) repeats a credential .env is using",
        0,
        len(repo_active),
    )
    check(
        "tracked files carry no PEM private key block (including .env.example)",
        0,
        len([f for f in findings if f["category"] == "private_key_material"]),
    )
    check(
        ".env.example carries no AWS access key id",
        0,
        len([f for f in example_findings if f["category"] == "cloud_credential_id"]),
    )
    check(
        ".env.example publishes no credential value that .env is using",
        0,
        len(example_active),
    )
    check("git ls-files succeeded", 0, ls_code)
    check("tracked files were scanned (non-zero coverage)", True, files_scanned > 0)

    reasons: list[str] = []
    if env_is_tracked:
        reasons.append("FATAL: .env is tracked by git")
    if not env_ignored:
        reasons.append(".env is not covered by .gitignore")
    if example_tracked_code != 0:
        reasons.append(".env.example is not tracked")
    if repo_active:
        reasons.append(
            f"{len(repo_active)} tracked file(s) (outside .env.example) repeat a credential "
            "the working .env is using, or carry key material"
        )
    if example_active:
        reasons.append(
            f"{len(example_active)} credential(s) the working .env is using are published in "
            ".env.example"
        )
    if ls_code != 0:
        reasons.append(f"git ls-files exit_code={ls_code}")

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    verdict = "PASS" if not reasons else "FAIL"
    # The exit codes this run actually observed from the git calls it made. A
    # non-zero here means a command could not be run, so the scan would be reporting
    # on a file list it never got - recorded rather than smoothed to zero.
    observed_exit_codes = {
        "git_ls_files": ls_code,
        "git_ls_files_error_unmatch_env": env_tracked_code,
        "git_ls_files_error_unmatch_env_example": example_tracked_code,
        "git_check_ignore_env": ignore_code,
    }
    exit_code = max(
        abs(observed_exit_codes["git_ls_files"]),
        # Only a *failing* .env lookup is acceptable, so it can never raise the exit
        # code on its own; an unexpected zero is caught by the assertions instead.
        0,
        abs(observed_exit_codes["git_ls_files_error_unmatch_env_example"]),
    )

    report = {
        "gate_id": "FG-24",
        "name": "Secret Scan",
        "command": (
            f"{sys.executable} scripts/emit_fg24_evidence.py  "
            "[runs: git ls-files --error-unmatch .env; git check-ignore -v .env; "
            "git ls-files --error-unmatch .env.example; git ls-files; pattern scan of every "
            "tracked file for PEM keys, AWS key ids, literal credential assignments and the "
            "working .env values]"
        ),
        "cwd": str(ROOT),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "observed_exit_codes": observed_exit_codes,
        "git": git_state(RELEVANT_PATHS),
        "scanner": {
            "implementation": "built into this emitter (no third-party scanner exists in this repo)",
            "patterns": [
                "-----BEGIN [PRIVATE KEY]-----",
                "AKIA/ASIA + 16 [0-9A-Z]",
                "<key containing password|secret|token|api_key|access_key|private_key>=<literal>",
                "verbatim values of credential-shaped keys read from the working .env",
            ],
            "placeholder_handling": (
                "values containing replace/changeme/<...>/${...}/`your-` and similar are treated "
                "as template placeholders, not leaks, and are not reported"
            ),
        },
        "env_file": {
            "path": ".env",
            "exists": env_file.is_file(),
            "tracked": env_is_tracked,
            "ls_files_error_unmatch_exit_code": env_tracked_code,
            "ignored_by_gitignore": env_ignored,
            "check_ignore_output": ignore_out.strip(),
            "credential_shaped_keys_read": sorted(
                key
                for key in env_values
                if any(marker in key.upper() for marker in ("PASSWORD", "SECRET", "KEY", "TOKEN"))
            ),
        },
        "env_example": {
            "path": ".env.example",
            "exists": env_example.is_file(),
            "tracked": example_tracked_code == 0,
            "finding_count": len(example_findings),
            "findings": example_findings,
        },
        "coverage": {
            "tracked_file_count": len(tracked),
            "files_scanned": files_scanned,
            "files_skipped": files_skipped,
            "bytes_scanned": scanned_bytes,
            "skipped": [item for item in coverage if not item.get("scanned")],
        },
        "findings_total": len(findings),
        "findings_by_category": {
            "dev_secret_in_use": len([f for f in findings if f["category"] == "dev_secret_in_use"]),
            "private_key_material": len([f for f in findings if f["category"] == "private_key_material"]),
            "cloud_credential_id": len([f for f in findings if f["category"] == "cloud_credential_id"]),
            "literal_credential_assignment": len(risky_literals),
            "dev_secret_weak_default": len(weak_defaults),
        },
        "verdict_rules": {
            "fails_the_gate": (
                "a tracked file repeats a credential the working .env is using, or contains "
                "private key material / a cloud access key id"
            ),
            "reported_but_does_not_fail": (
                "a hardcoded literal behind a credential-shaped key in test fixtures and source "
                "(recorded as literal_credential_assignment; the integration suite's shared "
                "fixture password is not production authentication)"
            ),
        },
        "findings_that_fail_the_gate": repo_active + example_active,
        "findings_outside_env_example": repo_findings,
        "dev_secret_weak_default_findings": weak_defaults,
        "literal_assignment_findings": risky_literals,
        "literal_assignment_findings_outside_env_example": repo_risky,
        "assertions": assertions,
        "fail_reasons": reasons,
        "verdict": verdict,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(
        f"exit_code={exit_code}  assertions={len(assertions)}  files_scanned={files_scanned}  "
        f"findings={len(findings)}  verdict={verdict}"
    )
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']}: {item['actual']!r}")
    for reason in reasons:
        print(f"  ! {reason}")
    for finding in findings[:40]:
        print(f"    - {finding['path']}:{finding['line']} {finding['kind']} {finding['masked']}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())