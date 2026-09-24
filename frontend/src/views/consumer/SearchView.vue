<script setup lang="ts">
/**
 * Product search. The keyword lives in the URL query so a search result is
 * shareable and the browser back button behaves (page state stays in the page, per
 * §105 — it is NOT pushed into a store).
 */
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { catalogApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import StateView from '@/components/ui/StateView.vue'
import ProductCard from '@/components/ui/ProductCard.vue'

const route = useRoute()
const router = useRouter()

const keyword = ref<string>(typeof route.query.q === 'string' ? route.query.q : '')
const sort = ref<'default' | 'price_asc' | 'price_desc' | 'sales'>('default')

const {
  data: productData,
  status,
  error,
  execute,
} = useAsyncState(
  () =>
    catalogApi.searchProducts({
      keyword: keyword.value || undefined,
      sort: sort.value === 'default' ? undefined : sort.value,
    }),
  { immediate: true },
)

const results = computed(() => productData.value?.items ?? [])

watch(
  () => route.query.q,
  (next) => {
    const value = typeof next === 'string' ? next : ''
    if (value === keyword.value) return
    keyword.value = value
    void execute()
  },
)

function submit(): void {
  void router.replace({ name: 'search', query: keyword.value ? { q: keyword.value } : {} })
  void execute()
}

function changeSort(value: typeof sort.value): void {
  sort.value = value
  void execute()
}
</script>

<template>
  <div class="nx-container search">
    <form class="search__bar" @submit.prevent="submit">
      <input
        v-model="keyword"
        type="search"
        class="search__input"
        placeholder="搜索手机、笔记本、耳机…"
        aria-label="搜索商品"
      />
      <button type="submit" class="nx-btn nx-btn--primary">搜索</button>
    </form>

    <div class="search__toolbar">
      <span class="nx-muted">共 {{ results.length }} 件商品</span>
      <div class="nx-pills">
        <button
          v-for="option in [
            { value: 'default', label: '综合' },
            { value: 'sales', label: '销量' },
            { value: 'price_asc', label: '价格升序' },
            { value: 'price_desc', label: '价格降序' },
          ]"
          :key="option.value"
          type="button"
          class="nx-pill"
          :class="{ 'nx-pill--active': sort === option.value }"
          @click="changeSort(option.value as typeof sort)"
        >
          {{ option.label }}
        </button>
      </div>
    </div>

    <StateView
      :state="status"
      :error="error"
      :title="status === 'empty' ? '没有找到匹配的商品' : undefined"
      :description="status === 'empty' ? '换一个关键词，或清空筛选条件再试。' : undefined"
      @retry="execute()"
    >
      <div class="search__grid">
        <ProductCard v-for="product in results" :key="product.id" :product="product" />
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.search {
  &__bar {
    display: flex;
    gap: 8px;
    max-width: 640px;
    margin-bottom: 16px;
  }

  &__input {
    flex: 1;
    height: 38px;
    padding: 0 14px;
    border: 1px solid var(--nx-border-strong);
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 14px;

    &:focus {
      border-color: var(--nx-primary);
      outline: none;
    }
  }

  &__toolbar {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
  }
}
</style>
