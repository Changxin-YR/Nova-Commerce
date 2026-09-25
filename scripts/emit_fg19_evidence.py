"""Emit FG-19 evidence: object storage round-trip against real MinIO.

FG-19 is an infrastructure gate: the question is not whether the storage *port*
compiles, it is whether an object can be written, read back byte-identical and
removed against the MinIO instance declared in ``.env``
(``S3_ENDPOINT=http://127.0.0.1:19000``). So this emitter talks to that instance
with the real ``minio`` client and the real credentials, and records what each
call returned.

What is actually proven, end to end:

  1. bucket exists, or is created (and the creation is remembered, because a bucket
     this script created is a bucket this script may remove afterwards);
  2. ``put_object`` of a known payload succeeds;
  3. ``stat_object`` reports the same size the payload has;
  4. ``get_object`` streams back bytes whose SHA-256 equals the checksum computed
     locally *before* the upload, and whose MD5 equals the ETag S3 reported - the
     two independent integrity checks S3 actually offers;
  5. ``remove_object`` succeeds and the object is then genuinely gone (``stat_object``
     raises ``S3Error``).

Every value in the assertions below is a return value from a call made during this
run. The bucket is only removed if this script created it AND it is empty; an
existing shared bucket is left exactly as it was found.

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

import hashlib
import json
import pathlib
import socket
import sys
import time
import urllib.parse
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gate_evidence import ROOT, git_state
from minio import Minio
from minio.error import S3Error

OUT = ROOT / "artifacts" / "evidence" / "storage" / "fg19_storage.json"

#: Explicitly test-scoped names, so a real bucket holding real objects is never
#: touched and a failure leaves an obviously-test artefact behind rather than
#: something an operator might mistake for production data.
TEST_BUCKET = "nova-fg19-evidence"
RUN_ID = f"{int(time.time())}-{hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:8]}"
OBJECT_KEY = f"final-gate/fg19/{RUN_ID}/roundtrip.bin"
PAYLOAD = (
    b"nova-fg19-object-storage-roundtrip\n"
    + bytes(range(256))
    + b"\nfg19 payload tail - written by scripts/emit_fg19_evidence.py\n"
)

RELEVANT_PATHS = (
    "scripts/emit_fg19_evidence.py",
    "scripts/gate_evidence.py",
    "backend/app/shared/storage",
    ".env.example",
    "ops/docker-compose.yml",
)


def _settings_from_env_file() -> dict[str, str]:
    """Read the S3 settings the application itself reads.

    The endpoint/credentials are parsed out of the real ``.env`` rather than
    duplicated here, so this gate cannot drift from the deployment it claims to
    describe. The file is read, never written.
    """
    values: dict[str, str] = {}
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return values
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _tcp_reachable(endpoint: str) -> tuple[bool, str, int]:
    parsed = urllib.parse.urlparse(endpoint)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 9000
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(5.0)
        code = probe.connect_ex((host, port))
    return code == 0, f"tcp {host}:{port} connect_ex={code}", code


def main() -> int:
    started = datetime.now(UTC)
    settings = _settings_from_env_file()
    endpoint = settings.get("S3_ENDPOINT", "")
    access_key = settings.get("S3_ACCESS_KEY", "")
    secret_key = settings.get("S3_SECRET_KEY", "")
    secure = endpoint.startswith("https://")
    host = endpoint.split("://", 1)[-1]

    expected_sha256 = hashlib.sha256(PAYLOAD).hexdigest()
    # MD5 here is not a security decision: it is the checksum semantics of an S3 ETag
    # for a single-part upload, which is what the assertion below compares against.
    expected_md5 = hashlib.md5(PAYLOAD).hexdigest()
    expected_size = len(PAYLOAD)

    observations: dict[str, object] = {
        "endpoint": endpoint,
        "access_key": access_key,
        "bucket": TEST_BUCKET,
        "object_key": OBJECT_KEY,
        "payload_size": expected_size,
        "expected_sha256": expected_sha256,
        "expected_md5": expected_md5,
        "tcp_reachable": None,
        "bucket_existed": None,
        "bucket_created": False,
        "bucket_removed": False,
        "put_succeeded": False,
        "stat_size": None,
        "stat_etag": None,
        "stat_content_type": None,
        "readback_size": None,
        "readback_sha256": None,
        "readback_md5": None,
        "remove_succeeded": False,
        "stat_after_remove_raised": None,
        "stat_after_remove_error": None,
        "errors": [],
    }

    #: Exit codes of the calls this run actually made. The gate reports these rather
    #: than a constant: if MinIO is unreachable the recorded exit code is the socket
    #: error, not a tidy zero that would misrepresent a call nobody managed to make.
    call_exit_codes: dict[str, int] = {}

    if endpoint:
        reachable, tcp_detail, tcp_code = _tcp_reachable(endpoint)
    else:
        reachable, tcp_detail, tcp_code = False, "S3_ENDPOINT missing", 4
    call_exit_codes["tcp_connect"] = tcp_code
    observations["tcp_reachable"] = reachable
    observations["tcp_detail"] = tcp_detail

    client = Minio(host, access_key=access_key, secret_key=secret_key, secure=secure)

    if reachable:
        try:
            observations["bucket_existed"] = client.bucket_exists(TEST_BUCKET)
            if not observations["bucket_existed"]:
                client.make_bucket(TEST_BUCKET)
                observations["bucket_created"] = True
                observations["bucket_existed_after_create"] = client.bucket_exists(TEST_BUCKET)
            call_exit_codes["bucket_setup"] = 0
        except S3Error as exc:
            observations["errors"].append(f"bucket setup: {exc.code}: {exc.message}")
            call_exit_codes["bucket_setup"] = 1
        except OSError as exc:
            observations["errors"].append(f"bucket setup: {type(exc).__name__}: {exc}")
            call_exit_codes["bucket_setup"] = 2

        if observations["bucket_existed"] is not None and not observations["errors"]:
            from io import BytesIO

            try:
                client.put_object(
                    TEST_BUCKET,
                    OBJECT_KEY,
                    BytesIO(PAYLOAD),
                    length=expected_size,
                    content_type="application/octet-stream",
                )
                observations["put_succeeded"] = True
                call_exit_codes["put_object"] = 0
            except (S3Error, OSError) as exc:
                observations["errors"].append(f"put_object: {type(exc).__name__}: {exc}")
                call_exit_codes["put_object"] = 1

        if observations["put_succeeded"]:
            try:
                stat = client.stat_object(TEST_BUCKET, OBJECT_KEY)
                observations["stat_size"] = stat.size
                observations["stat_etag"] = stat.etag
                observations["stat_content_type"] = stat.content_type
                call_exit_codes["stat_object"] = 0
            except (S3Error, OSError) as exc:
                observations["errors"].append(f"stat_object: {type(exc).__name__}: {exc}")
                call_exit_codes["stat_object"] = 1

            response = None
            try:
                response = client.get_object(TEST_BUCKET, OBJECT_KEY)
                data = response.read()
                observations["readback_size"] = len(data)
                observations["readback_sha256"] = hashlib.sha256(data).hexdigest()
                observations["readback_md5"] = hashlib.md5(data).hexdigest()
                observations["readback_matches_payload"] = data == PAYLOAD
                call_exit_codes["get_object"] = 0
            except (S3Error, OSError) as exc:
                observations["errors"].append(f"get_object: {type(exc).__name__}: {exc}")
                call_exit_codes["get_object"] = 1
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()

            try:
                client.remove_object(TEST_BUCKET, OBJECT_KEY)
                observations["remove_succeeded"] = True
                call_exit_codes["remove_object"] = 0
            except (S3Error, OSError) as exc:
                observations["errors"].append(f"remove_object: {type(exc).__name__}: {exc}")
                call_exit_codes["remove_object"] = 1

            try:
                client.stat_object(TEST_BUCKET, OBJECT_KEY)
                observations["stat_after_remove_raised"] = False
            except S3Error as exc:
                observations["stat_after_remove_raised"] = True
                observations["stat_after_remove_error"] = f"{exc.code}"
                call_exit_codes["stat_after_remove"] = 0
            except OSError as exc:
                observations["stat_after_remove_raised"] = True
                observations["stat_after_remove_error"] = type(exc).__name__
                call_exit_codes["stat_after_remove"] = 2

        # Only a bucket this run created, and only if it is empty, is removed.
        if observations["bucket_created"] and observations["remove_succeeded"]:
            try:
                remaining = list(client.list_objects(TEST_BUCKET, recursive=True))
                observations["objects_left_in_created_bucket"] = len(remaining)
                if not remaining:
                    client.remove_bucket(TEST_BUCKET)
                    observations["bucket_removed"] = True
            except (S3Error, OSError) as exc:
                observations["errors"].append(f"cleanup: {type(exc).__name__}: {exc}")

    etag = str(observations["stat_etag"] or "").strip('"')
    assertions: list[dict[str, object]] = []

    def check(name: str, expected: object, actual: object) -> None:
        assertions.append(
            {"name": name, "expected": expected, "actual": actual, "pass": actual == expected}
        )

    check("S3 endpoint declared in .env", True, bool(endpoint))
    check("S3 endpoint accepts TCP connections", True, reachable)
    check("test bucket available (existing or created)", True, observations["bucket_existed"] is True or observations["bucket_created"] is True)
    check("put_object succeeded", True, observations["put_succeeded"])
    check("stat_object reported the uploaded size", expected_size, observations["stat_size"])
    check("stat_object ETag equals the payload MD5", expected_md5, etag)
    check("get_object returned the same number of bytes", expected_size, observations["readback_size"])
    check("readback SHA-256 equals the pre-upload checksum", expected_sha256, observations["readback_sha256"])
    check("readback MD5 equals the pre-upload checksum", expected_md5, observations["readback_md5"])
    check("readback bytes are identical to the uploaded payload", True, observations["readback_matches_payload"])
    check("remove_object succeeded", True, observations["remove_succeeded"])
    check("object is gone after removal (stat_object raised S3Error)", True, observations["stat_after_remove_raised"])
    check("test bucket created by this run was cleaned up or left empty", True, _cleanup_ok(observations))

    reasons: list[str] = []
    if not endpoint:
        reasons.append("S3_ENDPOINT is not set in .env")
    if not reachable:
        reasons.append(f"MinIO endpoint not reachable: {tcp_detail}")
    for failure in assertions:
        if not failure["pass"]:
            reasons.append(f"{failure['name']}: expected {failure['expected']!r}, got {failure['actual']!r}")
    reasons.extend(observations["errors"])

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    verdict = "PASS" if not reasons else "FAIL"
    # The worst exit code any observed call produced, not a literal. A clean run is 0;
    # an unreachable endpoint contributes its own socket error.
    exit_code = max(call_exit_codes.values(), default=1)
    observations["call_exit_codes"] = call_exit_codes

    report = {
        "gate_id": "FG-19",
        "name": "Object Storage",
        "command": (
            f"{sys.executable} scripts/emit_fg19_evidence.py  "
            "[real minio client round-trip: bucket_exists/make_bucket, put_object, "
            "stat_object, get_object, remove_object, stat_object]"
        ),
        "cwd": str(ROOT),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "git": git_state(RELEVANT_PATHS),
        "infrastructure": {
            "engine": "MinIO (real container nx-minio on 127.0.0.1:19000, not mocked)",
            "why_real": (
                "Checksum, ETag and delete-visibility semantics are server behaviour. "
                "A memory-backed fake would prove only that this script can call itself."
            ),
        },
        "observations": observations,
        "assertions": assertions,
        "fail_reasons": reasons,
        "verdict": verdict,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"exit_code={exit_code}  assertions={len(assertions)}  verdict={verdict}")
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']}: {item['actual']!r}")
    for reason in reasons:
        print(f"  ! {reason}")
    return 0 if verdict == "PASS" else 1


def _cleanup_ok(observations: dict[str, object]) -> bool:
    if observations["bucket_created"]:
        return observations.get("bucket_removed") is True or observations.get(
            "objects_left_in_created_bucket"
        ) == 0
    return observations["bucket_existed"] is True


if __name__ == "__main__":
    raise SystemExit(main())