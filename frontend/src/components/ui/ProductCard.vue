<script setup lang="ts">
/**
 * Product card — the most repeated element in the store, so it carries the design
 * language: hairline border, ~0 radius, two-line clamped title, dominant red price,
 * promo badge row, and a hover state that RAISES the border and adds a soft shadow
 * (rather than the rounded floating card of a Western SaaS layout).
 *
 * Prices render through `<PriceText>` only. This component never formats money.
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import PriceText from '@/components/ui/PriceText.vue'
import type { ProductSummary } from '@/types/domain'

const props = withDefaults(
  defineProps<{
    product: ProductSummary
    /** Hide the promo badge row in very dense grids. */
    showTags?: boolean
  }>(),
  { showTags: true },
)

/**
 * Promo badges come from the product's own tags. They are OUR labels — no third-party
 * mark or promotional wording is reproduced (spec §127).
 */
const PROMO_STYLES: Record<string, string> = {
  自营: 'nx-badge--self',
  秒杀: 'nx-badge--seckill',
  满减: 'nx-badge--discount',
  领券: 'nx-badge--coupon',
  新品: 'nx-badge--new',
}

const badges = computed(() => (props.product.tags ?? []).slice(0, 3))
function badgeClass(tag: string): string {
  return PROMO_STYLES[tag] ?? 'nx-badge--neutral'
}

/** "已售 1.2万" reads better than a raw 5-digit number in a dense grid. */
const salesText = computed(() => {
  const count = props.product.sales_count
  if (count === undefined || count === null) return ''
  if (count >= 10000) return `已售 ${(count / 10000).toFixed(1)}万`
  return `已售 ${count}`
})
</script>

<template>
  <RouterLink :to="{ name: 'product', params: { id: product.id } }" class="pcard">
    <div class="pcard__stage">
      <img
        v-if="product.cover_url"
        :src="product.cover_url"
        :alt="product.title"
        class="pcard__image"
        loading="lazy"
      />
      <span v-else class="pcard__placeholder" aria-hidden="true">暂无图片</span>
    </div>

    <div class="pcard__body">
      <PriceText
        :amount="product.min_price_amount"
        :original-amount="product.original_price_amount"
        size="md"
        class="pcard__price"
      />

      <h3 class="pcard__title" :title="product.title">{{ product.title }}</h3>

      <p v-if="showTags && badges.length" class="pcard__badges">
        <span v-for="tag in badges" :key="tag" class="nx-badge" :class="badgeClass(tag)">{{ tag }}</span>
      </p>

      <p class="pcard__meta">
        <span v-if="salesText">{{ salesText }}</span>
        <span v-if="product.rating">好评 {{ product.rating.toFixed(1) }}</span>
      </p>
    </div>
  </RouterLink>
</template>

<style scoped lang="scss">
.pcard {
  display: block;
  padding: 10px;
  background: var(--nx-surface);
  border: 1px solid var(--nx-border);
  border-radius: var(--nx-radius);
  color: inherit;
  transition: border-color 0.15s, box-shadow 0.15s;

  &:hover {
    /* Hover RAISES the border and lifts slightly — the standard commerce affordance. */
    border-color: var(--nx-border-hover);
    box-shadow: var(--nx-shadow-card-hover);
    color: inherit;

    .pcard__title {
      color: var(--nx-brand);
    }
  }

  &__stage {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 160px;
    overflow: hidden;
    background: var(--nx-surface-stage);
  }

  &__image {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }

  &__placeholder {
    color: var(--nx-text-muted);
    font-size: 12px;
  }

  &__body {
    padding-top: 8px;
  }

  &__price {
    display: block;
    margin-bottom: 4px;
  }

  &__title {
    /* Two-line clamp: the dense-grid title height must stay predictable. */
    display: -webkit-box;
    height: 36px;
    margin: 0;
    overflow: hidden;
    font-size: 12px;
    font-weight: 400;
    line-height: 18px;
    color: var(--nx-text);
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    transition: color 0.15s;
  }

  &__badges {
    display: flex;
    gap: 4px;
    margin: 6px 0 0;
    overflow: hidden;
  }

  &__meta {
    display: flex;
    align-items: center;
    gap: 10px;
    min-height: 16px;
    margin: 6px 0 0;
    font-size: 12px;
    color: var(--nx-text-muted);
  }
}
</style>
