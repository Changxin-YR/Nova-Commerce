<script setup lang="ts">
/**
 * Consumer store shell (top navigation + footer + cart badge).
 *
 * Same build as the console shell (§100): the two layouts share stores, the HTTP
 * client and every UI-state component.
 */
import { computed, onMounted } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
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

const isAssistRoute = computed(() => route.name === 'ai-assistant')

const navItems = [
  { name: 'home', label: '首页' },
  { name: 'search', label: '全部商品' },
  { name: 'ai-assistant', label: 'AI 助手' },
]

onMounted(() => {
  // The cart badge is genuinely cross-page state, so it is refreshed by the shell
  // rather than by each page. A failure here must not break navigation.
  if (auth.isLoggedIn) {
    void cart.load().catch(() => undefined)
  }
})

function goToConsole(): void {
  void router.push({ name: 'console-dashboard' })
}
</script>

<template>
  <div class="store-shell">
    <header class="store-header">
      <div class="nx-container store-header__inner">
        <RouterLink :to="{ name: 'home' }" class="store-header__brand">
          {{ APP_TITLE }}
        </RouterLink>

        <!-- Menu visibility is UX only; the backend authorizes every request (§104). -->
        <nav class="store-header__nav">
          <RouterLink
            v-for="item in navItems"
            :key="item.name"
            :to="{ name: item.name }"
            class="store-header__link"
            :class="{ 'store-header__link--active': route.name === item.name }"
          >
            {{ item.label }}
          </RouterLink>
        </nav>

        <div class="store-header__actions">
          <button
            type="button"
            class="nx-btn nx-btn--ghost"
            :aria-label="app.isDark ? '切换到浅色主题' : '切换到深色主题'"
            @click="app.toggleTheme()"
          >
            {{ app.isDark ? '☀' : '☾' }}
          </button>

          <RouterLink
            v-if="auth.isLoggedIn"
            :to="{ name: 'cart' }"
            class="store-header__cart"
            aria-label="购物车"
          >
            购物车
            <span v-if="cart.itemCount > 0" class="store-header__badge">{{ cart.itemCount }}</span>
          </RouterLink>

          <RouterLink v-if="auth.isLoggedIn" :to="{ name: 'orders' }" class="nx-btn nx-btn--ghost">
            我的订单
          </RouterLink>

          <button
            v-if="auth.isLoggedIn && notifications.unreadCount > 0"
            type="button"
            class="nx-btn nx-btn--ghost"
            @click="notifications.markAllRead()"
          >
            通知 {{ notifications.unreadCount }}
          </button>

          <button v-if="auth.isLoggedIn" type="button" class="nx-btn nx-btn--ghost" @click="goToConsole">
            商家后台
          </button>

          <RouterLink v-if="!auth.isLoggedIn" :to="{ name: 'login' }" class="nx-btn nx-btn--primary">
            登录
          </RouterLink>

          <button v-else type="button" class="nx-btn" @click="auth.logout()">退出</button>
        </div>
      </div>
    </header>

    <main class="store-main" :class="{ 'store-main--flush': isAssistRoute }">
      <RouterView v-slot="{ Component }">
        <component :is="Component" />
      </RouterView>
    </main>

    <footer class="store-footer">
      <div class="nx-container">
        <p>Nexora Commerce · AI 原生的 3C 电商运营平台</p>
        <p class="nx-muted">
          金额一律以整数分（minor units）存储与传输；订单价格在下单时快照，不随后续改价变化。
        </p>
      </div>
    </footer>
  </div>
</template>

<style scoped lang="scss">
.store-shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.store-header {
  position: sticky;
  top: 0;
  z-index: 20;
  height: var(--nx-header-height);
  background: var(--nx-surface);
  border-bottom: 1px solid var(--nx-border);
  backdrop-filter: blur(8px);

  &__inner {
    display: flex;
    align-items: center;
    gap: 20px;
    height: 100%;
  }

  &__brand {
    font-size: 17px;
    font-weight: 600;
    letter-spacing: -0.019em;
    color: var(--nx-text);
    text-decoration: none;
    white-space: nowrap;
  }

  &__nav {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-left: 8px;
  }

  &__link {
    padding: 6px 12px;
    border-radius: var(--nx-radius-control);
    color: var(--nx-text-muted);
    text-decoration: none;
    font-size: 14px;

    &:hover {
      color: var(--nx-text);
      background: var(--nx-surface-sunken);
    }

    &--active {
      color: var(--nx-primary);
      background: var(--nx-primary-soft);
      font-weight: 600;
    }
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
  }

  &__cart {
    position: relative;
    padding: 6px 12px;
    border-radius: var(--nx-radius-control);
    color: var(--nx-text);
    text-decoration: none;
    font-size: 14px;
  }

  &__badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 18px;
    height: 18px;
    margin-left: 6px;
    padding: 0 5px;
    border-radius: var(--nx-radius-pill);
    background: var(--nx-primary);
    color: #fff;
    font-size: 11px;
    line-height: 1;
  }
}

.store-main {
  flex: 1;
  padding: 24px 0 48px;

  &--flush {
    padding-bottom: 0;
  }
}

.store-footer {
  padding: 20px 0 28px;
  border-top: 1px solid var(--nx-border);

  p {
    margin: 0;
    font-size: 13px;
    color: var(--nx-text-secondary);
  }
}

@media (max-width: 860px) {
  .store-header__nav {
    display: none;
  }
}
</style>
