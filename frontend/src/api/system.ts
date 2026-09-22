/**
 * System module: health probes + audit (§130, §133).
 *
 * The health endpoint lives OUTSIDE `/api/v1` and reports per-dependency status
 * with a criticality so the System page can distinguish "the shop is down"
 * (MySQL) from "RAG is unavailable" (degradable, commerce unaffected).
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type { PageQuery } from '@/types/api'
import type { Paged } from '@/types/domain'

export type DependencyCriticality = 'critical' | 'important' | 'degradable'
export type DependencyStatus = 'up' | 'down' | 'skipped'

export interface DependencyCheck {
  name: string
  criticality: DependencyCriticality
  status: DependencyStatus
  detail: string
  duration_ms: number
}

export interface HealthReport {
  status: 'ok' | 'degraded' | 'unhealthy'
  service: string
  version: string
  environment: string
  checked_at: string
  checks: DependencyCheck[]
}

export interface AuditRecord {
  id: string
  actor_id: string
  actor_type: 'USER' | 'AGENT' | 'SYSTEM'
  action: string
  resource_type: string
  resource_id: string
  trace_id: string
  /** Masked before/after snapshots; never raw PII (§133). */
  before_snapshot?: Record<string, unknown>
  after_snapshot?: Record<string, unknown>
  result: 'SUCCESS' | 'FAILURE' | 'BLOCKED'
  created_at: string
}

export const systemApi = {
  /** Absolute path: `/health/live` is not under the `/api/v1` base. */
  async live(): Promise<HealthReport> {
    return httpClient.absolute<HealthReport>(API.health.live)
  },

  async ready(): Promise<HealthReport> {
    return httpClient.absolute<HealthReport>(API.health.ready)
  },

  async audit(query: PageQuery & { resource_type?: string } = {}): Promise<Paged<AuditRecord>> {
    return httpClient.get<Paged<AuditRecord>>(API.audit.list, { params: query })
  },
}
