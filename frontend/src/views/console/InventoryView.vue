<script setup lang="ts">
/**
 * Console inventory with OPTIMISTIC LOCKING (§98).
 *
 * Every adjustment echoes the `version` the merchant last saw. If someone else changed
 * the row in the meantime, the server answers
 * `INVENTORY_CONFLICT_STALE_VERSION` (40002) and we REFUSE to retry automatically —
 * auto-retrying with a bumped version would silently overwrite the other operator's
 * change. The UI tells the user to refresh instead.
 */
import { computed, reactive, ref } from 'vue'
import { inventoryAdminApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import { newTraceId } from '@/utils/trace'
import StateView from '@/components/ui/StateView.vue'
import type { StockRow } from '@/api/inventory'

const notifications = useNotificationStore()

const page = ref(1)
const lowStockOnly = ref(false)

const {
  data: stockData,
  status,
  error,
  execute,
} = useAsyncState(
  () => inventoryAdminApi.stock({ page: page.value, page_size: 20, low_stock_only: lowStockOnly.value || undefined }),
  { immediate: true },
)

const rows = computed(() => stockData.value?.items ?? [])
const meta = computed(() => stockData.value?.meta ?? null)

const editing = ref<StockRow | null>(null)
const busy = ref(false)

const form = reactive({ delta: 0, reason: '' })

function openAdjust(row: StockRow): void {
  editing.value = row
  form.delta = 0
  form.reason = ''
}

async function submitAdjust(): Promise<void> {
  const row = editing.value
  if (!row) return
  if (!form.reason.trim()) {
    notifications.warning('请填写调整原因', '库存变动会写入流水，原因必填')
    return
  }
  busy.value = true
  try {
    await inventoryAdminApi.adjust(row.sku_id, {
      delta: form.delta,
      reason: form.reason,
      // Echo the version we rendered. This is the optimistic-lock token.
      version: row.version,
      idempotency_key: `adj-${newTraceId()}`,
    })
    notifications.success('库存已调整')
    editing.value = null
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    if (normalized.code === 40_002) {
      notifications.warning(
        '库存已被其他人修改',
        '本行数据已过期，请刷新后基于最新版本重新调整（不会自动重试，以免覆盖他人改动）。',
      )
      editing.value = null
      await execute()
    } else {
      notifications.error('调整失败', normalized.message, normalized.code, normalized.traceId)
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="inventory">
    <div class="inventory__head">
      <h2 class="nx-section-title">库存管理</h2>
      <label class="inventory__toggle">
        <input v-model="lowStockOnly" type="checkbox" @change="page = 1; execute()" />
        仅看低库存
      </label>
    </div>

    <StateView :state="status" :error="error" @retry="execute()">
      <table class="nx-table">
        <thead>
          <tr>
            <th>SKU</th>
            <th>商品</th>
            <th>在库</th>
            <th>预占</th>
            <th>可售</th>
            <th>版本</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.sku_id">
            <td><code>{{ row.sku_code }}</code></td>
            <td>
              <strong>{{ row.product_title }}</strong>
              <p class="nx-muted">{{ Object.values(row.specs).join(' / ') }}</p>
            </td>
            <td>{{ row.on_hand }}</td>
            <td>{{ row.reserved }}</td>
            <td class="inventory__sellable">{{ row.on_hand - row.reserved }}</td>
            <td><span class="inventory__version">v{{ row.version }}</span></td>
            <td>
              <button type="button" class="nx-btn nx-btn--ghost" @click="openAdjust(row)">调整</button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="meta" class="inventory__pager">
        <button type="button" class="nx-btn" :disabled="page <= 1" @click="page -= 1; execute()">上一页</button>
        <span class="nx-muted">第 {{ meta.page }} / {{ meta.total_pages }} 页</span>
        <button
          type="button"
          class="nx-btn"
          :disabled="meta.total_pages > 0 && page >= meta.total_pages"
          @click="page += 1; execute()"
        >
          下一页
        </button>
      </div>
    </StateView>

    <div v-if="editing" class="inventory__modal" role="dialog" aria-modal="true" aria-label="调整库存">
      <div class="inventory__modal-card nx-card">
        <div class="nx-card__body">
          <h3 class="nx-section-title">调整库存 · {{ editing.sku_code }}</h3>
          <p class="nx-muted">
            当前在库 {{ editing.on_hand }}，预占 {{ editing.reserved }}，版本 v{{ editing.version }}
          </p>

          <label class="inventory__field">
            <span>调整数量（正数入库，负数出库）</span>
            <input v-model.number="form.delta" type="number" />
          </label>

          <label class="inventory__field">
            <span>原因</span>
            <input v-model="form.reason" maxlength="100" placeholder="例如：盘点修正 / 采购入库" />
          </label>

          <div class="inventory__modal-actions">
            <button type="button" class="nx-btn nx-btn--primary" :disabled="busy" @click="submitAdjust()">
              提交
            </button>
            <button type="button" class="nx-btn" :disabled="busy" @click="editing = null">取消</button>
          </div>

          <p class="nx-muted inventory__note">
            提交时会带上版本号 v{{ editing.version }}；若期间被他人修改，服务端会拒绝本次调整（乐观锁），
            需要刷新后重做。
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.inventory {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }

  &__toggle {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
  }


  &__sellable {
    font-weight: 600;
  }

  &__version {
    padding: 1px 7px;
    border-radius: var(--nx-radius-pill);
    background: var(--nx-surface-sunken);
    color: var(--nx-text-muted);
    font-size: 11px;
  }

  &__pager {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 14px;
  }

  &__modal {
    position: fixed;
    inset: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.45);
  }

  &__modal-card {
    width: min(440px, 92vw);
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin: 12px 0;
    font-size: 13px;

    input {
      height: 34px;
      padding: 0 10px;
      border: 1px solid var(--nx-border-strong);
      border-radius: var(--nx-radius-control);
      background: var(--nx-surface);
      color: var(--nx-text);
      font-family: inherit;
      font-size: 13px;
    }
  }

  &__modal-actions {
    display: flex;
    gap: 8px;
  }

  &__note {
    margin: 12px 0 0;
    font-size: 11.5px;
    line-height: 1.6;
  }
}
</style>
