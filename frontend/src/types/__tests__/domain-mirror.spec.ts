/**
 * Contract-drift guard (§95, §105, REQ-FE-009).
 *
 * `src/types/api.ts` hand-mirrors `backend/app/core/errors.py::ErrorCode` so the UI
 * can branch on business codes. A hand-written mirror rots. This spec parses the
 * PYTHON source and fails the build when the two disagree, which turns "the mirror
 * is stale" from a runtime surprise into a red test.
 *
 * It also asserts the frozen status enums from §105 so a rename in one place cannot
 * silently pass.
 *
 * Skips itself (with a visible message) when the backend file is absent, so the
 * frontend repo can still be tested standalone.
 */

import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { ErrorCode } from '@/types/api'
import {
  AFTER_SALE_STATUSES,
  COUPON_STATUSES,
  FULFILLMENT_STATUSES,
  KNOWLEDGE_DOC_STATUSES,
  ORDER_STATUSES,
  PAYMENT_STATUSES,
  PENDING_ACTION_STATUSES,
  RISK_LEVELS,
} from '@/types/domain'
import { AGENT_EVENT_NAMES } from '@/agent/events'

const here = dirname(fileURLToPath(import.meta.url))
const backendErrors = resolve(here, '../../../../backend/app/core/errors.py')

/** Parse `NAME = 12_345` lines out of the Python ErrorCode enum body. */
function parsePythonErrorCodes(source: string): Map<string, number> {
  const codes = new Map<string, number>()
  const enumBody = source.split('class ErrorCode(enum.IntEnum):')[1] ?? ''
  // Stop at the next top-level definition so later enums cannot leak in.
  const body = enumBody.split('\n# ---')[0] ?? enumBody
  const pattern = /^\s{4}([A-Z][A-Z0-9_]*)\s*=\s*([0-9_]+)\s*$/gm
  for (const match of body.matchAll(pattern)) {
    const name = match[1]
    const raw = match[2]
    if (!name || !raw) continue
    codes.set(name, Number(raw.replace(/_/g, '')))
  }
  return codes
}

const hasBackend = existsSync(backendErrors)

describe('frontend/backend contract drift', () => {
  it.skipIf(!hasBackend)('mirrors every backend ErrorCode name and value', () => {
    const python = parsePythonErrorCodes(readFileSync(backendErrors, 'utf8'))
    expect(python.size).toBeGreaterThan(100)

    const mismatches: string[] = []
    const missing: string[] = []
    const extra: string[] = []

    const ts = ErrorCode as unknown as Record<string, number>
    for (const [name, value] of python) {
      if (!(name in ts)) {
        missing.push(`${name} (${value})`)
      } else if (ts[name] !== value) {
        mismatches.push(`${name}: backend=${value} frontend=${ts[name]}`)
      }
    }
    for (const name of Object.keys(ts)) {
      if (!python.has(name)) extra.push(name)
    }

    expect({ missing, mismatches, extra }).toEqual({ missing: [], mismatches: [], extra: [] })
  })

  it.skipIf(!hasBackend)('keeps the trace header name in sync with the backend constant', () => {
    const source = readFileSync(backendErrors.replace('errors.py', 'context.py'), 'utf8')
    const match = /TRACE_ID_HEADER\s*=\s*"([^"]+)"/.exec(source)
    expect(match?.[1]).toBe('X-Trace-Id')
  })
})

describe('frozen enums (§105)', () => {
  it('keeps the exact order status set', () => {
    expect([...ORDER_STATUSES]).toEqual([
      'PENDING_PAYMENT',
      'PROCESSING',
      'COMPLETED',
      'CANCELLED',
      'CLOSED',
    ])
  })

  it('keeps the exact payment status set', () => {
    expect([...PAYMENT_STATUSES]).toEqual([
      'UNPAID',
      'PAYING',
      'PAID',
      'PARTIAL_REFUNDED',
      'REFUNDED',
    ])
  })

  it('keeps the exact fulfillment status set', () => {
    expect([...FULFILLMENT_STATUSES]).toEqual([
      'UNFULFILLED',
      'PARTIAL_SHIPPED',
      'SHIPPED',
      'DELIVERED',
    ])
  })

  it('keeps the exact after-sale status set', () => {
    expect([...AFTER_SALE_STATUSES]).toEqual(['NONE', 'PROCESSING', 'PARTIAL_REFUNDED', 'REFUNDED'])
  })

  it('keeps the exact coupon status set', () => {
    expect([...COUPON_STATUSES]).toEqual(['UNUSED', 'LOCKED', 'USED', 'EXPIRED'])
  })

  it('keeps the exact knowledge document status set', () => {
    expect([...KNOWLEDGE_DOC_STATUSES]).toEqual([
      'UPLOADED',
      'PROCESSING',
      'READY',
      'FAILED',
      'ARCHIVED',
    ])
  })

  it('keeps the exact pending action status set', () => {
    expect([...PENDING_ACTION_STATUSES]).toEqual([
      'PENDING',
      'APPROVED',
      'REJECTED',
      'EXECUTING',
      'SUCCEEDED',
      'FAILED',
      'EXPIRED',
    ])
  })

  it('keeps the exact risk level set (READ is the lowest level, not a typo for RED)', () => {
    expect([...RISK_LEVELS]).toEqual(['READ', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'])
  })

  it('keeps exactly the 12 frozen agent events, with no chain-of-thought channel', () => {
    expect([...AGENT_EVENT_NAMES]).toEqual([
      'run_started',
      'route_selected',
      'plan_created',
      'tool_started',
      'tool_completed',
      'evidence_ready',
      'action_pending',
      'action_approved',
      'action_executing',
      'action_completed',
      'final_answer',
      'run_failed',
    ])
    expect(AGENT_EVENT_NAMES).toHaveLength(12)
    // Guard against a well-meaning addition of a reasoning channel.
    expect(
      AGENT_EVENT_NAMES.filter((name) => /reason|think|thought|cot/i.test(name)),
    ).toEqual([])
  })
})

describe('status codes stay ints and unique', () => {
  it('has no duplicate values in the frontend ErrorCode mirror', () => {
    const values = Object.entries(ErrorCode).map(([, value]) => value)
    expect(new Set(values).size).toBe(values.length)
  })
})
