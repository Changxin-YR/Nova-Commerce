<script setup lang="ts">
/**
 * Knowledge Center (§103).
 *
 * Three things the spec insists on being visible, not asserted:
 *   1. Document lifecycle states (UPLOADED / PROCESSING / READY / FAILED / ARCHIVED),
 *      including a Processing state that keeps polling until it resolves.
 *   2. Retrieval debug: every pipeline stage reported separately
 *      (rewrite → filter → dense → sparse → fusion → rerank → final evidence).
 *   3. RAG evaluation metrics.
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { knowledgeAdminApi, retrievalApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import { POLL_INTERVAL_MS } from '@/config/app'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'

const notifications = useNotificationStore()

const {
  data: baseData,
  status: baseStatus,
  error: baseError,
  execute: loadBases,
} = useAsyncState(() => knowledgeAdminApi.bases(), { immediate: true })

const bases = computed(() => baseData.value ?? [])
const selectedBaseId = ref('')

watch(
  bases,
  (list) => {
    if (!selectedBaseId.value && list.length > 0) selectedBaseId.value = list[0]?.id ?? ''
  },
  { immediate: true },
)

const {
  data: docData,
  status: docStatus,
  error: docError,
  execute: loadDocs,
  refresh: refreshDocs,
} = useAsyncState(
  () => (selectedBaseId.value ? knowledgeAdminApi.documents(selectedBaseId.value, { page: 1 }) : Promise.resolve(null)),
  { immediate: false },
)

watch(selectedBaseId, (id) => {
  if (id) void loadDocs()
}, { immediate: true })

const documents = computed(() => docData.value?.items ?? [])

/**
 * Poll while any document is PROCESSING so the Processing state (§108) resolves
 * without the user hitting refresh. Cleared as soon as nothing is processing.
 */
let pollTimer: ReturnType<typeof setInterval> | null = null

function syncPolling(): void {
  const needsPolling = documents.value.some((doc) => doc.status === 'PROCESSING')
  if (needsPolling && !pollTimer) {
    pollTimer = setInterval(() => void refreshDocs(), POLL_INTERVAL_MS)
  } else if (!needsPolling && pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

watch(documents, syncPolling)

onBeforeUnmount(() => {
  if (pollTimer) clearInterval(pollTimer)
})

const uploadInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false)

async function upload(file: File): Promise<void> {
  if (!selectedBaseId.value) return
  uploading.value = true
  try {
    await knowledgeAdminApi.uploadDocument(selectedBaseId.value, file)
    notifications.success('文档已上传', '解析与向量化将在后台进行')
    await loadDocs()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('上传失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    uploading.value = false
  }
}

function onFileChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) void upload(file)
  input.value = ''
}

async function reprocess(docId: string): Promise<void> {
  try {
    await knowledgeAdminApi.reprocess(docId)
    notifications.success('已提交重新处理')
    await loadDocs()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('重新处理失败', normalized.message, normalized.code, normalized.traceId)
  }
}

// -- Retrieval debug ---------------------------------------------------------
const debugQuery = ref('')
const debugLoading = ref(false)
const debugResult = ref<Awaited<ReturnType<typeof retrievalApi.debug>> | null>(null)
const debugError = ref<string>('')

async function runDebug(): Promise<void> {
  if (!debugQuery.value.trim() || !selectedBaseId.value) return
  debugLoading.value = true
  debugError.value = ''
  try {
    debugResult.value = await retrievalApi.debug({
      query: debugQuery.value,
      knowledge_base_id: selectedBaseId.value,
      top_k: 5,
      use_rerank: true,
    })
  } catch (e) {
    const normalized = normalizeError(e)
    debugError.value = normalized.message
    debugResult.value = null
  } finally {
    debugLoading.value = false
  }
}

// -- Evaluation --------------------------------------------------------------
const {
  data: evalData,
  execute: loadEvaluation,
} = useAsyncState(
  () => (selectedBaseId.value ? knowledgeAdminApi.evaluation(selectedBaseId.value) : Promise.resolve([])),
  { immediate: false },
)

watch(selectedBaseId, (id) => {
  if (id) void loadEvaluation()
}, { immediate: true })

function scorePercent(value: number): string {
  return `${Math.round(value * 100)}%`
}
</script>

<template>
  <div class="knowledge">
    <header class="knowledge__head">
      <h2 class="nx-section-title">知识中心</h2>
      <div class="knowledge__head-actions">
        <select v-model="selectedBaseId" class="knowledge__select" aria-label="选择知识库">
          <option v-for="base in bases" :key="base.id" :value="base.id">{{ base.name }}</option>
        </select>
        <button type="button" class="nx-btn" :disabled="!selectedBaseId || uploading" @click="uploadInput?.click()">
          上传文档
        </button>
        <input ref="uploadInput" type="file" hidden @change="onFileChange" />
      </div>
    </header>

    <StateView :state="baseStatus" :error="baseError" @retry="loadBases()">
      <section class="nx-card knowledge__docs">
        <div class="nx-card__body">
          <h3 class="nx-section-title">文档</h3>

          <!-- Knowledge surfaces must render Processing / Failed (§108). -->
          <StateView :state="docStatus" :error="docError" @retry="loadDocs()">
            <table class="knowledge__table">
              <thead>
                <tr>
                  <th>文件名</th>
                  <th>类型</th>
                  <th>大小</th>
                  <th>状态</th>
                  <th>分片</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="doc in documents" :key="doc.id">
                  <td class="knowledge__name">{{ doc.file_name }}</td>
                  <td>{{ doc.content_type }}</td>
                  <td>{{ Math.max(1, Math.round(doc.size_bytes / 1024)) }} KB</td>
                  <td>
                    <StatusChip :status="doc.status" kind="doc" />
                    <span v-if="doc.status === 'FAILED' && doc.error_message" class="knowledge__doc-error">
                      {{ doc.error_message }}
                    </span>
                  </td>
                  <td>{{ doc.chunk_count }}</td>
                  <td>
                    <button
                      v-if="doc.status === 'FAILED' || doc.status === 'READY'"
                      type="button"
                      class="nx-btn nx-btn--ghost"
                      @click="reprocess(doc.id)"
                    >
                      重新处理
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="documents.some((d) => d.status === 'PROCESSING')" class="nx-muted knowledge__polling">
              有文档正在解析，页面会自动刷新。
            </p>
          </StateView>
        </div>
      </section>
    </StateView>

    <section class="nx-card knowledge__debug">
      <div class="nx-card__body">
        <h3 class="nx-section-title">检索调试</h3>
        <div class="knowledge__debug-bar">
          <input
            v-model="debugQuery"
            class="knowledge__select knowledge__debug-input"
            placeholder="输入查询，查看各阶段召回结果"
            @keydown.enter="runDebug()"
          />
          <button type="button" class="nx-btn nx-btn--primary" :disabled="debugLoading" @click="runDebug()">
            {{ debugLoading ? '检索中…' : '开始检索' }}
          </button>
        </div>

        <p v-if="debugError" class="knowledge__debug-error">{{ debugError }}</p>

        <div v-if="debugResult" class="knowledge__stages">
          <p class="knowledge__rewritten">
            改写后查询：<strong>{{ debugResult.rewritten_query }}</strong>
            <span class="nx-muted">（总耗时 {{ debugResult.total_duration_ms }} ms）</span>
          </p>

          <article v-for="stage in debugResult.stages" :key="stage.stage" class="knowledge__stage">
            <header>
              <strong>{{ stage.stage }}</strong>
              <span class="nx-muted">{{ stage.candidates.length }} 条 · {{ stage.duration_ms }} ms</span>
            </header>
            <ol>
              <li v-for="hit in stage.candidates.slice(0, 5)" :key="`${stage.stage}-${hit.chunk_id}`">
                <span class="knowledge__score">{{ hit.score.toFixed(4) }}</span>
                <span class="knowledge__hit-doc">{{ hit.doc_name }}</span>
                <p>{{ hit.text }}</p>
              </li>
            </ol>
          </article>

          <article class="knowledge__stage knowledge__stage--final">
            <header><strong>final_evidence</strong></header>
            <ol>
              <li v-for="evidence in debugResult.final_evidence" :key="evidence.chunk_id">
                <span class="knowledge__score">{{ evidence.score.toFixed(4) }}</span>
                <span class="knowledge__hit-doc">{{ evidence.doc_name }}</span>
                <p>{{ evidence.snippet }}</p>
              </li>
            </ol>
          </article>
        </div>
      </div>
    </section>

    <section class="nx-card knowledge__eval">
      <div class="nx-card__body">
        <h3 class="nx-section-title">RAG 评测</h3>
        <table v-if="(evalData ?? []).length" class="knowledge__table">
          <thead>
            <tr>
              <th>数据集</th>
              <th>Recall@k</th>
              <th>Precision@k</th>
              <th>MRR</th>
              <th>NDCG</th>
              <th>样本数</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="result in evalData ?? []" :key="result.id">
              <td>{{ result.dataset_name }}</td>
              <td>{{ scorePercent(result.recall_at_k) }}</td>
              <td>{{ scorePercent(result.precision_at_k) }}</td>
              <td>{{ scorePercent(result.mrr) }}</td>
              <td>{{ scorePercent(result.ndcg) }}</td>
              <td>{{ result.sample_count }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="nx-muted">该知识库还没有评测记录。</p>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.knowledge {
  display: flex;
  flex-direction: column;
  gap: 16px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  &__head-actions {
    display: flex;
    gap: 8px;
  }

  &__select {
    height: 34px;
    padding: 0 10px;
    border: 1px solid var(--nx-border-strong);
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 13px;
  }

  &__table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12.5px;

    th,
    td {
      padding: 9px 12px;
      text-align: left;
      border-bottom: 1px solid var(--nx-border);
      vertical-align: top;
    }

    th {
      background: var(--nx-surface-sunken);
      font-weight: 600;
      color: var(--nx-text-secondary);
    }
  }

  &__name {
    font-weight: 600;
  }

  &__doc-error {
    display: block;
    margin-top: 4px;
    color: var(--nx-danger);
    font-size: 11.5px;
  }

  &__polling {
    margin: 10px 0 0;
    font-size: 11.5px;
  }

  &__debug-bar {
    display: flex;
    gap: 8px;
    margin-bottom: 12px;
  }

  &__debug-input {
    flex: 1;
  }

  &__debug-error {
    color: var(--nx-danger);
    font-size: 12.5px;
  }

  &__stages {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }

  &__rewritten {
    grid-column: 1 / -1;
    margin: 0;
    font-size: 13px;
  }

  &__stage {
    padding: 10px 12px;
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-stage);
    background: var(--nx-surface-sunken);

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      font-size: 12.5px;
      margin-bottom: 6px;
    }

    ol {
      margin: 0;
      padding: 0;
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    li {
      font-size: 12px;
    }

    p {
      margin: 2px 0 0;
      color: var(--nx-text-muted);
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    &--final {
      border-color: var(--nx-primary);
      background: var(--nx-primary-soft);
    }
  }

  &__score {
    margin-right: 8px;
    font-variant-numeric: tabular-nums;
    color: var(--nx-primary);
  }

  &__hit-doc {
    font-weight: 600;
  }
}
</style>
