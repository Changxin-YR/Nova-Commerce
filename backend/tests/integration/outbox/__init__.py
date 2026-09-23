"""Phase 6 outbox integration tests (spec §49, REQ-CON-003).

Split by seam rather than by module under test:

    test_writer.py     ``OutboxWriter.enqueue`` - dedup, the false-collision guard, payload
    test_publisher.py  ``publish_due`` - selection, retry arithmetic, the terminal state
    test_seams.py      the three marked call sites, driven through the real workflows

Everything here runs against **real MySQL**: the properties under test are database
properties (the ``uq_event_type_aggregate`` unique index, the ``status``/``next_retry_at``
scan and its row locks, and the fact that an event row commits *with* the business rows).
Spec §113 forbids demonstrating any of that on a mock.
"""
