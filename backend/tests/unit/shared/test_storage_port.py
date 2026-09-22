"""Object storage boundary security.

Spec §109 lists path traversal and file-upload spoofing; §122 defines the storage
gate (invalid MIME, path traversal, oversized file, missing object, checksum);
§52 requires MIME/extension/**signature** validation before anything is parsed.

These are pure-logic tests against the port's validators, so they need no
container. FG-19 additionally repeats the same attacks against real MinIO,
because "my validator rejects it" and "the system rejects it" are different
claims.
"""

from __future__ import annotations

import pytest

from app.shared.storage.port import (
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    StorageUnsafeKeyError,
    StorageUnsupportedContentTypeError,
    build_object_key,
    sanitise_filename,
    sha256_hex,
    sniff_content_type,
    validate_object_key,
    validate_upload,
)

# ---------------------------------------------------------------------------
# Path traversal (§109, §122)
# ---------------------------------------------------------------------------
TRAVERSAL_KEYS = [
    "../etc/passwd",
    "a/../../etc/passwd",
    "objects/../../secret",
    "/absolute/path",
    "C:/Windows/System32/config",
    "C:\\Windows\\win.ini",
    "objects/..%2f..%2fetc",
    "objects/%2e%2e/%2e%2e/etc",
    "objects/%252e%252e/etc",
    "objects//double",
    "objects/./dot",
    "",
    "a" * 600,
    "objects/nul\x00byte",
    "~/home/secret",
]


@pytest.mark.parametrize("key", TRAVERSAL_KEYS)
def test_unsafe_object_keys_are_rejected(key: str) -> None:
    with pytest.raises(StorageUnsafeKeyError):
        validate_object_key(key)


SAFE_KEYS = [
    "products/1/hero.png",
    "knowledge/42/manual-v1.pdf",
    "reports/2026/09/sales.csv",
    "objects/a-b_c.d.txt",
]


@pytest.mark.parametrize("key", SAFE_KEYS)
def test_safe_object_keys_are_accepted(key: str) -> None:
    assert validate_object_key(key) == key


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\win.ini", "win.ini"),
        ("C:\\temp\\report.pdf", "report.pdf"),
        ("/var/tmp/a b.png", "a_b.png"),
        ("", "upload"),
        ("..", "upload"),
        (".hidden", "hidden"),
        ("日本語マニュアル.pdf", "日本語マニュアル.pdf"),
    ],
)
def test_filenames_are_reduced_to_a_single_safe_segment(filename: str, expected: str) -> None:
    assert sanitise_filename(filename) == expected


def test_full_width_characters_cannot_smuggle_a_traversal() -> None:
    """NFKC normalisation happens first, or a full-width dot survives the filter.

    The full-width characters below are the *subject* of the test, not a typo, so
    ruff's ambiguity rule (RUF001) is suppressed on exactly these two lines.
    Without NFKC normalisation a full-width solidus reaches the backend
    unchanged and a downstream filesystem may treat it as a separator.
    """
    assert "/" not in sanitise_filename("．．／etc／passwd")  # noqa: RUF001
    assert sanitise_filename("ａ．ｐｎｇ") == "a.png"  # noqa: RUF001


def test_build_object_key_composes_safely() -> None:
    key = build_object_key(prefix="products", filename="../../x.png", owner_id=7, unique="abc")
    assert key.startswith("products/7/")
    assert ".." not in key
    assert validate_object_key(key) == key


# ---------------------------------------------------------------------------
# Content-type allow-list and signature agreement (§52, §122)
# ---------------------------------------------------------------------------
def test_declared_type_must_be_on_the_allow_list() -> None:
    with pytest.raises(StorageUnsupportedContentTypeError):
        validate_upload(
            filename="payload.exe",
            declared_content_type="application/x-msdownload",
            head=b"MZ\x90\x00",
            size=100,
            max_bytes=10_000,
        )


def test_signature_must_agree_with_the_declaration() -> None:
    """The classic spoof: a zip bomb named as a PDF."""
    with pytest.raises(StorageUnsupportedContentTypeError):
        validate_upload(
            filename="invoice.pdf",
            declared_content_type="application/pdf",
            head=b"PK\x03\x04",
            size=500,
            max_bytes=10_000,
        )


def test_png_declared_as_jpeg_is_rejected() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    with pytest.raises(StorageUnsupportedContentTypeError):
        validate_upload(
            filename="x.jpg",
            declared_content_type="image/jpeg",
            head=png,
            size=len(png),
            max_bytes=10_000,
        )


def test_matching_signature_is_accepted() -> None:
    pdf = b"%PDF-1.7\n" + b"\x00" * 50
    resolved = validate_upload(
        filename="policy.pdf",
        declared_content_type="application/pdf",
        head=pdf,
        size=len(pdf),
        max_bytes=10_000,
    )
    assert resolved == "application/pdf"


def test_ooxml_zip_container_is_accepted_as_its_declared_type() -> None:
    """docx/xlsx genuinely are zip files, so the zip signature must not be a
    false conflict."""
    docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    resolved = validate_upload(
        filename="manual.docx",
        declared_content_type=docx_type,
        head=b"PK\x03\x04\x14\x00",
        size=2048,
        max_bytes=10_000,
    )
    assert resolved == docx_type


def test_oversized_upload_is_rejected_before_buffering() -> None:
    with pytest.raises(StorageObjectTooLargeError):
        validate_upload(
            filename="big.pdf",
            declared_content_type="application/pdf",
            head=b"%PDF-1.7",
            size=50_000_000,
            max_bytes=1_000_000,
        )


def test_empty_upload_is_rejected() -> None:
    with pytest.raises(StorageUnsupportedContentTypeError):
        validate_upload(
            filename="empty.pdf",
            declared_content_type="application/pdf",
            head=b"",
            size=0,
            max_bytes=1000,
        )


def test_unknown_declared_type_falls_back_to_the_filename_extension() -> None:
    """Browsers send application/octet-stream constantly; a plausible extension
    is strictly better than rejecting a legitimate upload."""
    resolved = validate_upload(
        filename="spec.pdf",
        declared_content_type="application/octet-stream",
        head=b"%PDF-1.7",
        size=100,
        max_bytes=10_000,
    )
    assert resolved == "application/pdf"


def test_content_type_parameters_are_ignored() -> None:
    resolved = validate_upload(
        filename="a.png",
        declared_content_type="image/png; charset=binary",
        head=b"\x89PNG\r\n\x1a\n",
        size=100,
        max_bytes=10_000,
    )
    assert resolved == "image/png"


def test_sniffing_recognises_the_supported_signatures() -> None:
    assert sniff_content_type(b"%PDF-1.7") == "application/pdf"
    assert sniff_content_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_content_type(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert sniff_content_type(b"GIF89a") == "image/gif"
    assert sniff_content_type(b"hello world") is None


# ---------------------------------------------------------------------------
# Checksums (§20, INV-017)
# ---------------------------------------------------------------------------
def test_sha256_is_the_expected_digest() -> None:
    # Known-answer test: a silently wrong checksum would break INV-017.
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


# ---------------------------------------------------------------------------
# Backend behaviour (in-memory double, used only in tests)
# ---------------------------------------------------------------------------
def test_put_and_get_round_trip(storage) -> None:
    stored = storage.put_object(
        bucket=storage.bucket_knowledge,
        key="knowledge/1/policy.pdf",
        data=b"%PDF-1.7 hello",
        content_type="application/pdf",
    )
    assert stored.size == len(b"%PDF-1.7 hello")
    assert stored.checksum == sha256_hex(b"%PDF-1.7 hello")
    assert storage.get_object(bucket=storage.bucket_knowledge, key="knowledge/1/policy.pdf") == (
        b"%PDF-1.7 hello"
    )
    assert storage.object_exists(bucket=storage.bucket_knowledge, key="knowledge/1/policy.pdf")


def test_missing_object_raises_rather_than_returning_empty(storage) -> None:
    """INV-017: absence must be distinguishable from emptiness, or a document can
    be marked READY with no content behind it."""
    with pytest.raises(StorageObjectNotFoundError):
        storage.get_object(bucket=storage.bucket_knowledge, key="knowledge/1/missing.pdf")
    assert not storage.object_exists(bucket=storage.bucket_knowledge, key="knowledge/1/missing.pdf")


def test_checksum_verification_detects_tampering(storage) -> None:
    stored = storage.put_object(
        bucket=storage.bucket_knowledge, key="knowledge/1/a.txt", data=b"original"
    )
    assert storage.verify_checksum(
        bucket=storage.bucket_knowledge, key="knowledge/1/a.txt", expected=stored.checksum
    )
    assert not storage.verify_checksum(
        bucket=storage.bucket_knowledge, key="knowledge/1/a.txt", expected="0" * 64
    )


def test_verify_checksum_raises_when_the_object_is_gone(storage) -> None:
    from app.shared.storage.port import StorageChecksumMismatchError

    with pytest.raises(StorageChecksumMismatchError):
        storage.verify_checksum(
            bucket=storage.bucket_knowledge, key="knowledge/1/never.txt", expected="0" * 64
        )


def test_traversal_is_blocked_at_the_write_boundary_too(storage) -> None:
    """Validation must happen on write as well as on read.

    Checking only on read would mean the hostile key was already persisted, and
    the traversal attempt would merely be unable to *retrieve* it.
    """
    with pytest.raises(StorageUnsafeKeyError):
        storage.put_object(bucket=storage.bucket_knowledge, key="../escape.txt", data=b"x")


def test_write_to_an_uncreated_bucket_is_refused(storage) -> None:
    from app.shared.storage.port import StorageBucketUnavailableError

    with pytest.raises(StorageBucketUnavailableError):
        storage.put_object(bucket="does-not-exist", key="a.txt", data=b"x")


def test_presigned_url_for_a_missing_object_raises(storage) -> None:
    with pytest.raises(StorageObjectNotFoundError):
        storage.presigned_get_url(bucket=storage.bucket_knowledge, key="knowledge/1/nope.txt")


def test_oversized_write_is_refused_by_the_backend(storage, settings) -> None:
    storage._max_bytes = 128
    with pytest.raises(StorageObjectTooLargeError):
        storage.put_object(bucket=storage.bucket_knowledge, key="big.txt", data=b"x" * 500)


def test_storage_outage_is_distinguishable_from_a_missing_object(storage) -> None:
    """§130: an outage is a degraded dependency, not a 404. Conflating them would
    let the caller conclude "the document was legitimately deleted"."""
    from app.shared.storage.port import StorageBucketUnavailableError

    storage.put_object(bucket=storage.bucket_knowledge, key="a.txt", data=b"x")
    storage.simulate_outage = True
    with pytest.raises(StorageBucketUnavailableError):
        storage.get_object(bucket=storage.bucket_knowledge, key="a.txt")


def test_presigned_put_rejects_a_disallowed_type(storage) -> None:
    with pytest.raises(StorageUnsupportedContentTypeError):
        storage.presigned_put_url(
            bucket=storage.bucket_knowledge, key="x.exe", content_type="application/x-msdownload"
        )
