/**
 * SSE parser + agent event tests (§101, §132).
 *
 * The parser is pure, so the wire format can be tested without a network. The
 * security-relevant assertion is the last group: an unknown or malformed event must be
 * DROPPED, never rendered, and there must be no reasoning channel.
 */

import { describe, expect, it } from 'vitest'
import { createSseParser, decodeSseFrame } from '@/agent/sse'
import {
  AGENT_EVENT_NAMES,
  isAgentEventName,
  parseAgentEvent,
  toToolProgress,
} from '@/agent/events'

function frame(name: string, payload: Record<string, unknown>): string {
  return `event: ${name}\ndata: ${JSON.stringify({ event: name, ...payload })}\n\n`
}

const base = { run_id: 'run-1', thread_id: 'thread-1', seq: 1, at: '2025-01-01T00:00:00Z' }

describe('createSseParser', () => {
  it('parses one complete frame', () => {
    const parse = createSseParser()
    const frames = parse(frame('run_started', { ...base, agent_name: 'assistant', query: 'hi' }))
    expect(frames).toHaveLength(1)
    expect(frames[0]?.event).toBe('run_started')
  })

  it('parses MULTIPLE frames delivered in one chunk', () => {
    const parse = createSseParser()
    const chunk =
      frame('run_started', { ...base, agent_name: 'assistant', query: 'hi' }) +
      frame('route_selected', { ...base, seq: 2, route: 'catalog', reason: 'keyword' })
    expect(parse(chunk)).toHaveLength(2)
  })

  it('handles a frame split across chunk boundaries', () => {
    const parse = createSseParser()
    const whole = frame('final_answer', { ...base, answer: 'done', citations: [] })
    const cut = Math.floor(whole.length / 2)
    expect(parse(whole.slice(0, cut))).toHaveLength(0)
    expect(parse(whole.slice(cut))).toHaveLength(1)
  })

  it('ignores comment/keep-alive lines', () => {
    const parse = createSseParser()
    const frames = parse(`: keep-alive\n\n${frame('run_started', { ...base, agent_name: 'a', query: 'q' })}`)
    expect(frames).toHaveLength(1)
    expect(frames[0]?.event).toBe('run_started')
  })

  it('normalizes CRLF line endings', () => {
    const parse = createSseParser()
    const chunk = `event: run_started\r\ndata: ${JSON.stringify({ ...base, event: 'run_started' })}\r\n\r\n`
    expect(parse(chunk)).toHaveLength(1)
  })

  it('joins multi-line data fields', () => {
    const parse = createSseParser()
    const frames = parse('event: x\ndata: {"a":\ndata: 1}\n\n')
    expect(frames[0]?.data).toBe('{"a":\n1}')
  })
})

describe('decodeSseFrame', () => {
  it('decodes a well-formed event', () => {
    const event = decodeSseFrame({
      event: 'tool_started',
      data: JSON.stringify({ ...base, event: 'tool_started', tool_call_id: 't1', tool_name: 'get_order' }),
    })
    expect(event?.event).toBe('tool_started')
  })

  it('drops a frame with invalid JSON', () => {
    expect(decodeSseFrame({ event: 'run_started', data: '{oops' })).toBeNull()
  })

  it('drops a frame with no data', () => {
    expect(decodeSseFrame({ event: 'run_started', data: '' })).toBeNull()
  })

  it('uses the SSE `event:` field when the payload omits `event`', () => {
    const event = decodeSseFrame({
      event: 'final_answer',
      data: JSON.stringify({ ...base, answer: 'ok' }),
    })
    expect(event?.event).toBe('final_answer')
  })
})

describe('parseAgentEvent — fail closed', () => {
  it('accepts only the 12 frozen names', () => {
    for (const name of AGENT_EVENT_NAMES) {
      expect(isAgentEventName(name)).toBe(true)
    }
    expect(isAgentEventName('reasoning_delta')).toBe(false)
    expect(isAgentEventName('thought')).toBe(false)
  })

  it('drops an UNKNOWN event name instead of rendering it', () => {
    expect(parseAgentEvent({ ...base, event: 'chain_of_thought', text: 'secret reasoning' })).toBeNull()
    expect(parseAgentEvent({ ...base, event: 'eval', code: 'alert(1)' })).toBeNull()
  })

  it('drops a payload that is not an object', () => {
    expect(parseAgentEvent('<script>alert(1)</script>')).toBeNull()
    expect(parseAgentEvent(null)).toBeNull()
    expect(parseAgentEvent([{ event: 'run_started' }])).toBeNull()
  })

  it('drops an event without a run_id', () => {
    expect(parseAgentEvent({ event: 'run_started', thread_id: 't', seq: 1, at: 'now' })).toBeNull()
  })

  it('rejects a chart_spec on any event other than final_answer', () => {
    const smuggled = parseAgentEvent({
      ...base,
      event: 'tool_completed',
      tool_call_id: 't',
      tool_name: 'x',
      status: 'succeeded',
      chart_spec: { kind: 'line', series: [{ name: 's', data: [1] }] },
    })
    expect(smuggled).toBeNull()
  })
})

describe('toToolProgress', () => {
  it('projects the identifying fields of a tool event', () => {
    const progress = toToolProgress({
      event: 'tool_started',
      run_id: 'r',
      thread_id: 't',
      seq: 1,
      at: 'now',
      tool_call_id: 'call-9',
      tool_name: 'query_inventory',
    })
    expect(progress).toEqual({ tool_call_id: 'call-9', tool_name: 'query_inventory' })
  })
})
