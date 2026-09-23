<script setup lang="ts">
/**
 * Cart — the checkout-conversion page, so it uses the commercial conventions:
 *   * a hairline table (checkbox | product | unit price | quantity | subtotal | action)
 *   * a quantity stepper, not a free-text field
 *   * a STICKY action bar with the selected count, the total and the checkout button
 *
 * All totals come from the server's cart response. This page performs NO money
 * arithmetic — a client total that disagrees with the server is exactly what
 * `ORDER_AMOUNT_MISMATCH` exists to catch. `<PriceText>` only formats.
 */
import { computed, onMounted } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { useCartStore } from '@/stores/cart'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import PriceText from '@/components/ui/PriceText.vue'

const cart = useCartStore()
const router = useRouter()
const notifications = useNotificationStore()

const selectedIds = computed(() => cart.selectedItems.map((item) => item.id))
const selectedCount = computed(() => cart.selectedItems.length)
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
  void report(() => cart.selectItems(cart.items.map((item) => item.id), next), '更新选择失败')
}

function toggleOne(itemId: string, selected: boolean): void {
  void report(() => cart.selectItems([itemId], selected), '更新选择失败')
}

const status = computed(() => {
  if (cart.loading) return 'loading' as const
  if (cart.error) return cart.error.forbidden ? ('permission_denied' as const) : ('error' as const)
  return cart.isEmpty ? ('empty' as const) : ('success' as const)
})
</script>

<template>
  <div class="cart">
    <div class="nx-container">
      <h1 class="cart__page-title">
        我的购物车
        <span class="nx-muted cart__count">共 {{ cart.itemCount }} 件商品</span>
      </h1>

      <StateView
        :state="status"
        :error="cart.error"
        :title="cart.isEmpty ? '购物车还是空的' : undefined"
        :description="cart.isEmpty ? '挑几件喜欢的 3C 好物再回来结算。' : undefined"
        @retry="cart.load()"
      >
        <div class="cart__wrap">
          <!-- header row -->
          <div class="cart__head">
            <label class="cart__check">
              <input type="checkbox" :checked="allSelected" @change="toggleAll()" />
              <span>全选</span>
            </label>
            <span class="cart__col cart__col--product">商品信息</span>
            <span class="cart__col cart__col--price">单价</span>
            <span class="cart__col cart__col--qty">数量</span>
            <span class="cart__col cart__col--sub">小计</span>
            <span class="cart__col cart__col--action">操作</span>
          </div>

          <!-- item rows -->
          <article
            v-for="item in cart.items"
            :key="item.id"
            class="cart__row"
            :class="{ 'cart__row--unavailable': !item.available }"
          >
            <label class="cart__check">
              <input
                type="checkbox"
                :checked="item.selected"
                :disabled="!item.available"
                @change="toggleOne(item.id, ($event.target as HTMLInputElement).checked)"
              />
            </label>

            <div class="cart__product">
              <RouterLink
                :to="{ name: 'product', params: { id: item.product_id } }"
                class="cart__thumb-link"
              >
                <img v-if="item.cover_url" :src="item.cover_url" :alt="item.product_title" class="cart__thumb" />
                <span v-else class="cart__thumb cart__thumb--empty" aria-hidden="true">暂无图片</span>
              </RouterLink>

              <div class="cart__info">
                <RouterLink
                  :to="{ name: 'product', params: { id: item.product_id } }"
                  class="cart__title"
                >
                  {{ item.product_title }}
                </RouterLink>
                <p class="cart__specs">{{ Object.values(item.sku_specs).join(' / ') || item.sku_id }}</p>
                <p v-if="!item.available" class="cart__warning">
                  {{ item.unavailable_reason ?? '该商品当前不可购买' }}
                </p>
              </div>
            </div>

            <div class="cart__price">
              <PriceText :amount="item.unit_price_amount" size="sm" muted />
            </div>

            <div class="cart__qty">
              <div class="qty">
                <button
                  type="button"
                  class="qty__btn"
                  :disabled="item.quantity <= 1 || cart.mutating"
                  @click="report(() => cart.updateQuantity(item.id, item.quantity - 1), '修改数量失败')"
                >
                  −
                </button>
                <span class="qty__value">{{ item.quantity }}</span>
                <button
                  type="button"
                  class="qty__btn"
                  :disabled="cart.mutating"
                  @click="report(() => cart.updateQuantity(item.id, item.quantity + 1), '修改数量失败')"
                >
                  +
                </button>
              </div>
            </div>

            <div class="cart__sub">
              <PriceText :amount="item.subtotal_amount" size="md" />
            </div>

            <div class="cart__action">
              <button
                type="button"
                class="nx-btn nx-btn--text"
                :disabled="cart.mutating"
                @click="report(() => cart.removeItem(item.id), '删除失败')"
              >
                删除
              </button>
            </div>
          </article>
        </div>

        <!-- sticky settlement bar -->
        <div class="cart__bar">
          <label class="cart__check cart__bar-check">
            <input type="checkbox" :checked="allSelected" @change="toggleAll()" />
            <span>全选</span>
          </label>
          <button
            type="button"
            class="nx-btn nx-btn--text cart__bar-clear"
            :disabled="cart.mutating || cart.isEmpty"
            @click="report(() => cart.clear(), '清空失败')"
          >
            清空购物车
          </button>

          <RouterLink :to="{ name: 'addresses' }" class="cart__bar-link">管理收货地址</RouterLink>

          <div class="cart__bar-right">
            <span class="cart__bar-summary">
              已选 <b>{{ selectedCount }}</b> 件，合计
            </span>
            <PriceText :amount="cart.selectedAmount" size="lg" class="cart__bar-total" />

            <button
              type="button"
              class="nx-btn nx-btn--primary nx-btn--lg cart__bar-checkout"
              :disabled="selectedIds.length === 0"
              @click="router.push({ name: 'checkout' })"
            >
              去结算
            </button>
          </div>
        </div>

        <p class="nx-muted cart__note">
          合计金额由服务端计算并返回，页面不做本地金额计算；提交订单时会再由服务端复核一次。
        </p>
      </StateView>
    </div>
  </div>
</template>

<style scoped lang="scss">
.cart {
  &__page-title {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin: 0 0 10px;
    font-size: 18px;
    font-weight: 700;
  }

  &__count {
    font-size: 12px;
    font-weight: 400;
  }

  &__wrap {
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
  }

  /* -- table-like grid ---------------------------------------------------- */
  &__head,
  &__row {
    display: grid;
    grid-template-columns: 40px 1fr 110px 130px 120px 80px;
    align-items: center;
    gap: 10px;
    padding: 0 12px;
  }

  &__head {
    height: 38px;
    background: var(--nx-surface-sunken);
    border-bottom: 1px solid var(--nx-border);
    color: var(--nx-text-secondary);
    font-size: 12px;
  }

  &__col--price,
  &__col--qty,
  &__col--sub,
  &__col--action {
    text-align: center;
  }

  &__row {
    padding-top: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--nx-border);

    &:last-child {
      border-bottom: none;
    }

    &--unavailable {
      background: var(--nx-surface-sunken);
      opacity: 0.85;
    }
  }

  &__check {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    cursor: pointer;
  }

  &__product {
    display: flex;
    gap: 10px;
    min-width: 0;
  }

  &__thumb-link {
    flex: 0 0 auto;
  }

  &__thumb {
    width: 70px;
    height: 70px;
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
    min-width: 0;
  }

  &__title {
    display: -webkit-box;
    overflow: hidden;
    color: var(--nx-text);
    font-size: 12px;
    line-height: 18px;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;

    &:hover {
      color: var(--nx-brand);
    }
  }

  &__specs {
    margin: 4px 0 0;
    color: var(--nx-text-muted);
    font-size: 12px;
  }

  &__warning {
    margin: 4px 0 0;
    color: var(--nx-warning);
    font-size: 12px;
  }

  &__price,
  &__sub {
    text-align: center;
  }

  &__qty {
    display: flex;
    justify-content: center;
  }

  &__action {
    text-align: center;
  }

  /* -- sticky settlement bar --------------------------------------------- */
  &__bar {
    position: sticky;
    bottom: 0;
    z-index: 20;
    display: flex;
    align-items: center;
    gap: 14px;
    height: 56px;
    margin-top: 1px;
    padding: 0 12px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.06);
  }

  &__bar-check {
    font-size: 13px;
  }

  &__bar-clear,
  &__bar-link {
    font-size: 12px;
  }

  &__bar-right {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-left: auto;
  }

  &__bar-summary {
    font-size: 12px;
    color: var(--nx-text-secondary);

    b {
      color: var(--nx-brand);
      font-weight: 700;
    }
  }

  &__bar-checkout {
    min-width: 140px;
  }

  &__note {
    margin: 8px 0 0;
    font-size: 12px;
  }
}

/* -- square stepper (shared shape with the product page) ------------------- */
.qty {
  display: flex;
  border: 1px solid var(--nx-border-strong);

  &__btn {
    width: 26px;
    height: 28px;
    border: none;
    background: var(--nx-surface-sunken);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 14px;
    cursor: pointer;

    &:hover:not(:disabled) {
      color: var(--nx-brand);
    }

    &:disabled {
      color: var(--nx-text-muted);
      cursor: not-allowed;
    }
  }

  &__value {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 40px;
    height: 28px;
    border-left: 1px solid var(--nx-border-strong);
    border-right: 1px solid var(--nx-border-strong);
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }
}

@media (max-width: 1000px) {
  .cart__head {
    display: none;
  }

  .cart__row {
    grid-template-columns: 30px 1fr;
    row-gap: 8px;
  }

  .cart__price,
  .cart__qty,
  .cart__sub,
  .cart__action {
    grid-column: 2;
    justify-content: flex-start;
    text-align: left;
  }
}
</style>
