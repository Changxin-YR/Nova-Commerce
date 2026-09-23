/**
 * Knowledge document action availability (§53, §99).
 *
 * WHY THIS IS PURE AND TESTED
 *  The document lifecycle has five states (UPLOADED / PROCESSING / READY / FAILED / ARCHIVED) and
 *  two task endpoints (`/reprocess`, `/archive`). The console is the only place a human drives that
 *  lifecycle, so an inline `v-if` is exactly how the UI drifts from the backend's rules — an
 *  operator clicks 重新处理 on a document that is already mid-parse and the button "just does
 *  nothing", or worse, starts a second job.
 *
 * FRONTEND POLICY, SERVER AUTHORITY (§104)
 *  These predicates decide only what the UI OFFERS. The server re-validates every transition and
 *  answers `DOCUMENT_STATE_INVALID` (100002), so a 403/409 here means the UI and the server
 *  disagreed — a real case the view handles rather than assumes away.
 *
 * THE RULES, and why each one is where it is:
 *  - `PROCESSING` blocks BOTH actions. A parse/embed job is in flight; reprocessing would queue a
 *    second job over the same document, and archiving would pull the document out from under a job
 *    that is going to write chunks to it. Neither is recoverable by the operator.
 *  - `ARCHIVED` blocks BOTH. The document is retained but out of the active set; a reprocess would
 *    silently resurrect it, which is the opposite of what archiving meant. Bringing it back is a
 *    deliberate operation the contract does not define yet — so the UI does not offer it rather
 *    than inventing one.
 *  - `UPLOADED` offers ARCHIVE but NOT reprocess: nothing has been parsed yet, so "re-process" has
 *    no meaning; withdrawing a queued upload does.
 *  - `READY` and `FAILED` offer both: re-running is the documented recovery for a failure, and a
 *    ready document is re-run deliberately when its source changed.
 *
 * NOTE: the `PROCESSING`/`ARCHIVED` guards are the frontend's reading of §53. They are the
 * conservative choice (refuse the ambiguous transition) and they are marked as an assumption in the
 * migration report, so the backend can tighten or relax them without the UI silently disagreeing.
 */

import type { KnowledgeDocStatus } from '@/types/domain'

export type KnowledgeDocAction = 'reprocess' | 'archive'

/** A parse/embed job is in flight; the document is mid-transition. */
export function isProcessing(status: KnowledgeDocStatus): boolean {
  return status === 'PROCESSING'
}

/** A failed document needs operator attention — surfaced, never hidden. */
export function needsAttention(status: KnowledgeDocStatus): boolean {
  return status === 'FAILED'
}

export function canReprocess(status: KnowledgeDocStatus): boolean {
  return status === 'READY' || status === 'FAILED'
}

export function canArchive(status: KnowledgeDocStatus): boolean {
  return status === 'UPLOADED' || status === 'READY' || status === 'FAILED'
}

/** Neither action is available — the row renders a hint instead of a dead button. */
export function isTerminalForOperator(status: KnowledgeDocStatus): boolean {
  return status === 'PROCESSING' || status === 'ARCHIVED'
}

/** Human reason an action is unavailable, surfaced as a tooltip rather than a hidden rule. */
export function docActionBlockedReason(action: KnowledgeDocAction, status: KnowledgeDocStatus): string {
  if (status === 'PROCESSING') {
    return action === 'reprocess' ? '文档正在解析，请等待完成' : '文档正在解析，无法归档'
  }
  if (status === 'ARCHIVED') {
    return action === 'reprocess' ? '文档已归档，无法重新处理' : '文档已归档'
  }
  if (action === 'reprocess') return '仅已就绪或失败的文档可重新处理'
  return '当前状态无法归档'
}
