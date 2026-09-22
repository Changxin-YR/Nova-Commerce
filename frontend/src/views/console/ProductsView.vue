<script setup lang="ts">
/**
 * Console product list with TASK-BASED transitions (§99).
 *
 * Publish and unpublish are explicit POST intents (`/publish`, `/unpublish`), never a
 * `PATCH {status}`. Two consequences the UI shows: the action is idempotent-ish at the
 * intent level, and the server — not the button's enabled state — decides whether the
 * transition is legal (`PRODUCT_STATE_INVALID` / `PRODUCT_ALREADY_PUBLISHED`).
 */
import { computed, ref } from 'vue'
import { catalogAdminApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { formatMoney } from '@/utils/money'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'

const notifications = useNotificationStore()

const keyword = ref('')
const statusFilter = ref('')
const page = ref(1)
const busyId = ref('')

const {
  data: pageData,
  status,
  error,
  execute,
} = useAsyncState(
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

async function transition(id: string, action: 'publish' | 'unpublish'): Promise<void> {
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
    busyId.value = ''
  }
}

function search(): void {
  page.value = 1
  void execute()
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
  <div class="products">
    <div class="products__head">
      <h2 class="nx-section-title">商品管理</h2>
      <div class="products__filters">
        <input
          v-model="keyword"
          class="products__input"
          placeholder="按名称搜索"
          @keydown.enter="search()"
        />
        <select v-model="statusFilter" class="products__input" @change="search()">
          <option value="">全部状态</option>
          <option value="DRAFT">草稿</option>
          <option value="PUBLISHED">已上架</option>
          <option value="UNPUBLISHED">已下架</option>
          <option value="ARCHIVED">已归档</option>
        </select>
        <button type="button" class="nx-btn" @click="search()">查询</button>
      </div>
    </div>

    <StateView :state="status" :error="error" @retry="execute()">
      <table class="products__table">
        <thead>
          <tr>
            <th>商品</th>
            <th>价格区间</th>
            <th>状态</th>
            <th>销量</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="product in products" :key="product.id">
            <td>
              <div class="products__cell">
                <img v-if="product.cover_url" :src="product.cover_url" :alt="product.title" />
                <span v-else class="products__no-image" aria-hidden="true">无图</span>
                <div>
                  <strong>{{ product.title }}</strong>
                  <p class="nx-muted">{{ product.brand_name ?? '—' }}</p>
                </div>
              </div>
            </td>
            <td class="nx-money">
              {{ formatMoney(product.min_price_amount) }}
              <template v-if="product.original_price_amount">
                <span class="products__was">/ {{ formatMoney(product.original_price_amount) }}</span>
              </template>
            </td>
            <td><StatusChip :status="product.status" /></td>
            <td>{{ product.sales_count ?? 0 }}</td>
            <td class="products__actions">
              <button
                type="button"
                class="nx-btn nx-btn--ghost"
                :disabled="busyId === product.id"
                @click="transition(product.id, 'publish')"
              >
                上架
              </button>
              <button
                type="button"
                class="nx-btn nx-btn--ghost"
                :disabled="busyId === product.id"
                @click="transition(product.id, 'unpublish')"
              >
                下架
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="meta" class="products__pager">
        <button type="button" class="nx-btn" :disabled="page <= 1" @click="changePage(-1)">上一页</button>
        <span class="nx-muted">第 {{ meta.page }} / {{ meta.total_pages }} 页 · 共 {{ meta.total }} 条</span>
        <button
          type="button"
          class="nx-btn"
          :disabled="meta.total_pages > 0 && page >= meta.total_pages"
          @click="changePage(1)"
        >
          下一页
        </button>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.products {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }

  &__filters {
    display: flex;
    gap: 8px;
  }

  &__input {
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
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-stage);
    overflow: hidden;
    font-size: 12.5px;

    th,
    td {
      padding: 10px 12px;
      text-align: left;
      border-bottom: 1px solid var(--nx-border);
    }

    th {
      background: var(--nx-surface-sunken);
      font-weight: 600;
      color: var(--nx-text-secondary);
    }

    tr:last-child td {
      border-bottom: none;
    }
  }

  &__cell {
    display: flex;
    align-items: center;
    gap: 10px;

    img,
    .products__no-image {
      width: 40px;
      height: 40px;
      border-radius: 8px;
      background: var(--nx-surface-stage);
      object-fit: contain;
    }

    .products__no-image {
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--nx-text-muted);
      font-size: 10px;
    }

    p {
      margin: 2px 0 0;
      font-size: 11.5px;
    }
  }

  &__was {
    color: var(--nx-text-muted);
    font-weight: 400;
  }

  &__actions {
    display: flex;
    gap: 4px;
  }

  &__pager {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 14px;
  }
}
</style>
