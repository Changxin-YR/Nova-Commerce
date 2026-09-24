<script setup lang="ts">
/**
 * Consumer home — the dense-commerce layout, top to bottom:
 *
 *   [ left category menu (sticky, 200px) ][ carousel banner ][ user/service panel ]
 *   [ service promise icons ]
 *   [ product floor 1: title + tabs + dense grid ]
 *   [ product floor 2 ]
 *
 * The layout is the reference convention: a persistent category rail, a wide banner that
 * owns the visual centre, and floors that surface as much product as possible above the
 * fold. Deliberately NOT a wide-whitespace hero page.
 *
 * Banners are pure CSS gradient compositions with OUR copy. No third-party artwork,
 * logo or trademark is used anywhere (spec §127).
 *
 * Product data comes from the published catalog API and empty/error states are
 * rendered directly when no products are available.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { catalogApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useAuthStore } from '@/stores/auth'
import StateView from '@/components/ui/StateView.vue'
import ProductCard from '@/components/ui/ProductCard.vue'
import PriceText from '@/components/ui/PriceText.vue'

const auth = useAuthStore()

/* -- category rail (static UI structure, not cross-page state: §105) -------- */
const CATEGORY_MENU = [
  { name: '手机通讯', items: ['手机', '老人机', '对讲机', '手机配件'] },
  { name: '电脑办公', items: ['笔记本', '台式机', '显示器', '键鼠外设'] },
  { name: '影音娱乐', items: ['耳机', '音箱', '智能投影'] },
  { name: '智能穿戴', items: ['智能手表', '智能手环', 'VR 眼镜'] },
  { name: '智能家居', items: ['智能门锁', '扫地机器人', '智能照明'] },
  { name: '数码配件', items: ['充电器', '移动电源', '数据线', '存储卡'] },
  { name: '游戏设备', items: ['游戏主机', '游戏手柄', '电竞显示器'] },
  { name: '摄影摄像', items: ['微单相机', '运动相机', '无人机'] },
]
const activeCategory = ref<number | null>(null)

/* -- CSS-only banner carousel ---------------------------------------------- */
const BANNERS = [
  { title: '开学季 数码焕新', subtitle: '笔记本 / 平板 / 耳机 低至 5 折', tone: 'a', to: 'search' },
  { title: '智能穿戴专场', subtitle: '手表手环 满 1000 减 200', tone: 'b', to: 'search' },
  { title: 'AI 助手帮你挑', subtitle: '说出需求，直接给到可选型号', tone: 'c', to: 'ai-assistant' },
] as const
const bannerIndex = ref(0)
let bannerTimer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  bannerTimer = setInterval(() => {
    bannerIndex.value = (bannerIndex.value + 1) % BANNERS.length
  }, 5000)
})

onBeforeUnmount(() => {
  if (bannerTimer) clearInterval(bannerTimer)
})

/* -- floors ---------------------------------------------------------------- */
const FLOOR_TABS = [
  { key: 'hot', label: '热销榜' },
  { key: 'new', label: '新品' },
  { key: 'price', label: '价格' },
]

const {
  data: productData,
  status: productStatus,
  error: productError,
  execute: loadProducts,
  refresh: refreshProducts,
} = useAsyncState(() => catalogApi.searchProducts({ page_size: 20 }), { immediate: true })

const products = computed(() => productData.value?.items ?? [])
const activeTab = ref('hot')

const SERVICE_ICONS = [
  { label: '正品保障', hint: '官方授权' },
  { label: '极速发货', hint: '当日出库' },
  { label: '无忧退换', hint: '7 天无理由' },
  { label: '专业客服', hint: '9:00-24:00' },
]
</script>

<template>
  <div class="home">
    <!-- band 1: category rail + banner + user panel --------------------- -->
    <section class="nx-container home__top">
      <nav class="homemenu" aria-label="商品分类">
        <button
          v-for="(group, index) in CATEGORY_MENU"
          :key="group.name"
          type="button"
          class="homemenu__item"
          :class="{ 'homemenu__item--active': activeCategory === index }"
          @mouseenter="activeCategory = index"
          @mouseleave="activeCategory = null"
        >
          {{ group.name }}
          <span class="homemenu__arrow" aria-hidden="true">›</span>

          <div v-if="activeCategory === index" class="homemenu__sub">
            <RouterLink
              v-for="item in group.items"
              :key="item"
              :to="{ name: 'search', query: { q: item } }"
              class="homemenu__subitem"
            >
              {{ item }}
            </RouterLink>
          </div>
        </button>
      </nav>

      <div class="home__banner-col">
        <div class="banner" :class="`banner--${BANNERS[bannerIndex]?.tone}`">
          <RouterLink :to="{ name: BANNERS[bannerIndex]?.to ?? 'search' }" class="banner__link">
            <h2 class="banner__title">{{ BANNERS[bannerIndex]?.title }}</h2>
            <p class="banner__subtitle">{{ BANNERS[bannerIndex]?.subtitle }}</p>
            <span class="banner__cta">立即查看</span>
          </RouterLink>
        </div>
        <div class="banner__dots">
          <button
            v-for="(banner, index) in BANNERS"
            :key="banner.title"
            type="button"
            class="banner__dot"
            :class="{ 'banner__dot--active': index === bannerIndex }"
            :aria-label="`切换到第 ${index + 1} 张`"
            @click="bannerIndex = index"
          />
        </div>
      </div>

      <aside class="home__side">
        <div class="home__userpanel">
          <div class="home__user-avatar" aria-hidden="true">
            {{ auth.isLoggedIn ? auth.displayName.slice(0, 1) : '游' }}
          </div>
          <p class="home__user-name">
            {{ auth.isLoggedIn ? auth.displayName : '欢迎来到 Nova' }}
          </p>
          <p class="nx-muted home__user-hint">
            {{ auth.isLoggedIn ? '查看我的订单与售后进度' : '登录后可结算商品并查看订单' }}
          </p>
          <div class="home__user-actions">
            <RouterLink v-if="!auth.isLoggedIn" :to="{ name: 'login' }" class="nx-btn nx-btn--primary nx-btn--block">
              登录 / 注册
            </RouterLink>
            <RouterLink v-else :to="{ name: 'orders' }" class="nx-btn nx-btn--primary nx-btn--block">
              我的订单
            </RouterLink>
          </div>
        </div>

        <ul class="home__services">
          <li v-for="service in SERVICE_ICONS" :key="service.label">
            <strong>{{ service.label }}</strong>
            <span>{{ service.hint }}</span>
          </li>
        </ul>
      </aside>
    </section>

    <!-- band 2: floors --------------------------------------------------- -->
    <section class="nx-container home__floor">
      <header class="home__floor-head">
        <h2 class="nx-floor-title">为你推荐</h2>
        <nav class="home__floor-tabs">
          <button
            v-for="tab in FLOOR_TABS"
            :key="tab.key"
            type="button"
            class="nx-tab"
            :class="{ 'nx-tab--active': activeTab === tab.key }"
            @click="activeTab = tab.key"
          >
            {{ tab.label }}
          </button>
        </nav>
        <button type="button" class="nx-btn nx-btn--sm home__refresh" @click="refreshProducts()">
          刷新
        </button>
      </header>

      <StateView :state="productStatus" :error="productError" @retry="loadProducts()">
        <div class="home__grid">
          <ProductCard v-for="product in products" :key="product.id" :product="product" />
        </div>
      </StateView>
    </section>

    <!-- band 3: a second floor with a different shape (ranked list) ------ -->
    <section class="nx-container home__floor">
      <header class="home__floor-head">
        <h2 class="nx-floor-title">热销榜</h2>
        <RouterLink :to="{ name: 'search' }" class="home__more">查看更多 ›</RouterLink>
      </header>

      <ol class="ranklist">
        <li v-for="(product, index) in products.slice(0, 5)" :key="`rank-${product.id}`">
          <span class="ranklist__no" :class="{ 'ranklist__no--top': index < 3 }">{{ index + 1 }}</span>
          <RouterLink :to="{ name: 'product', params: { id: product.id } }" class="ranklist__title">
            {{ product.title }}
          </RouterLink>
          <PriceText :amount="product.min_price_amount" size="sm" class="ranklist__price" />
          <span class="nx-muted ranklist__sales">
            {{ product.sales_count ? `已售 ${product.sales_count}` : '' }}
          </span>
        </li>
      </ol>
    </section>
  </div>
</template>

<style scoped lang="scss">
.home {
  &__top {
    display: grid;
    grid-template-columns: 200px 1fr 200px;
    gap: 0;
    margin-bottom: 10px;
  }

  &__banner-col {
    position: relative;
    min-width: 0;
  }

  &__side {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding-left: 10px;
  }

  &__userpanel {
    padding: 10px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    text-align: center;
  }

  &__user-avatar {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 44px;
    margin: 0 auto 6px;
    border: 2px solid var(--nx-brand);
    border-radius: 50%;
    color: var(--nx-brand);
    font-size: 18px;
    font-weight: 700;
  }

  &__user-name {
    margin: 0;
    font-size: 13px;
    font-weight: 700;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__user-hint {
    margin: 4px 0 8px;
    font-size: 12px;
    line-height: 1.5;
  }

  &__services {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 1px;
    margin: 0;
    padding: 0;
    background: var(--nx-border);
    border: 1px solid var(--nx-border);
    list-style: none;

    li {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding: 8px 6px;
      background: var(--nx-surface);
      text-align: center;
    }

    strong {
      font-size: 12px;
      font-weight: 400;
      color: var(--nx-text);
    }

    span {
      font-size: 12px;
      color: var(--nx-text-muted);
    }
  }

  &__floor {
    margin-bottom: 20px;
  }

  &__floor-head {
    display: flex;
    align-items: center;
    gap: 20px;
    margin-bottom: 10px;
    padding-bottom: 6px;
    border-bottom: 2px solid var(--nx-brand);
  }

  &__floor-tabs {
    display: flex;
    align-items: center;
    border-bottom: none;
  }

  &__refresh {
    margin-left: auto;
  }

  &__more {
    margin-left: auto;
    font-size: 12px;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 10px;
  }
}

/* -- category rail --------------------------------------------------------- */
.homemenu {
  display: flex;
  flex-direction: column;
  padding: 6px 0;
  background: var(--nx-surface);
  border: 1px solid var(--nx-border);
  border-right: none;

  &__item {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 6px 12px;
    border: none;
    background: transparent;
    color: var(--nx-text);
    font-family: inherit;
    font-size: 13px;
    text-align: left;
    cursor: pointer;

    &--active,
    &:hover {
      background: var(--nx-brand-soft);
      color: var(--nx-brand);
    }
  }

  &__arrow {
    color: var(--nx-text-muted);
  }

  &__sub {
    position: absolute;
    top: 0;
    left: 100%;
    z-index: 40;
    display: grid;
    grid-template-columns: repeat(3, minmax(80px, 1fr));
    gap: 4px 12px;
    width: 420px;
    padding: 12px 16px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    box-shadow: var(--nx-shadow-pop);
  }

  &__subitem {
    font-size: 12px;
    color: var(--nx-text-secondary);

    &:hover {
      color: var(--nx-brand);
    }
  }
}

/* -- banner (pure CSS artwork, our own copy) ------------------------------- */
.banner {
  display: flex;
  align-items: center;
  height: 300px;
  padding: 0 48px;
  overflow: hidden;

  &--a {
    background: linear-gradient(120deg, #e1251b 0%, #ff6a3d 55%, #ffb199 100%);
  }

  &--b {
    background: linear-gradient(120deg, #0f2b46 0%, #1d7de0 60%, #7fd0ff 100%);
  }

  &--c {
    background: linear-gradient(120deg, #2b1055 0%, #6d3ac9 55%, #b39ddb 100%);
  }

  &__link {
    display: block;
    color: #fff;

    &:hover {
      color: #fff;
    }
  }

  &__title {
    margin: 0;
    font-size: 34px;
    font-weight: 700;
    letter-spacing: 0.02em;
    text-shadow: 0 2px 6px rgba(0, 0, 0, 0.18);
  }

  &__subtitle {
    margin: 10px 0 18px;
    font-size: 16px;
    opacity: 0.92;
  }

  &__cta {
    display: inline-flex;
    align-items: center;
    height: 34px;
    padding: 0 20px;
    background: rgba(255, 255, 255, 0.92);
    color: #333;
    font-size: 14px;
    font-weight: 700;
  }

  &__dots {
    position: absolute;
    bottom: 10px;
    left: 48px;
    display: flex;
    gap: 6px;
  }

  &__dot {
    width: 20px;
    height: 3px;
    padding: 0;
    border: none;
    background: rgba(255, 255, 255, 0.5);
    cursor: pointer;

    &--active {
      background: #fff;
    }
  }
}

/* -- ranked list floor ---------------------------------------------------- */
.ranklist {
  margin: 0;
  padding: 0;
  background: var(--nx-surface);
  border: 1px solid var(--nx-border);
  list-style: none;

  li {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--nx-border);

    &:last-child {
      border-bottom: none;
    }
  }

  &__no {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 18px;
    height: 18px;
    background: var(--nx-surface-sunken);
    color: var(--nx-text-secondary);
    font-size: 12px;
    font-weight: 700;

    &--top {
      background: var(--nx-brand);
      color: #fff;
    }
  }

  &__title {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    color: var(--nx-text);
    font-size: 12px;
    text-overflow: ellipsis;
    white-space: nowrap;

    &:hover {
      color: var(--nx-brand);
    }
  }

  &__price {
    flex: 0 0 auto;
  }

  &__sales {
    flex: 0 0 90px;
    text-align: right;
  }
}

@media (max-width: 1200px) {
  .home__top {
    grid-template-columns: 180px 1fr;
  }

  .home__side {
    display: none;
  }

  .home__grid {
    grid-template-columns: repeat(4, 1fr);
  }
}

@media (max-width: 900px) {
  .home__grid {
    grid-template-columns: repeat(3, 1fr);
  }

  .homemenu {
    display: none;
  }

  .home__top {
    grid-template-columns: 1fr;
  }
}
</style>
