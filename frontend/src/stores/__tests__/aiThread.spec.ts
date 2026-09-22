/**
 * Agent thread store: event application, fail-closed charts, cancel semantics (§101).
 *
 * These run against the real Pinia store with NO network: `applyEvent` is the single
 * mutation point for streamed state, which is exactly why it is worth testing in
 * isolation.
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useAiThreadStore } from '@/stores/aiThread'
import type { AgentEvent } from '@/agent/events'

const base = { run_id: 'run-1', thread_id: 'thread-1', at: '2025-01-01T00:00:00Z' }

function ev<T extends AgentEvent>(event: T): T {
  return event
}

describe('aiThread store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('starts a message on run_started and reaches succeeded on final_answer', () => {
    const store = useAiThreadStore()

    store.applyEvent(ev({ ...base, seq: 1, event: 'run_started', agent_name: 'assistant', query: 'hi' }))
    expect(store.status).toBe('run_started')
    expect(store.messages).toHaveLength(1)
    expect(store.activeRunId).toBe('run-1')

    store.applyEvent(ev({ ...base, seq: 2, event: 'final_answer', answer: '完成', citations: [] }))
    expect(store.status).toBe('succeeded')
    const blocks = store.messages[0]?.blocks ?? []
    expect(blocks.some((b) => b.kind === 'text' && b.text === '完成')).toBe(true)
  })

  it('tracks tool progress and updates it in place on tool_completed', () => {
    const store = useAiThreadStore()
    store.applyEvent(ev({ ...base, seq: 1, event: 'run_started', agent_name: 'operations', query: 'ship' }))
    store.applyEvent(
      ev({
        ...base,
        seq: 2,
        event: 'tool_started',
        tool_call_id: 'call-1',
        tool_name: 'query_inventory',
      }),
    )
    expect(store.toolCalls).toHaveLength(1)
    expect(store.toolCalls[0]?.status).toBe('running')

    store.applyEvent(
      ev({
        ...base,
        seq: 3,
        event: 'tool_completed',
        tool_call_id: 'call-1',
        tool_name: 'query_inventory',
        status: 'succeeded',
        summary: '查到 12 件',
        duration_ms: 42,
      }),
    )
    expect(store.toolCalls[0]?.status).toBe('succeeded')
    expect(store.toolCalls[0]?.summary).toBe('查到 12 件')
    // The block is updated, not duplicated.
    const toolBlocks = store.messages[0]?.blocks.filter((b) => b.kind === 'tool_progress') ?? []
    expect(toolBlocks).toHaveLength(1)
    expect(toolBlocks[0]?.tool?.status).toBe('succeeded')
  })

  it('enters waiting_approval and renders an ActionProposal block (never auto-executes)', () => {
    const store = useAiThreadStore()
    store.applyEvent(
      ev({
        ...base,
        seq: 1,
        event: 'action_pending',
        pending_action_id: 'pa-1',
        action_type: 'ADJUST_INVENTORY',
        tool_name: 'adjust_stock',
        summary: '将 SKU-1 库存 +10',
        risk_level: 'HIGH',
        payload_hash: 'hash-abc',
        expires_at: '2025-01-01T01:00:00Z',
        requires_approval: true,
      }),
    )

    expect(store.status).toBe('waiting_approval')
    expect(store.isWaitingApproval).toBe(true)
    expect(store.pendingAction?.id).toBe('pa-1')
    const actionBlocks = store.messages[0]?.blocks.filter((b) => b.kind === 'action_proposal') ?? []
    expect(actionBlocks).toHaveLength(1)
    expect(actionBlocks[0]?.action?.status).toBe('PENDING')
    expect(actionBlocks[0]?.action?.riskLevel).toBe('HIGH')
  })

  it('reflects an approval decision on the block', () => {
    const store = useAiThreadStore()
    store.applyEvent(
      ev({
        ...base,
        seq: 1,
        event: 'action_pending',
        pending_action_id: 'pa-1',
        action_type: 'X',
        tool_name: 't',
        summary: 's',
        risk_level: 'MEDIUM',
        payload_hash: 'h',
        expires_at: '2025-01-01T01:00:00Z',
        requires_approval: true,
      }),
    )
    store.applyEvent(
      ev({
        ...base,
        seq: 2,
        event: 'action_approved',
        pending_action_id: 'pa-1',
        approved_by: 'admin',
        approved: true,
      }),
    )
    const block = store.messages[0]?.blocks.find((b) => b.kind === 'action_proposal')
    expect(block?.action?.status).toBe('APPROVED')
    expect(store.status).toBe('streaming')
  })

  it('RENDERS A VALID chart and REFUSES an invalid one (fail closed)', () => {
    const store = useAiThreadStore()
    store.applyEvent(
      ev({
        ...base,
        seq: 1,
        event: 'final_answer',
        answer: '看图',
        citations: [],
        chart_spec: {
          kind: 'line',
          title: 'GMV',
          categories: ['a', 'b'],
          series: [{ name: 'GMV', data: [1, 2] }],
        },
      }),
    )
    const chartBlock = store.messages[0]?.blocks.find((b) => b.kind === 'chart')
    expect(chartBlock?.chart?.kind).toBe('line')

    // Unknown kind: must become an Error block, not a chart.
    store.selectThread('t2')
    store.applyEvent(
      ev({
        ...base,
        seq: 1,
        event: 'final_answer',
        answer: 'bad',
        citations: [],
        chart_spec: { kind: 'iframe', src: 'https://evil.example' } as never,
      }),
    )
    const blocks = store.messages[0]?.blocks ?? []
    expect(blocks.some((b) => b.kind === 'chart')).toBe(false)
    const errorBlock = blocks.find((b) => b.kind === 'error')
    expect(errorBlock?.text).toContain('已拒绝渲染')
  })

  it('drops out-of-order and duplicate events by seq', () => {
    const store = useAiThreadStore()
    store.applyEvent(ev({ ...base, seq: 5, event: 'run_started', agent_name: 'a', query: 'q' }))
    store.applyEvent(
      ev({
        ...base,
        seq: 9,
        event: 'tool_started',
        tool_call_id: 'c',
        tool_name: 'duplicate-me',
      }),
    )
    store.applyEvent(
      ev({
        ...base,
        seq: 7,
        event: 'tool_started',
        tool_call_id: 'c2',
        tool_name: 'late-arrival',
      }),
    )
    expect(store.toolCalls.map((t) => t.tool_name)).toEqual(['duplicate-me'])
  })

  it('maps run_failed onto the Failed state with a traceable error', () => {
    const store = useAiThreadStore()
    store.applyEvent(
      ev({
        ...base,
        seq: 1,
        event: 'run_failed',
        error_code: 110_001,
        message: '本次任务超出预算上限，已终止',
        retryable: false,
      }),
    )
    expect(store.status).toBe('failed')
    expect(store.error?.code).toBe(110_001)
    expect(store.error?.retryable).toBe(false)
  })

  it('cancel() produces the Cancelled state, NOT a failure (§108)', () => {
    const store = useAiThreadStore()
    store.applyEvent(ev({ ...base, seq: 1, event: 'run_started', agent_name: 'a', query: 'q' }))
    store.applyEvent(
      ev({ ...base, seq: 2, event: 'tool_started', tool_call_id: 'c', tool_name: 'slow_tool' }),
    )
    expect(store.isRunning).toBe(true)

    store.cancel()
    expect(store.status).toBe('cancelled')
    expect(store.error).toBeNull()
    const text = store.messages[0]?.blocks.map((b) => b.text).join(' ') ?? ''
    expect(text).toContain('取消')
  })

  it('records citations from evidence_ready as a Citation block', () => {
    const store = useAiThreadStore()
    store.applyEvent(
      ev({
        ...base,
        seq: 1,
        event: 'evidence_ready',
        citations: [
          { index: 1, doc_id: 'd1', doc_name: '退换货政策.md', chunk_id: 'c1', score: 0.87, snippet: '7 天无理由' },
        ],
      }),
    )
    const block = store.messages[0]?.blocks.find((b) => b.kind === 'citation')
    expect(block?.citations).toHaveLength(1)
    expect(block?.citations?.[0]?.doc_name).toBe('退换货政策.md')
  })
})
