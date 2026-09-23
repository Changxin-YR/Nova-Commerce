/**
 * The ONLY supported way to talk to the backend (§106, REQ-FE-008).
 *
 * Every module below goes through `src/api/client.ts`, which centrally handles the
 * auth header, the 401->refresh retry (single-flight), trace-id propagation, the
 * request timeout and error mapping into `{code, message, data, trace_id}`.
 *
 * Components MUST import from here (or from a specific module) and MUST NOT import
 * axios. This barrel exists so the rule is easy to follow.
 */

export { http, httpClient, NovaHttpClient } from '@/api/client'
export type { NovaClientOptions, NovaRequestConfig } from '@/api/client'

export { API, path } from '@/api/endpoints'

export { normalizeError, messageForCode, isApiEnvelope } from '@/api/error'
export {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  hasSession,
  onSessionExpired,
  setTokens,
} from '@/api/tokenStore'

export { authApi, addressApi } from '@/api/auth'
export { catalogApi, catalogAdminApi } from '@/api/catalog'
export { cartApi } from '@/api/cart'
export { orderApi, orderAdminApi, fulfillmentAdminApi } from '@/api/order'
export { paymentApi } from '@/api/payment'
export { afterSaleApi, afterSaleAdminApi } from '@/api/aftersales'
export { inventoryApi, inventoryAdminApi } from '@/api/inventory'
export { marketingApi, marketingAdminApi } from '@/api/marketing'
export { analyticsApi } from '@/api/analytics'
export { knowledgeAdminApi, retrievalApi } from '@/api/knowledge'
export { agentApi } from '@/api/agent'
export { governanceApi } from '@/api/governance'
export { systemApi } from '@/api/system'

export type { InventoryMovement } from '@/api/inventory'
export type { PendingActionQuery } from '@/api/governance'
export type { RagEvaluationResult } from '@/api/knowledge'
export type { AgentToolInfo } from '@/api/agent'
export type { AnalyticsQuery } from '@/api/analytics'
export type {
  AuditRecord,
  DependencyCheck,
  DependencyCriticality,
  DependencyStatus,
  HealthReport,
} from '@/api/system'
export type { CouponPayload, CouponPreviewResult, CouponCreatePayload, Promotion } from '@/api/marketing'
