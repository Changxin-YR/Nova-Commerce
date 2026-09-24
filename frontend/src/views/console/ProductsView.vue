<script setup lang="ts">
/**
 * Console product list — the reference for the operations-table pattern that every
 * console page follows:
 *
 *   [ filter bar (query + selects + actions) ] -> [ dense hairline table ] -> [ pager ]
 *
 * Density is the point: 12px type, 8px row padding, header row tinted, row hover, and
 * every column right-aligned when it holds a number so figures line up when scanned.
 *
 * Publish/unpublish are TASK endpoints (§99), never `PATCH { status }`. The server owns
 * the legality of the transition (`PRODUCT_STATE_INVALID` / `PRODUCT_ALREADY_PUBLISHED`),
 * so the UI exposes the intent and reports the outcome.
 */
import { computed, ref } from 'vue'
import { catalogAdminApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'
import PriceText from '@/components/ui/PriceText.vue'

const notifications = useNotificationStore()

const keyword = ref('')
const statusFilter = ref('')
const page = ref(1)
const busyId = ref<number | null>(null)
/** Set of row ids selected via checkboxes (page-local state, so it stays here: §105). */
const selected = ref<number[]>([])

const { data: pageData, status, error, execute } = useAsyncState(
  () =>
    catalogAdminApi.products({
      page: page.value,
      page_size: 20,
      keyword: keyword.value || undefined,
      status: statusFilter.value || undefined,
    }),
  { immediate: true },
)

const products = computed(() => pageData.value?.items ?? [])
const meta = computed(() => pageData.value?.meta ?? null)
const allChecked = computed(
  () => products.value.length > 0 && selected.value.length === products.value.length,
)

function toggleAll(): void {
  selected.value = allChecked.value ? [] : products.value.map((p) => p.id)
}

function toggleOne(id: number): void {
  selected.value = selected.value.includes(id)
    ? selected.value.filter((x) => x !== id)
    : [...selected.value, id]
}

function search(): void {
  page.value = 1
  selected.value = []
  void execute()
}

async function transition(id: number, action: 'publish' | 'unpublish'): Promise<void> {
  busyId.value = id
  try {
    if (action === 'publish') await catalogAdminApi.publish(id)
    else await catalogAdminApi.unpublish(id)
    notifications.success(action === 'publish' ? '商品已上架' : '商品已下架')
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('操作失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busyId.value = null
  }
}

function changePage(delta: number): void {
  const next = page.value + delta
  if (next < 1) return
  if (meta.value && next > meta.value.total_pages) return
  page.value = next
  void execute()
}
</script>

<template>
  <div class="p-list">
    <div class="nx-block">
      <!-- filter bar --------------------------------------------------- -->
      <div class="nx-filterbar">
        <label>
          商品名称
          <input v-model="keyword" class="nx-input" placeholder="输入名称搜索" @keydown.enter="search()" />
        </label>
        <label>
          状态
          <select v-model="statusFilter" class="nx-input" @change="search()">
            <option value="">全部</option>
            <option value="DRAFT">草稿</option>
            <option value="PUBLISHED">已上架</option>
            <option value="UNPUBLISHED">已下架</option>
            <option value="ARCHIVED">已归档</option>
          </select>
        </label>
        <button type="button" class="nx-btn nx-btn--primary nx-btn--sm" @click="search()">查询</button>
        <button type="button" class="nx-btn nx-btn--sm" @click="keyword = ''; statusFilter = ''; search()">
          重置
        </button>
        <span class="nx-filterbar__spacer nx-muted">
          已选 {{ selected.length }} 项{{ meta ? ` · 共 ${meta.total} 条` : '' }}
        </span>
      </div>

      <StateView :state="status" :error="error" @retry="execute()">
        <table class="nx-table">
          <thead>
            <tr>
              <th style="width: 36px">
                <input type="checkbox" :checked="allChecked" @change="toggleAll()" />
              </th>
              <th>商品信息</th>
              <th style="width: 150px; text-align: right">价格</th>
              <th style="width: 100px; text-align: right">销量</th>
              <th style="width: 100px">状态</th>
              <th style="width: 120px">商品 ID</th>
              <th style="width: 140px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="product in products" :key="product.id">
              <td>
                <input
                  type="checkbox"
                  :checked="selected.includes(product.id)"
                  @change="toggleOne(product.id)"
                />
              </td>
              <td>
                <div class="p-list__cell">
                  <img
                    v-if="product.cover_url"
                    :src="product.cover_url"
                    :alt="product.title"
                    class="p-list__thumb"
                  />
                  <span v-else class="p-list__thumb p-list__thumb--empty" aria-hidden="true">无图</span>
                  <div class="p-list__info">
                    <span class="p-list__title" :title="product.title">{{ product.title }}</span>
                    <span class="nx-muted">{{ product.brand_name ?? '未设置品牌' }}</span>
                  </div>
                </div>
              </td>
              <td style="text-align: right">
                <PriceText
                  :amount="product.min_price_amount"
                  :original-amount="product.original_price_amount"
                  size="sm"
                  :grouping="false"
                />
              </td>
              <td class="nx-num" style="text-align: right">{{ product.sales_count ?? 0 }}</td>
              <td><StatusChip :status="product.status" dot /></td>
              <td><code class="p-list__id">{{ product.id }}</code></td>
              <td>
                <div class="p-list__actions">
                  <button
                    type="button"
                    class="nx-btn nx-btn--text"
                    :disabled="busyId === product.id"
                    @click="transition(product.id, 'publish')"
                  >
                    上架
                  </button>
                  <button
                    type="button"
                    class="nx-btn nx-btn--text"
                    :disabled="busyId === product.id"
                    @click="transition(product.id, 'unpublish')"
                  >
                    下架
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- pager ------------------------------------------------------ -->
        <div v-if="meta" class="p-list__pager">
          <button type="button" class="nx-btn nx-btn--sm" :disabled="page <= 1" @click="changePage(-1)">
            上一页
          </button>
          <span class="nx-muted">第 {{ meta.page }} / {{ meta.total_pages }} 页 · 共 {{ meta.total }} 条</span>
          <button
            type="button"
            class="nx-btn nx-btn--sm"
            :disabled="meta.total_pages > 0 && page >= meta.total_pages"
            @click="changePage(1)"
          >
            下一页
          </button>
        </div>
      </StateView>
    </div>
  </div>
</template>

<style scoped lang="scss">
.p-list {
  &__cell {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  &__thumb {
    width: 36px;
    height: 36px;
    flex: 0 0 36px;
    border: 1px solid var(--nx-border);
    background: var(--nx-surface-stage);
    object-fit: contain;

    &--empty {
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--nx-text-muted);
      font-size: 12px;
    }
  }

  &__info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  &__title {
    overflow: hidden;
    color: var(--nx-text);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 380px;
  }

  &__id {
    color: var(--nx-text-muted);
    font-size: 12px;
  }

  &__actions {
    display: flex;
    gap: 8px;
  }

  &__pager {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-top: 1px solid var(--nx-border);
    font-size: 12px;
  }
}
</style>
