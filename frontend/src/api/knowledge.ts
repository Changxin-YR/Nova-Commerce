/**
 * Knowledge / RAG module (§103).
 *
 * The retrieval-debug response exposes each pipeline stage separately
 * (rewrite -> filter -> dense -> sparse -> fusion -> rerank -> final evidence) so
 * the console can SHOW why a chunk surfaced instead of asserting that RAG works.
 */

import type { PageQuery } from '@/types/api'
import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type {
  CreateKnowledgeBaseRequest,
  RetrievalDebugRequest,
  RetrievalDebugResponse,
} from '@/types/api-contract'
import type { KnowledgeBase, KnowledgeDoc, Paged } from '@/types/domain'

export interface RagEvaluationResult {
  id: string
  knowledge_base_id: string
  dataset_name: string
  /** Retrieval quality metrics, 0..1. */
  recall_at_k: number
  precision_at_k: number
  mrr: number
  ndcg: number
  sample_count: number
  created_at: string
}

export const knowledgeAdminApi = {
  async bases(): Promise<KnowledgeBase[]> {
    return httpClient.get<KnowledgeBase[]>(API.knowledge.bases)
  },

  async createBase(payload: CreateKnowledgeBaseRequest): Promise<KnowledgeBase> {
    return httpClient.post<KnowledgeBase>(API.knowledge.bases, payload)
  },

  async documents(baseId: string, query: PageQuery = {}): Promise<Paged<KnowledgeDoc>> {
    return httpClient.get<Paged<KnowledgeDoc>>(API.knowledge.documents(baseId), { params: query })
  },

  /** Upload is multipart; the client keeps the envelope handling identical. */
  async uploadDocument(baseId: string, file: File): Promise<KnowledgeDoc> {
    const form = new FormData()
    form.append('file', file)
    return httpClient.upload<KnowledgeDoc>(API.knowledge.documents(baseId), form)
  },

  async reprocess(docId: string): Promise<KnowledgeDoc> {
    return httpClient.post<KnowledgeDoc>(API.knowledge.reprocess(docId), {})
  },

  async archive(docId: string): Promise<KnowledgeDoc> {
    return httpClient.post<KnowledgeDoc>(API.knowledge.archive(docId), {})
  },

  async evaluation(baseId: string): Promise<RagEvaluationResult[]> {
    return httpClient.get<RagEvaluationResult[]>(API.knowledge.evaluation, {
      params: { knowledge_base_id: baseId },
    })
  },
}

export const retrievalApi = {
  async debug(payload: RetrievalDebugRequest): Promise<RetrievalDebugResponse> {
    return httpClient.post<RetrievalDebugResponse>(API.knowledge.retrievalDebug, payload)
  },
}
