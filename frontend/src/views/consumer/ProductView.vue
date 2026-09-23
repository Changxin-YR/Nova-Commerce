<script setup lang="ts">
/**
 * Product detail — the commerce two-column convention:
 *
 *   [ gallery + thumbnails ] [ price block, SKU chips, quantity, 加购/立即购买 ]
 *
 * The SKU selector is a chip grid: every spec combination is visible at once, out-of-stock
 * combinations are visibly disabled AND non-clickable, and the selected combination is
 * outlined in brand red. This is the affordance shoppers expect; a dropdown would hide
 * what is actually available.
 *
 * STOCK IS DISPLAY ONLY. `available_stock` comes from the server for UX; the binding check
 * happens inside the order transaction, which is why the error path for
 * `INSUFFICIENT_STOCK` (40000) is handled even when the button looked enabled.
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { catalogApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useCartStore } from '@/stores/cart'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import PriceText from '@/components/ui/PriceText.vue'

const route = useRoute()
const router = useRouter()
const cart = useCartStore()
const notifications = useNotificationStore()

const productId = computed(() => String(route.params.id ?? ''))

const { data: product, status, error, execute } = useAsyncState(
  () => catalogApi.product(productId.value),
  { immediate: true },
)

const selectedSkuId = ref('')
const quantity = ref(1)
const activeImage = ref(0)
const submitting = ref(false)

const skus = computed(() => product.value?.skus ?? [])
const selectedSku = computed(() => skus.value.find((sku) => sku.id === selectedSkuId.value) ?? null)
const images = computed(() => product.value?.images ?? [])
const currentImage = computed(() => images.value[activeImage.value] ?? images.value[0] ?? null)

/** Sellable quantity as reported by the server — not a client stock computation. */
const maxQuantity = computed(() => Math.max(1, selectedSku.value?.available_stock ?? 1))
const soldOut = computed(() => (selectedSku.value?.available_stock ?? 0) <= 0)
const currentPrice = computed(() => selectedSku.value?.price_amount ?? product.value?.min_price_amount ?? 0)
const listPrice = computed(
  () => selectedSku.value?.original_price_amount ?? product.value?.original_price_amount,
)

/** Default to the first in-stock combination once the product loads. */
function ensureSkuSelected(): void {
  if (selectedSkuId.value) return
  const first = skus.value.find((sku) => (sku.available_stock ?? 0) > 0) ?? skus.value[0]
  if (first) selectedSkuId.value = first.id
}
watch(product, ensureSkuSelected, { immediate: true })

/** Flatten a SKU's specs into the chip label: "深空黑 / 256GB". */
function skuLabel(sku: { specs: Record<string, string> }): string {
  const values = Object.values(sku.specs)
  return values.length > 0 ? values.join(' / ') : '默认规格'
}

const specGroups = computed(() => {
  const groups = new Map<string, string[]>()
  for (const sku of skus.value) {
    for (const [key, value] of Object.entries(sku.specs)) {
      const list = groups.get(key) ?? []
      if (!list.includes(value)) list.push(value)
      groups.set(key, list)
    }
  }
  return [...groups.entries()]
})

const SERVICE_NOTES = [
  '正品保障：官方授权渠道发货',
  '极速发货：仓库实时库存校验',
  '无忧退换：7 天无理由（以售后政策为准）',
]

async function addToCart(): Promise<boolean> {
  if (!selectedSku.value || soldOut.value) return false
  submitting.value = true
  try {
    await cart.addItem(productId.value, selectedSku.value.id, quantity.value, {
      product_title: product.value?.title,
      sku_name: skuLabel(selectedSku.value),
      cover_url: currentImage.value?.url,
    })
    notifications.success('已加入购物车', `${product.value?.title ?? ''} × ${quantity.value}`)
    return true
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('加入购物车失败', normalized.message, normalized.code, normalized.traceId)
    return false
  } finally {
    submitting.value = false
  }
}

async function buyNow(): Promise<void> {
  // Buy-now still goes through the server-side preview: the client never invents a price.
  if (await addToCart()) await router.push({ name: 'cart' })
}
</script>

<template>
  <div class="nx-container pdetail">
    <StateView :state="status" :error="error" @retry="execute()">
      <div v-if="product" class="pdetail__main">
        <!-- gallery ------------------------------------------------------ -->
        <div class="pdetail__gallery">
          <div class="pdetail__stage">
            <img
              v-if="currentImage"
              :src="currentImage.url"
              :alt="currentImage.alt ?? product.title"
              class="pdetail__image"
            />
            <span v-else class="nx-muted">暂无图片</span>
          </div>

          <div v-if="images.length > 1" class="pdetail__thumbs">
            <button
              v-for="(image, index) in images"
              :key="image.id"
              type="button"
              class="pdetail__thumb"
              :class="{ 'pdetail__thumb--active': index === activeImage }"
              @click="activeImage = index"
            >
              <img :src="image.url" :alt="`图片 ${index + 1}`" />
            </button>
          </div>
        </div>

        <!-- buy box ------------------------------------------------------ -->
        <div class="pdetail__buy">
          <h1 class="pdetail__title">{{ product.title }}</h1>
          <p v-if="product.subtitle" class="pdetail__subtitle">{{ product.subtitle }}</p>

          <!-- price block on the tinted panel -->
          <div class="pdetail__pricebox">
            <PriceText :amount="currentPrice" :original-amount="listPrice" size="xl" />
            <p class="pdetail__pricehint">
              <span v-if="listPrice" class="pdetail__save">
                立省
                <PriceText :amount="Math.max(0, listPrice - currentPrice)" size="sm" />
              </span>
              <span class="nx-muted">价格为整数分展示；下单时价格快照锁定</span>
            </p>
          </div>

          <!-- SKU chips -------------------------------------------------- -->
          <dl v-if="specGroups.length" class="pdetail__specs">
            <div v-for="[key, values] in specGroups" :key="key">
              <dt>{{ key }}</dt>
              <dd>
                <span v-for="value in values" :key="value" class="pdetail__specvalue">{{ value }}</span>
              </dd>
            </div>
          </dl>

          <div v-if="skus.length" class="pdetail__skus">
            <p class="pdetail__label">选择规格</p>
            <div class="pdetail__skugrid">
              <button
                v-for="sku in skus"
                :key="sku.id"
                type="button"
                class="pdetail__sku"
                :class="{
                  'pdetail__sku--active': sku.id === selectedSkuId,
                  'pdetail__sku--disabled': (sku.available_stock ?? 0) <= 0,
                }"
                :disabled="(sku.available_stock ?? 0) <= 0"
                :title="(sku.available_stock ?? 0) <= 0 ? '该规格缺货' : undefined"
                @click="selectedSkuId = sku.id"
              >
                <span class="pdetail__skulabel">{{ skuLabel(sku) }}</span>
                <PriceText :amount="sku.price_amount" size="sm" />
                <span v-if="(sku.available_stock ?? 0) <= 0" class="pdetail__skuoos">缺货</span>
              </button>
            </div>
          </div>

          <!-- quantity --------------------------------------------------- -->
          <div class="pdetail__qty">
            <p class="pdetail__label">数量</p>
            <div class="pdetail__qtyrow">
              <div class="qty">
                <button type="button" class="qty__btn" :disabled="quantity <= 1" @click="quantity -= 1">−</button>
                <input v-model.number="quantity" type="number" min="1" :max="maxQuantity" class="qty__input" />
                <button
                  type="button"
                  class="qty__btn"
                  :disabled="quantity >= maxQuantity"
                  @click="quantity += 1"
                >
                  +
                </button>
              </div>
              <span class="nx-muted">
                {{ soldOut ? '该规格暂时缺货' : `可售 ${selectedSku?.available_stock ?? 0} 件` }}
              </span>
            </div>
          </div>

          <!-- dual CTA: orange outline + solid red ----------------------- -->
          <div class="pdetail__actions">
            <button
              type="button"
              class="nx-btn nx-btn--outline nx-btn--lg"
              :disabled="submitting || soldOut"
              @click="addToCart()"
            >
              加入购物车
            </button>
            <button
              type="button"
              class="nx-btn nx-btn--primary nx-btn--lg"
              :disabled="submitting || soldOut"
              @click="buyNow()"
            >
              立即购买
            </button>
          </div>

          <ul class="pdetail__services">
            <li v-for="note in SERVICE_NOTES" :key="note">{{ note }}</li>
          </ul>

          <p class="nx-muted pdetail__disclaimer">
            库存以提交订单时服务器校验为准；下单价格快照保存，后续改价不影响已有订单。
          </p>
        </div>
      </div>

      <!-- description block ------------------------------------------------- -->
      <section v-if="product?.description" class="nx-block pdetail__desc">
        <div class="nx-block__head">
          <h2 class="nx-block__title">商品详情</h2>
        </div>
        <div class="nx-block__body">
          <p class="pdetail__desctext">{{ product.description }}</p>
        </div>
      </section>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.pdetail {
  &__main {
    display: grid;
    grid-template-columns: 400px 1fr;
    gap: 20px;
    padding: 20px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
  }

  /* -- gallery ----------------------------------------------------------- */
  &__stage {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 400px;
    background: var(--nx-surface-stage);
    border: 1px solid var(--nx-border);
  }

  &__image {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
  }

  &__thumbs {
    display: flex;
    gap: 6px;
    margin-top: 8px;
  }

  &__thumb {
    width: 56px;
    height: 56px;
    padding: 2px;
    border: 1px solid var(--nx-border);
    background: var(--nx-surface);
    cursor: pointer;

    img {
      width: 100%;
      height: 100%;
      object-fit: contain;
    }

    &--active {
      border-color: var(--nx-brand);
    }
  }

  /* -- buy box ----------------------------------------------------------- */
  &__title {
    font-size: 18px;
    font-weight: 700;
    line-height: 1.5;
  }

  &__subtitle {
    margin: 6px 0 0;
    font-size: 12px;
    color: var(--nx-brand);
  }

  &__pricebox {
    margin: 12px 0;
    padding: 10px 12px;
    background: var(--nx-surface-sunken);
    border: 1px solid var(--nx-border);
  }

  &__pricehint {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 6px 0 0;
    font-size: 12px;
  }

  &__save {
    display: inline-flex;
    align-items: baseline;
    gap: 4px;
    color: var(--nx-brand);
  }

  &__specs {
    margin: 12px 0;
    font-size: 12px;

    > div {
      display: flex;
      gap: 10px;
      padding: 4px 0;
    }

    dt {
      flex: 0 0 60px;
      color: var(--nx-text-muted);
    }

    dd {
      display: flex;
      gap: 12px;
      margin: 0;
    }
  }

  &__specvalue {
    color: var(--nx-text-secondary);
  }

  &__label {
    margin: 0 0 6px;
    font-size: 12px;
    color: var(--nx-text-muted);
  }

  &__skugrid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: 8px;
  }

  &__sku {
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    padding: 8px 10px;
    border: 1px solid var(--nx-border-strong);
    background: var(--nx-surface);
    font-family: inherit;
    text-align: left;
    cursor: pointer;

    &:hover:not(:disabled) {
      border-color: var(--nx-brand);
    }

    &--active {
      border-color: var(--nx-brand);
      box-shadow: inset 0 0 0 1px var(--nx-brand);
    }

    /* Disabled is visually obvious AND not clickable. */
    &--disabled {
      border-style: dashed;
      background: var(--nx-surface-sunken);
      cursor: not-allowed;
      opacity: 0.7;
    }
  }

  &__skulabel {
    font-size: 12px;
    color: var(--nx-text);
  }

  &__skuoos {
    position: absolute;
    top: 4px;
    right: 4px;
    padding: 0 3px;
    background: var(--nx-text-muted);
    color: #fff;
    font-size: 12px;
    line-height: 14px;
  }

  &__qty {
    margin: 14px 0;
  }

  &__qtyrow {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  &__actions {
    display: flex;
    gap: 10px;
    margin-top: 16px;
  }

  &__services {
    margin: 16px 0 0;
    padding: 10px 0 0;
    border-top: 1px dashed var(--nx-border);
    list-style: none;

    li {
      padding: 2px 0;
      color: var(--nx-text-secondary);
      font-size: 12px;

      &::before {
        content: '·';
        margin-right: 6px;
        color: var(--nx-brand);
        font-weight: 700;
      }
    }
  }

  &__disclaimer {
    margin: 12px 0 0;
    font-size: 12px;
    line-height: 1.6;
  }

  &__desc {
    margin-top: 14px;
  }

  &__desctext {
    margin: 0;
    max-width: 90ch;
    color: var(--nx-text-secondary);
    font-size: 13px;
    line-height: 1.8;
    white-space: pre-wrap;
  }
}

/* -- quantity stepper: square, hairline-joined ----------------------------- */
.qty {
  display: flex;
  border: 1px solid var(--nx-border-strong);

  &__btn {
    width: 28px;
    height: 30px;
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

  &__input {
    width: 48px;
    height: 30px;
    border: none;
    border-left: 1px solid var(--nx-border-strong);
    border-right: 1px solid var(--nx-border-strong);
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 13px;
    text-align: center;

    &:focus {
      outline: none;
    }
  }
}

@media (max-width: 900px) {
  .pdetail__main {
    grid-template-columns: 1fr;
  }

  .pdetail__stage {
    height: 300px;
  }
}
</style>
