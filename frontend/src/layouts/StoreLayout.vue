<script setup lang="ts">
/**
 * The four-band consumer shell:
 *
 *   1. utility bar  — city, login/register, my orders, service, site nav  (30px, 12px type)
 *   2. header       — logo + CENTERED wide search + cart button           (80px)
 *   3. category row — the big left menu sits under the header, next to the banner
 *   4. content
 *
 * This banding is the information-dense commerce convention: the search box owns the
 * horizontal centre, and the utility bar carries account/service links that a Western
 * header would hide in a dropdown.
 *
 * The category menu data is static UI structure (not cross-page state), so it lives here
 * rather than in a store (§105).
 */
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import { useCartStore } from '@/stores/cart'
import { useNotificationStore } from '@/stores/notification'
import { APP_TITLE } from '@/config/app'

const app = useAppStore()
const auth = useAuthStore()
const cart = useCartStore()
const notifications = useNotificationStore()
const route = useRoute()
const router = useRouter()

const keyword = ref('')
const categoryOpen = ref(false)

/**
 * Top-level categories with their sub-entries. Until the catalog module serves real
 * category trees, this is the local fallback so the shell is structurally complete.
 */
const CATEGORY_MENU = [
  { name: '手机通讯', items: ['手机', '老人机', '对讲机', '手机配件', '以旧换新'] },
  { name: '电脑办公', items: ['笔记本', '台式机', '显示器', '键鼠', '外设', '办公设备'] },
  { name: '影音娱乐', items: ['耳机', '音箱', '麦克风', '智能投影', '影音配件'] },
  { name: '智能穿戴', items: ['智能手表', '智能手环', 'VR 眼镜', '运动追踪'] },
  { name: '智能家居', items: ['智能门锁', '扫地机器人', '智能照明', '环境电器'] },
  { name: '数码配件', items: ['充电器', '移动电源', '数据线', '存储卡', '支架'] },
  { name: '游戏设备', items: ['游戏主机', '游戏手柄', '电竞显示器', '游戏耳机'] },
  { name: '摄影摄像', items: ['微单相机', '运动相机', '无人机', '镜头', '三脚架'] },
]

const HOT_WORDS = ['蓝牙耳机', '轻薄本', '智能手表', '快充充电器', '扫地机器人']

const isHome = computed(() => route.name === 'home')

onMounted(() => {
  // The cart badge is genuinely cross-page state, so the shell refreshes it. A failure
  // here must not break navigation.
  if (auth.isLoggedIn) void cart.load().catch(() => undefined)
})

function search(): void {
  void router.push({ name: 'search', query: keyword.value ? { q: keyword.value } : {} })
}

function searchHotWord(word: string): void {
  keyword.value = word
  search()
}

function goCategory(name: string): void {
  categoryOpen.value = false
  void router.push({ name: 'search', query: { q: name } })
}

function goToConsole(): void {
  void router.push({ name: 'console-dashboard' })
}
</script>

<template>
  <div class="store-shell">
    <!-- 1. utility bar ---------------------------------------------------- -->
    <div class="utilbar">
      <div class="nx-container utilbar__inner">
        <div class="utilbar__left">
          <span class="utilbar__city">配送至：北京</span>
          <template v-if="auth.isLoggedIn">
            <span class="utilbar__welcome">你好，{{ auth.displayName }}</span>
          </template>
          <template v-else>
            <RouterLink :to="{ name: 'login' }" class="utilbar__link">你好，请登录</RouterLink>
            <RouterLink :to="{ name: 'login' }" class="utilbar__link utilbar__link--accent">免费注册</RouterLink>
          </template>
        </div>

        <div class="utilbar__right">
          <RouterLink v-if="auth.isLoggedIn" :to="{ name: 'orders' }" class="utilbar__link">
            我的订单
          </RouterLink>
          <RouterLink v-if="auth.isLoggedIn" :to="{ name: 'after-sales' }" class="utilbar__link">
            售后服务
          </RouterLink>
          <RouterLink v-if="auth.isLoggedIn" :to="{ name: 'addresses' }" class="utilbar__link">
            收货地址
          </RouterLink>
          <button
            v-if="auth.isLoggedIn && notifications.unreadCount > 0"
            type="button"
            class="utilbar__link utilbar__button"
            @click="notifications.markAllRead()"
          >
            消息 {{ notifications.unreadCount }}
          </button>
          <button type="button" class="utilbar__link utilbar__button" @click="app.toggleTheme()">
            {{ app.isDark ? '浅色模式' : '深色模式' }}
          </button>
          <button v-if="auth.isLoggedIn" type="button" class="utilbar__link utilbar__button" @click="goToConsole()">
            商家后台
          </button>
          <button v-if="auth.isLoggedIn" type="button" class="utilbar__link utilbar__button" @click="auth.logout()">
            退出登录
          </button>
        </div>
      </div>
    </div>

    <!-- 2. header: logo + centred search + cart --------------------------- -->
    <header class="sheader">
      <div class="nx-container sheader__inner">
        <RouterLink :to="{ name: 'home' }" class="sheader__brand">
          <span class="sheader__brand-mark">N</span>
          <span class="sheader__brand-name">{{ APP_TITLE }}</span>
        </RouterLink>

        <div class="sheader__searchwrap">
          <form class="sheader__search" role="search" @submit.prevent="search">
            <input
              v-model="keyword"
              type="search"
              class="sheader__input"
              placeholder="搜索 3C 数码 / 智能硬件，如：蓝牙耳机"
              aria-label="搜索商品"
            />
            <button type="submit" class="sheader__submit">搜索</button>
          </form>
          <p class="sheader__hotwords">
            <button
              v-for="word in HOT_WORDS"
              :key="word"
              type="button"
              class="sheader__hotword"
              @click="searchHotWord(word)"
            >
              {{ word }}
            </button>
          </p>
        </div>

        <RouterLink :to="{ name: 'cart' }" class="sheader__cart">
          <span class="sheader__cart-icon" aria-hidden="true">🛒</span>
          <span>购物车</span>
          <span class="sheader__cart-count">{{ cart.itemCount }}</span>
        </RouterLink>
      </div>
    </header>

    <!-- 3. category strip ------------------------------------------------- -->
    <div class="catbar">
      <div class="nx-container catbar__inner">
        <div
          class="catbar__all"
          @mouseenter="categoryOpen = true"
          @mouseleave="categoryOpen = false"
        >
          <button type="button" class="catbar__allbtn" @click="categoryOpen = !categoryOpen">
            全部商品分类
          </button>

          <!-- Hover panel: hidden on the home page, where the sticky sidebar owns it. -->
          <div v-if="categoryOpen && !isHome" class="catpanel">
            <button
              v-for="group in CATEGORY_MENU"
              :key="group.name"
              type="button"
              class="catpanel__item"
              @click="goCategory(group.name)"
            >
              {{ group.name }} <span class="catpanel__arrow" aria-hidden="true">›</span>
            </button>
          </div>
        </div>

        <nav class="catbar__links">
          <RouterLink :to="{ name: 'search' }" class="catbar__link">全部商品</RouterLink>
          <RouterLink :to="{ name: 'ai-assistant' }" class="catbar__link">AI 助手</RouterLink>
          <RouterLink :to="{ name: 'orders' }" class="catbar__link">我的订单</RouterLink>
          <RouterLink :to="{ name: 'coupons' }" class="catbar__link">优惠券</RouterLink>
          <RouterLink :to="{ name: 'after-sales' }" class="catbar__link">售后服务</RouterLink>
        </nav>
      </div>
    </div>

    <main class="store-main">
      <RouterView />
    </main>

    <!-- 4. service guarantees + footer ------------------------------------ -->
    <footer class="sfooter">
      <div class="nx-container">
        <ul class="sfooter__promises">
          <li><strong>正品保障</strong><span>官方授权渠道</span></li>
          <li><strong>极速发货</strong><span>仓库实时库存</span></li>
          <li><strong>无忧退换</strong><span>7 天无理由</span></li>
          <li><strong>价格透明</strong><span>下单即锁定价格</span></li>
        </ul>
        <div class="sfooter__legal">
          <p>{{ APP_TITLE }} · AI 原生的 3C 电商运营平台</p>
          <p class="nx-muted">
            金额一律以整数分（minor units）存储与传输；订单价格在下单时快照，不随后续改价变化。
          </p>
        </div>
      </div>
    </footer>
  </div>
</template>

<style scoped lang="scss">
.store-shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
  background: var(--nx-page-bg);
}

/* -- 1. utility bar ------------------------------------------------------- */
.utilbar {
  height: var(--nx-utilitybar-height);
  background: var(--nx-surface-sunken);
  border-bottom: 1px solid var(--nx-border);
  font-size: 12px;
  color: var(--nx-text-secondary);

  &__inner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 100%;
  }

  &__left,
  &__right {
    display: flex;
    align-items: center;
    gap: 0;
  }

  &__city {
    color: var(--nx-text-muted);
  }

  &__welcome {
    margin-left: 12px;
    padding-left: 12px;
    border-left: 1px solid var(--nx-border);
  }

  &__link {
    padding: 0 10px;
    border-left: 1px solid var(--nx-border);
    color: var(--nx-text-secondary);

    &:hover {
      color: var(--nx-brand);
    }

    &--accent {
      color: var(--nx-brand);
    }
  }

  &__left > .utilbar__link:first-of-type {
    margin-left: 12px;
  }

  &__button {
    border-top: none;
    border-right: none;
    border-bottom: none;
    background: transparent;
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
  }
}

/* -- 2. header ------------------------------------------------------------ */
.sheader {
  background: var(--nx-header-bg);
  border-bottom: 1px solid var(--nx-border);

  &__inner {
    display: flex;
    align-items: center;
    gap: 40px;
    height: var(--nx-header-height);
  }

  &__brand {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 0 0 auto;
    color: var(--nx-text);

    &:hover {
      color: var(--nx-brand);
    }
  }

  &__brand-mark {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 34px;
    height: 34px;
    background: var(--nx-brand);
    color: #fff;
    font-size: 18px;
    font-weight: 700;
  }

  &__brand-name {
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }

  /* The search box takes the centre: it is the primary navigation affordance. */
  &__searchwrap {
    flex: 1 1 auto;
    max-width: 620px;
  }

  &__search {
    display: flex;
    height: 36px;
    border: 2px solid var(--nx-brand);
  }

  &__input {
    flex: 1;
    min-width: 0;
    padding: 0 10px;
    border: none;
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 14px;

    &:focus {
      outline: none;
    }
  }

  &__submit {
    width: 82px;
    border: none;
    background: var(--nx-brand);
    color: #fff;
    font-family: inherit;
    font-size: 14px;
    cursor: pointer;

    &:hover {
      background: var(--nx-brand-hover);
    }
  }

  &__hotwords {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 4px 0 0;
    font-size: 12px;
  }

  &__hotword {
    padding: 0;
    border: none;
    background: transparent;
    color: var(--nx-text-muted);
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;

    &:hover {
      color: var(--nx-brand);
    }
  }

  &__cart {
    display: flex;
    align-items: center;
    gap: 6px;
    flex: 0 0 auto;
    height: 36px;
    padding: 0 14px;
    border: 1px solid var(--nx-border);
    background: var(--nx-surface-sunken);
    color: var(--nx-brand);
    font-size: 14px;

    &:hover {
      border-color: var(--nx-brand);
      color: var(--nx-brand);
    }
  }

  &__cart-icon {
    font-size: 15px;
  }

  &__cart-count {
    min-width: 18px;
    padding: 0 4px;
    background: var(--nx-brand);
    color: #fff;
    font-size: 12px;
    text-align: center;
  }
}

/* -- 3. category strip ---------------------------------------------------- */
.catbar {
  background: var(--nx-surface);
  border-bottom: 1px solid var(--nx-border);

  &__inner {
    display: flex;
    align-items: stretch;
    height: 36px;
  }

  &__all {
    position: relative;
    flex: 0 0 200px;
  }

  &__allbtn {
    width: 100%;
    height: 36px;
    border: none;
    background: var(--nx-brand);
    color: #fff;
    font-family: inherit;
    font-size: 14px;
    font-weight: 700;
    cursor: pointer;
  }

  &__links {
    display: flex;
    align-items: center;
    gap: 22px;
    padding-left: 22px;
  }

  &__link {
    color: var(--nx-text);
    font-size: 14px;

    &:hover {
      color: var(--nx-brand);
    }
  }
}

.catpanel {
  position: absolute;
  top: 36px;
  left: 0;
  z-index: 30;
  width: 200px;
  padding: 6px 0;
  background: var(--nx-surface);
  border: 1px solid var(--nx-border);
  box-shadow: var(--nx-shadow-pop);

  &__item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    padding: 6px 12px;
    border: none;
    background: transparent;
    color: var(--nx-text);
    font-family: inherit;
    font-size: 12px;
    text-align: left;
    cursor: pointer;

    &:hover {
      background: var(--nx-brand-soft);
      color: var(--nx-brand);
    }
  }

  &__arrow {
    color: var(--nx-text-muted);
  }
}

/* -- 4. content + footer -------------------------------------------------- */
.store-main {
  flex: 1;
  padding: 10px 0 30px;
}

.sfooter {
  margin-top: 20px;
  padding: 20px 0 28px;
  background: var(--nx-surface);
  border-top: 1px solid var(--nx-border);

  &__promises {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin: 0 0 18px;
    padding: 0;
    list-style: none;

    li {
      display: flex;
      flex-direction: column;
      gap: 2px;
      padding-left: 10px;
      border-left: 3px solid var(--nx-brand);
    }

    strong {
      font-size: 14px;
    }

    span {
      color: var(--nx-text-muted);
      font-size: 12px;
    }
  }

  &__legal p {
    margin: 0;
    font-size: 12px;
    color: var(--nx-text-secondary);
  }
}

@media (max-width: 1000px) {
  .sheader__inner {
    gap: 16px;
  }

  .catbar__all {
    flex: 0 0 150px;
  }

  .sfooter__promises {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
