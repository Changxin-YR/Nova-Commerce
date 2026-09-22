<script setup lang="ts">
/**
 * Cart. All totals come from the server's cart response — this page performs NO
 * arithmetic on money, because a client-computed total that disagrees with the
 * server is exactly the class of bug the backend's `ORDER_AMOUNT_MISMATCH` guard
 * exists to catch.
 */
import { computed, onMounted } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { useCartStore } from '@/stores/cart'
import { useNotificationStore } from '@/stores/notification'
import { formatMoney } from '@/utils/money'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'

const cart = useCartStore()
const router = useRouter()
const notifications = useNotificationStore()

const selectedIds = computed(() => cart.selectedItems.map((item) => item.id))
const allSelected = computed(
  () => cart.items.length > 0 && cart.items.every((item) => item.selected),
)

onMounted(() => {
  void cart.load().catch(() => undefined)
})

async function report(action: () => Promise<unknown>, title: string): Promise<void> {
  try {
    await action()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error(title, normalized.message, normalized.code, normalized.traceId)
  }
}

function toggleAll(): void {
  const next = !allSelected.value
  void report(
    () => cart.selectItems(cart.items.map((item) => item.id), next),
    '更新选择失败',
  )
}

function toggleOne(itemId: string, selected: boolean): void {
  void report(() => cart.selectItems([itemId], selected), '更新选择失败')
}
</script>

<template>
  <div class="nx-container cart">
    <h1 class="nx-page-title">购物车</h1>

    <StateView
      :state="cart.loading ? 'loading' : cart.error ? (cart.error.forbidden ? 'permission_denied' : 'error') : cart.isEmpty ? 'empty' : 'success'"
      :error="cart.error"
      :title="cart.isEmpty ? '购物车还是空的' : undefined"
      :description="cart.isEmpty ? '挑几件喜欢的 3C 好物再回来结算。' : undefined"
      @retry="cart.load()"
    >
      <div class="cart__layout">
        <div class="cart__list">
          <div class="cart__list-head">
            <label class="cart__check">
              <input type="checkbox" :checked="allSelected" @change="toggleAll()" />
              全选
            </label>
            <span class="nx-muted">共 {{ cart.itemCount }} 件</span>
          </div>

          <article
            v-for="item in cart.items"
            :key="item.id"
            class="cart__item"
            :class="{ 'cart__item--unavailable': !item.available }"
          >
            <label class="cart__check">
              <input
                type="checkbox"
                :checked="item.selected"
                :disabled="!item.available"
                @change="toggleOne(item.id, ($event.target as HTMLInputElement).checked)"
              />
            </label>

            <img v-if="item.cover_url" :src="item.cover_url" :alt="item.product_title" class="cart__thumb" />
            <div v-else class="cart__thumb cart__thumb--empty" aria-hidden="true">无图</div>

            <div class="cart__info">
              <RouterLink
                :to="{ name: 'product', params: { id: item.product_id } }"
                class="cart__title"
              >
                {{ item.product_title }}
              </RouterLink>
              <p class="nx-muted">
                {{ Object.values(item.sku_specs).join(' / ') || item.sku_id }}
              </p>
              <p v-if="!item.available" class="cart__warning">
                {{ item.unavailable_reason ?? '该商品当前不可购买' }}
              </p>
            </div>

            <div class="cart__price">
              <span class="nx-money">{{ formatMoney(item.unit_price_amount) }}</span>
            </div>

            <div class="cart__quantity">
              <button
                type="button"
                class="nx-btn"
                :disabled="item.quantity <= 1 || cart.mutating"
                @click="report(() => cart.updateQuantity(item.id, item.quantity - 1), '修改数量失败')"
              >
                −
              </button>
              <span class="cart__quantity-value">{{ item.quantity }}</span>
              <button
                type="button"
                class="nx-btn"
                :disabled="cart.mutating"
                @click="report(() => cart.updateQuantity(item.id, item.quantity + 1), '修改数量失败')"
              >
                +
              </button>
            </div>

            <div class="cart__subtotal">
              <span class="nx-money">{{ formatMoney(item.subtotal_amount) }}</span>
            </div>

            <button
              type="button"
              class="nx-btn nx-btn--ghost"
              :disabled="cart.mutating"
              @click="report(() => cart.removeItem(item.id), '删除失败')"
            >
              删除
            </button>
          </article>
        </div>

        <aside class="cart__summary nx-card">
          <div class="nx-card__body">
            <h2 class="nx-section-title">结算信息</h2>
            <dl class="cart__totals">
              <div>
                <dt>已选商品</dt>
                <dd>{{ selectedIds.length }} 件</dd>
              </div>
              <div>
                <dt>商品金额</dt>
                <dd class="nx-money">{{ formatMoney(cart.selectedAmount) }}</dd>
              </div>
              <div class="cart__totals-note">
                <dt>运费 / 优惠</dt>
                <dd class="nx-muted">提交订单时由服务端计算</dd>
              </div>
            </dl>

            <button
              type="button"
              class="nx-btn nx-btn--primary cart__checkout"
              :disabled="selectedIds.length === 0"
              @click="router.push({ name: 'checkout' })"
            >
              去结算
            </button>
            <p class="nx-muted cart__hint">应付金额以服务端预览结果为准，页面不做本地计算。</p>
          </div>
        </aside>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.cart {
  &__layout {
    display: grid;
    grid-template-columns: 1fr 300px;
    gap: 20px;
    margin-top: 16px;
  }

  &__list {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  &__list-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 4px;
  }

  &__check {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
  }

  &__item {
    display: grid;
    grid-template-columns: 28px 68px 1fr auto auto auto auto;
    align-items: center;
    gap: 12px;
    padding: 12px 14px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-stage);

    &--unavailable {
      opacity: 0.6;
    }
  }

  &__thumb {
    width: 68px;
    height: 68px;
    border-radius: 10px;
    background: var(--nx-surface-stage);
    object-fit: contain;

    &--empty {
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--nx-text-muted);
      font-size: 11px;
    }
  }

  &__title {
    color: var(--nx-text);
    text-decoration: none;
    font-size: 14px;
    font-weight: 600;

    &:hover {
      color: var(--nx-primary);
    }
  }

  &__warning {
    margin: 4px 0 0;
    color: var(--nx-warning);
    font-size: 12px;
  }

  &__quantity {
    display: flex;
    align-items: center;
    gap: 6px;

    .nx-btn {
      min-width: 30px;
      padding: 0 8px;
    }
  }

  &__quantity-value {
    min-width: 24px;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }

  &__subtotal {
    min-width: 90px;
    text-align: right;
    color: var(--nx-danger);
  }

  &__totals {
    margin: 0 0 16px;

    > div {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 0;
      font-size: 13px;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
    }
  }

  &__totals-note dd {
    font-size: 12px;
  }

  &__checkout {
    width: 100%;
    min-height: 40px;
  }

  &__hint {
    margin: 8px 0 0;
    font-size: 12px;
  }
}

@media (max-width: 960px) {
  .cart__layout {
    grid-template-columns: 1fr;
  }
}
</style>
