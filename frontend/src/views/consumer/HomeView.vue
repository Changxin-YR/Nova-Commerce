<script setup lang="ts">
/**
 * Consumer home: hero + category pills + recommended grid.
 *
 * State contract (§108): Loading / Success / Empty / Error / PermissionDenied are all
 * rendered through `<StateView>`; this page supplies only the data.
 */
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { catalogApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import StateView from '@/components/ui/StateView.vue'
import ProductCard from '@/components/ui/ProductCard.vue'

const activeCategory = ref<string>('')

// Destructured so the template can use them without `.value` (top-level refs are
// auto-unwrapped; properties of a returned object are not).
const {
  data: categoryData,
  status: categoryStatus,
} = useAsyncState(() => catalogApi.categories(), { immediate: true })

/** Recommended products; re-fetched when the category pill changes. */
const {
  data: productData,
  status: productStatus,
  error: productError,
  execute: loadProducts,
} = useAsyncState(
  () => catalogApi.searchProducts(activeCategory.value ? { category_id: activeCategory.value } : {}),
  { immediate: true },
)

const list = computed(() => productData.value ?? [])
const categoryList = computed(() => categoryData.value ?? [])

function selectCategory(id: string): void {
  activeCategory.value = activeCategory.value === id ? '' : id
  void loadProducts()
}
</script>

<template>
  <div class="nx-container home">
    <section class="home__hero">
      <p class="home__eyebrow">AI 原生的 3C 电商</p>
      <h1 class="home__title">选对设备，也让运营有据可依</h1>
      <p class="home__subtitle">
        商品、库存、订单、售后在同一条事务链路上；智能体的每一次写操作都需要人工审批。
      </p>
      <div class="home__hero-actions">
        <RouterLink :to="{ name: 'search' }" class="nx-btn nx-btn--primary">浏览全部商品</RouterLink>
        <RouterLink :to="{ name: 'ai-assistant' }" class="nx-btn">问问 AI 助手</RouterLink>
      </div>
    </section>

    <!-- Category pills: hidden while loading, so an empty bar never flashes. -->
    <section v-if="categoryStatus === 'success' && categoryList.length > 0" class="home__categories">
      <div class="nx-pills">
        <button
          type="button"
          class="nx-pill"
          :class="{ 'nx-pill--active': activeCategory === '' }"
          @click="selectCategory('')"
        >
          全部
        </button>
        <button
          v-for="category in categoryList"
          :key="category.id"
          type="button"
          class="nx-pill"
          :class="{ 'nx-pill--active': activeCategory === category.id }"
          @click="selectCategory(category.id)"
        >
          {{ category.name }}
        </button>
      </div>
    </section>

    <section class="home__products">
      <h2 class="nx-section-title">推荐商品</h2>

      <StateView :state="productStatus" :error="productError" @retry="loadProducts()">
        <div class="home__grid">
          <ProductCard v-for="product in list" :key="product.id" :product="product" />
        </div>
      </StateView>
    </section>
  </div>
</template>

<style scoped lang="scss">
.home {
  &__hero {
    padding: 36px 0 28px;
  }

  &__eyebrow {
    margin: 0 0 8px;
    font-size: 12px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--nx-primary);
  }

  &__title {
    margin: 0;
    font-size: 30px;
    font-weight: 600;
    letter-spacing: -0.019em;
  }

  &__subtitle {
    max-width: 60ch;
    margin: 10px 0 0;
    color: var(--nx-text-muted);
    font-size: 14px;
  }

  &__hero-actions {
    display: flex;
    gap: 8px;
    margin-top: 18px;
  }

  &__categories {
    padding-bottom: 20px;
    overflow-x: auto;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
  }
}
</style>
