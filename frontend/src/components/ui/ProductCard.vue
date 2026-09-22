<script setup lang="ts">
/**
 * Reusable consumer product card (used by Home and Search).
 *
 * Prices are rendered through `formatMoney` so integer minor units never reach the
 * DOM as a float.
 */
import { RouterLink } from 'vue-router'
import { formatMoney } from '@/utils/money'
import type { ProductSummary } from '@/types/domain'

defineProps<{ product: ProductSummary }>()
</script>

<template>
  <RouterLink :to="{ name: 'product', params: { id: product.id } }" class="product-card">
    <div class="product-card__stage">
      <img
        v-if="product.cover_url"
        :src="product.cover_url"
        :alt="product.title"
        class="product-card__image"
        loading="lazy"
      />
      <span v-else class="product-card__placeholder" aria-hidden="true">无图</span>
    </div>

    <div class="product-card__body">
      <h3 class="product-card__title">{{ product.title }}</h3>
      <p v-if="product.brand_name" class="product-card__brand">{{ product.brand_name }}</p>

      <div class="product-card__price">
        <span class="nx-money">{{ formatMoney(product.min_price_amount) }}</span>
        <span v-if="product.original_price_amount" class="product-card__original">
          {{ formatMoney(product.original_price_amount) }}
        </span>
      </div>

      <p class="product-card__meta">
        <span v-if="product.rating">评分 {{ product.rating.toFixed(1) }}</span>
        <span v-if="product.sales_count !== undefined">已售 {{ product.sales_count }}</span>
      </p>
    </div>
  </RouterLink>
</template>

<style scoped lang="scss">
.product-card {
  display: block;
  background: var(--nx-surface);
  border: 1px solid var(--nx-border);
  border-radius: var(--nx-radius-card);
  box-shadow: var(--nx-shadow-card);
  color: inherit;
  text-decoration: none;
  overflow: hidden;
  transition: box-shadow 0.2s ease, transform 0.2s ease, background-color 0.2s ease;

  &:hover {
    box-shadow: var(--nx-shadow-card-hover);
    background: var(--nx-surface-hover);
    transform: translateY(-2px);
  }

  &__stage {
    display: flex;
    align-items: center;
    justify-content: center;
    height: 172px;
    margin: 12px 12px 0;
    background: var(--nx-surface-stage);
    border-radius: var(--nx-radius-stage);
    overflow: hidden;
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
    padding: 12px 16px 16px;
  }

  &__title {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    letter-spacing: -0.01em;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    min-height: 38px;
  }

  &__brand {
    margin: 4px 0 0;
    font-size: 12px;
    color: var(--nx-text-muted);
  }

  &__price {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-top: 8px;
    font-size: 16px;
    color: var(--nx-danger);
  }

  &__original {
    font-size: 12px;
    color: var(--nx-text-muted);
    text-decoration: line-through;
  }

  &__meta {
    display: flex;
    gap: 10px;
    margin: 6px 0 0;
    font-size: 12px;
    color: var(--nx-text-muted);
  }
}
</style>
