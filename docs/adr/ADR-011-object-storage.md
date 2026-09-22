# ADR-011 — Object Storage

- **Status:** Accepted
- **Date:** 2026-09-22
- **Spec references:** §20, §25, §51, §52, §122, §128, §138 (ADR-011)
- **Amended by Phase 0 evidence** (image source — see "Decision" note below)

---

## Context

Spec §20 requires that a MinIO / S3-compatible object store hold product images,
knowledge source files, downloadable reports and large evidence artifacts, and
states four rules:

1. MySQL must never store large BLOBs.
2. The database stores `object_key`, `checksum`, `content_type`, `size`,
   `metadata`.
3. Knowledge documents live in a **private** bucket by default; product images
   may be served through a controlled public or signed URL.
4. Persistent files must **never** rely on the container-local filesystem.

Spec §122 defines the acceptance gate (FG-19): product image upload, knowledge
upload, checksum verification, private knowledge access, missing object, invalid
MIME, path traversal, oversized file, **storage restart**.

## Decision

**Use MinIO as the S3-compatible object store, accessed through the official
`minio` Python client behind a `ObjectStorage` port, with four logical buckets
and no container-local persistence.**

### Amendment discovered during Phase 0

The Docker Hub image `minio/minio` is **not pullable** from this environment:

```
docker pull minio/minio:latest
  -> Error response from daemon: pull access denied for minio/minio,
     repository does not exist or may require 'docker login'
```

This was reproduced twice, including with an explicit release tag, so it is not
a transient failure or a rate-limit artefact.

**Resolution:** obtain MinIO from MinIO's own registry —
`quay.io/minio/minio:latest`. This was verified to **pull and run**, and its
health endpoint was verified live:

```
GET /minio/health/live  ->  HTTP 200
```

Rationale for not switching products: spec §8/§20 permit any S3-compatible
store, so substituting (e.g. SeaweedFS, VersityGW, Zenko) would also be
in-spec. But MinIO needs no substitution — only a different registry. Changing
the registry is the **smallest possible change** that satisfies the spec, and
§150 requires exactly that: minimal-impact repair preserving the frozen
design. Keeping MinIO also preserves every documented operator workflow.

### Bucket layout

| Bucket | Default access | Contents |
|---|---|---|
| `nova-product-images` | controlled public / signed GET | Product and SKU images |
| `nova-knowledge-private` | **private** (signed URL only) | Knowledge source documents |
| `nova-reports` | private | Generated reports/exports |
| `nova-evidence` | private | Large gate evidence artifacts |

### Storage port

Business modules never import the MinIO SDK. They depend on an
`ObjectStorage` protocol held in `app/shared/storage/`, exposing:

```python
put_object(bucket, key, data, content_type) -> StoredObject
get_object(bucket, key) -> bytes
stat_object(bucket, key) -> ObjectStat          # raises ObjectNotFound
presigned_get(bucket, key, ttl) -> str
presigned_put(bucket, key, ttl, content_type) -> str
delete_object(bucket, key) -> None
ensure_buckets() -> None
```

`StoredObject` carries `object_key`, `checksum` (SHA-256), `content_type`,
`size` — exactly the columns §20 requires the database to persist. The MinIO
implementation lives under `app/shared/storage/backends/minio_backend.py`; a
`FakeObjectStorage` in-memory implementation exists for unit tests, and by
ADR-nothing it is **never** used for FG-19, which requires real storage.

### Database-side contract

Any table referencing a stored object stores only:

```
object_key      VARCHAR(512)  NOT NULL
checksum        CHAR(64)      NOT NULL     -- SHA-256 hex
content_type    VARCHAR(127)  NOT NULL
size_bytes      BIGINT UNSIGNED NOT NULL
metadata_json   JSON NULL                  -- extension only (spec §19)
```

No `BLOB`/`LONGBLOB` column for file content exists anywhere in the schema; an
architecture test asserts this.

## Consequences

**Positive**

- Object storage is an infrastructure detail behind a port, so the ingestion
  pipeline and catalog module are testable without MinIO, while FG-19 still
  exercises real MinIO.
- All four buckets are created idempotently at startup (`ensure_buckets()`), so
  a fresh environment needs no manual setup.
- Private-by-default for knowledge means the §59 visibility rules are enforced
  in two independent places (signed URL + retriever filter), which is the
  correct defence-in-depth for a gate that is otherwise easy to pass by accident.

**Negative / accepted**

- MinIO must be reached over a *client-visible* endpoint for presigned URLs. A
  container-internal hostname (`minio:9000`) produces URLs the browser cannot
  resolve. Therefore two endpoints are configured: `S3_ENDPOINT` (server-side
  operations) and `S3_PUBLIC_ENDPOINT` (presigning). This is recorded in
  `.env.example` and must not be collapsed into one variable.
- `quay.io` availability is now a build dependency. Mitigated by the optional
  `adobe/s3mock` / `versitygw` images verified pullable in Phase 0 as fallbacks
  if that registry is ever unavailable.
- Restart durability (FG-19) depends on a named volume being configured; the
  compose file declares `nx-minio-data` and the gate explicitly restarts the
  container to prove it, rather than trusting the declaration.
