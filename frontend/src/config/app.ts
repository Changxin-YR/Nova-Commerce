/** Product-level constants shared by the shell, the document title and the router. */

export const APP_TITLE = import.meta.env.VITE_APP_TITLE ?? 'Nova Commerce'

export const APP_SHORT_NAME = 'Nova'

/** Agent tabs (§101). `value` matches the backend `agent_name` routing key. */
export const AGENT_TABS = [
  { value: 'assistant', label: '智能助手', description: '商品咨询、订单查询、政策问答' },
  { value: 'operations', label: '运营助手', description: '库存、订单、售后等写操作（需审批）' },
  { value: 'analytics', label: '数据分析', description: '经营指标与图表解读' },
] as const

export const AGENT_NAMES = AGENT_TABS.map((tab) => tab.value)
export type AgentName = (typeof AGENT_TABS)[number]['value']

/** Polling interval for in-flight runs and processing documents (§108 states). */
export const POLL_INTERVAL_MS = 3000
