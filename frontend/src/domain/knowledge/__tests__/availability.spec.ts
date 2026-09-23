/**
 * Knowledge document action availability — the lifecycle drift guard (§53, §99).
 *
 * These tests exist because the console is the ONLY place a human drives the document lifecycle,
 * and an inline `v-if` is exactly how a UI drifts from the backend's rules: an operator clicks
 * 重新处理 on a document that is already parsing and either nothing happens or a second job is
 * queued over the same document.
 *
 * Every assertion pins a RULE, not an implementation detail, so "simplifying" a predicate fails the
 * test and names the rule that broke.
 */

import { describe, expect, it } from 'vitest'
import {
  canArchive,
  canReprocess,
  docActionBlockedReason,
  isProcessing,
  isTerminalForOperator,
  needsAttention,
} from '@/domain/knowledge/availability'
import { KNOWLEDGE_DOC_STATUSES, type KnowledgeDocStatus } from '@/types/domain'

describe('canReprocess — re-running a document (§53)', () => {
  it('allows READY and FAILED', () => {
    // READY: re-run deliberately when the source changed. FAILED: this is the documented recovery.
    expect(canReprocess('READY')).toBe(true)
    expect(canReprocess('FAILED')).toBe(true)
  })

  it('REFUSES while PROCESSING — a parse/embed job is already in flight', () => {
    // Starting a second job over the same document is not something the operator can undo.
    expect(canReprocess('PROCESSING')).toBe(false)
  })

  it('REFUSES an ARCHIVED document, which would silently resurrect it', () => {
    expect(canReprocess('ARCHIVED')).toBe(false)
  })

  it('REFUSES UPLOADED — nothing has been parsed yet, so "re-process" has no meaning', () => {
    expect(canReprocess('UPLOADED')).toBe(false)
  })
})

describe('canArchive — withdrawing a document (§53)', () => {
  it('allows UPLOADED, READY and FAILED', () => {
    expect(canArchive('UPLOADED')).toBe(true)
    expect(canArchive('READY')).toBe(true)
    expect(canArchive('FAILED')).toBe(true)
  })

  it('REFUSES while PROCESSING — the in-flight job would write chunks into an archived document', () => {
    expect(canArchive('PROCESSING')).toBe(false)
  })

  it('REFUSES an already ARCHIVED document (the action is not idempotent as a button)', () => {
    expect(canArchive('ARCHIVED')).toBe(false)
  })
})

describe('the two actions are never both unavailable by accident (every non-terminal state offers one)', () => {
  it('READY and FAILED offer both; UPLOADED offers archive only', () => {
    expect(canReprocess('READY') && canArchive('READY')).toBe(true)
    expect(canReprocess('FAILED') && canArchive('FAILED')).toBe(true)
    expect(canReprocess('UPLOADED')).toBe(false)
    expect(canArchive('UPLOADED')).toBe(true)
  })

  it('PROCESSING and ARCHIVED are the only operator-terminal states', () => {
    for (const status of KNOWLEDGE_DOC_STATUSES as readonly KnowledgeDocStatus[]) {
      const terminal = isTerminalForOperator(status)
      expect(terminal, `terminal=${terminal} for ${status}`).toBe(
        status === 'PROCESSING' || status === 'ARCHIVED',
      )
      // A terminal row must genuinely offer nothing, or the hint would hide a usable action.
      if (terminal) {
        expect(canReprocess(status) || canArchive(status), `${status} must offer at least one action`).toBe(
          false,
        )
      }
    }
  })

  it('covers the whole frozen vocabulary, so a new status cannot slip through untested', () => {
    expect([...KNOWLEDGE_DOC_STATUSES]).toEqual([
      'UPLOADED',
      'PROCESSING',
      'READY',
      'FAILED',
      'ARCHIVED',
    ])
  })
})

describe('§108 knowledge surface states', () => {
  it('PROCESSING is the live/processing state', () => {
    expect(isProcessing('PROCESSING')).toBe(true)
    for (const status of ['UPLOADED', 'READY', 'FAILED', 'ARCHIVED'] as KnowledgeDocStatus[]) {
      expect(isProcessing(status)).toBe(false)
    }
  })

  it('FAILED is the state that needs operator attention', () => {
    expect(needsAttention('FAILED')).toBe(true)
    for (const status of ['UPLOADED', 'PROCESSING', 'READY', 'ARCHIVED'] as KnowledgeDocStatus[]) {
      expect(needsAttention(status)).toBe(false)
    }
  })
})

describe('docActionBlockedReason — the UI explains WHY instead of hiding the rule', () => {
  it('explains that a parsing document is busy, per action', () => {
    expect(docActionBlockedReason('reprocess', 'PROCESSING')).toContain('解析')
    expect(docActionBlockedReason('archive', 'PROCESSING')).toContain('解析')
  })

  it('explains that an archived document is out of the active set', () => {
    expect(docActionBlockedReason('reprocess', 'ARCHIVED')).toContain('归档')
    expect(docActionBlockedReason('archive', 'ARCHIVED')).toContain('归档')
  })

  it('never returns an empty explanation for any status/action pair', () => {
    const actions = ['reprocess', 'archive'] as const
    for (const status of KNOWLEDGE_DOC_STATUSES as readonly KnowledgeDocStatus[]) {
      for (const action of actions) {
        const reason = docActionBlockedReason(action, status)
        expect(reason.length, `${action} blocked on ${status} must be explained`).toBeGreaterThan(0)
      }
    }
  })
})
