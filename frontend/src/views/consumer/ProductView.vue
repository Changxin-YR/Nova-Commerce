<script setup lang="ts">
/**
 * Product detail: gallery, SKU picker, quantity, add-to-cart / buy-now.
 *
 * Stock is DISPLAY ONLY. `available_stock` comes from the server for UX; the real
 * check happens at order creation, where `INSUFFICIENT_STOCK` (40000) is enforced
 * inside the transaction. The UI must therefore still handle that error even when the
 * button looked enabled.
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { catalogApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useCartStore } from '@/stores/cart'
import { useNotificationStore } from '@/stores/notification'
import { formatMoney } from '@/utils/money'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'

const route = useRoute()
const router = useRouter()
const cart = useCartStore()
const notifications = useNotificationStore()

const productId = computed(() => String(route.params.id ?? ''))

const { data: product, status, error, execute } = useAsyncState(
  () => catalogApi.product(productId.value),
  { immediate: true },
)

const selectedSkuId = ref<string>('')
const quantity = ref(1)
const activeImage = ref(0)
const submitting = ref(false)

const skus = computed(() => product.value?.skus ?? [])
const selectedSku = computed(() => skus.value.find((sku) => sku.id === selectedSkuId.value) ?? null)
const images = computed(() => product.value?.images ?? [])
const currentImage = computed(() => images.value[activeImage.value] ?? images.value[0] ?? null)

/** Sellable quantity as reported by the server (not a client-side stock computation). */
const maxQuantity = computed(() => Math.max(1, selectedSku.value?.available_stock ?? 1))
const soldOut = computed(() => (selectedSku.value?.available_stock ?? 0) <= 0)

// Default to the first available SKU once the product loads.
function ensureSkuSelected(): void {
  if (selectedSkuId.value) return
  const first = skus.value.find((sku) => (sku.available_stock ?? 0) > 0) ?? skus.value[0]
  if (first) selectedSkuId.value = first.id
}

// The product arrives asynchronously, so seed the SKU selection when it lands.
watch(product, ensureSkuSelected, { immediate: true })

async function addToCart(): Promise<void> {
  if (!selectedSku.value || soldOut.value) return
  submitting.value = true
  try {
    await cart.addItem(productId.value, selectedSku.value.id, quantity.value)
    notifications.success('已加入购物车', `${product.value?.title ?? ''} × ${quantity.value}`)
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('加入购物车失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    submitting.value = false
  }
}

async function buyNow(): Promise<void> {
  // Buy-now still goes through the server-side cart/order preview: the client never
  // invents a price or an order. `cart.error` is store state (already unwrapped by
  // Pinia), not a ref.
  await addToCart()
  if (!cart.error) await router.push({ name: 'cart' })
}
</script>

<template>
  <div class="nx-container product">
    <StateView :state="status" :error="error" @retry="execute()">
      <div v-if="product" class="product__layout">
        <div class="product__gallery">
          <div class="product__stage">
            <img v-if="currentImage" :src="currentImage.url" :alt="currentImage.alt ?? product.title" />
            <span v-else class="nx-muted">暂无图片</span>
          </div>
          <div v-if="images.length > 1" class="product__thumbs">
            <button
              v-for="(image, index) in images"
              :key="image.id"
              type="button"
              class="product__thumb"
              :class="{ 'product__thumb--active': index === activeImage }"
              @click="activeImage = index"
            >
              <img :src="image.url" :alt="`图片 ${index + 1}`" />
            </button>
          </div>
        </div>

        <div class="product__info">
          <h1 class="nx-page-title">{{ product.title }}</h1>
          <p v-if="product.subtitle" class="nx-muted">{{ product.subtitle }}</p>

          <div class="product__price">
            <span class="nx-money product__price-main">
              {{ formatMoney(selectedSku?.price_amount ?? product.min_price_amount) }}
            </span>
            <span
              v-if="selectedSku?.original_price_amount ?? product.original_price_amount"
              class="product__price-original"
            >
              {{ formatMoney(selectedSku?.original_price_amount ?? product.original_price_amount ?? 0) }}
            </span>
          </div>

          <div v-if="skus.length" class="product__skus">
            <p class="product__label">选择规格</p>
            <div class="product__sku-list">
              <button
                v-for="sku in skus"
                :key="sku.id"
                type="button"
                class="product__sku"
                :class="{
                  'product__sku--active': sku.id === selectedSkuId,
                  'product__sku--disabled': (sku.available_stock ?? 0) <= 0,
                }"
                :disabled="(sku.available_stock ?? 0) <= 0"
                @click="selectedSkuId = sku.id"
              >
                <span v-for="(value, key) in sku.specs" :key="key" class="product__sku-spec">
                  {{ value }}
                </span>
                <span class="product__sku-price">{{ formatMoney(sku.price_amount) }}</span>
              </button>
            </div>
          </div>

          <div class="product__quantity">
            <p class="product__label">数量</p>
            <div class="product__quantity-control">
              <button type="button" class="nx-btn" :disabled="quantity <= 1" @click="quantity -= 1">−</button>
              <input v-model.number="quantity" type="number" min="1" :max="maxQuantity" class="product__quantity-input" />
              <button
                type="button"
                class="nx-btn"
                :disabled="quantity >= maxQuantity"
                @click="quantity += 1"
              >
                +
              </button>
              <span class="nx-muted">
                {{ soldOut ? '该规格暂时缺货' : `可售 ${selectedSku?.available_stock ?? 0} 件` }}
              </span>
            </div>
          </div>

          <div class="product__actions">
            <button
              type="button"
              class="nx-btn nx-btn--primary"
              :disabled="submitting || soldOut"
              @click="addToCart()"
            >
              加入购物车
            </button>
            <button type="button" class="nx-btn" :disabled="submitting || soldOut" @click="buyNow()">
              立即购买
            </button>
          </div>

          <p class="nx-muted product__note">
            库存以提交订单时服务器校验为准；下单价格在创建订单时快照，后续改价不影响已有订单。
          </p>
        </div>
      </div>

      <section v-if="product?.description" class="product__description">
        <h2 class="nx-section-title">商品详情</h2>
        <p>{{ product.description }}</p>
      </section>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.product {
  &__layout {
    display: grid;
    grid-template-columns: minmax(280px, 420px) 1fr;
    gap: 28px;
  }

  &__stage {
    display: flex;
    align-items: center;
    justify-content: center;
    aspect-ratio: 1 / 1;
    background: var(--nx-surface-stage);
    border-radius: var(--nx-radius-card);
    overflow: hidden;

    img {
      width: 100%;
      height: 100%;
      object-fit: contain;
    }
  }

  &__thumbs {
    display: flex;
    gap: 8px;
    margin-top: 10px;
  }

  &__thumb {
    width: 56px;
    height: 56px;
    padding: 2px;
    border: 1px solid var(--nx-border);
    border-radius: 10px;
    background: var(--nx-surface);
    cursor: pointer;

    img {
      width: 100%;
      height: 100%;
      object-fit: contain;
    }

    &--active {
      border-color: var(--nx-primary);
    }
  }

  &__price {
    display: flex;
    align-items: baseline;
    gap: 10px;
    margin: 14px 0;
  }

  &__price-main {
    font-size: 26px;
    color: var(--nx-danger);
  }

  &__price-original {
    font-size: 14px;
    color: var(--nx-text-muted);
    text-decoration: line-through;
  }

  &__label {
    margin: 0 0 8px;
    font-size: 13px;
    font-weight: 600;
  }

  &__sku-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  &__sku {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    border: 1px solid var(--nx-border-strong);
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 13px;
    cursor: pointer;

    &--active {
      border-color: var(--nx-primary);
      color: var(--nx-primary);
      background: var(--nx-primary-soft);
    }

    &--disabled {
      opacity: 0.45;
      cursor: not-allowed;
      text-decoration: line-through;
    }
  }

  &__sku-spec::after {
    content: '·';
    margin-left: 6px;
    color: var(--nx-text-muted);
  }

  &__sku-spec:last-of-type::after {
    content: '';
  }

  &__quantity {
    margin: 18px 0;
  }

  &__quantity-control {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__quantity-input {
    width: 64px;
    height: 32px;
    padding: 0 8px;
    border: 1px solid var(--nx-border-strong);
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    color: var(--nx-text);
    text-align: center;
    font-family: inherit;
  }

  &__actions {
    display: flex;
    gap: 10px;
    margin-top: 20px;
  }

  &__note {
    margin-top: 14px;
    max-width: 52ch;
  }

  &__description {
    margin-top: 36px;
    padding-top: 20px;
    border-top: 1px solid var(--nx-border);

    p {
      max-width: 80ch;
      color: var(--nx-text-secondary);
      font-size: 14px;
      white-space: pre-wrap;
    }
  }
}

@media (max-width: 820px) {
  .product__layout {
    grid-template-columns: 1fr;
  }
}
</style>
