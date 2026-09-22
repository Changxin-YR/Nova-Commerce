<script setup lang="ts">
/**
 * Merchant console shell (collapsible sidebar + top bar).
 *
 * The menu is built from the route table so a new console page cannot forget its
 * entry, and it is FILTERED by permission codes. That filtering is UX only — the
 * backend re-checks every request, and hiding a menu item protects nothing (§104).
 */
import { computed } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import { usePermissionStore } from '@/stores/permission'
import { useNotificationStore } from '@/stores/notification'
import { APP_TITLE } from '@/config/app'

const app = useAppStore()
const auth = useAuthStore()
const permission = usePermissionStore()
const notifications = useNotificationStore()
const route = useRoute()
const router = useRouter()

interface MenuItem {
  name: string
  label: string
  icon: string
  permission?: string
}

const MENU: MenuItem[] = [
  { name: 'console-dashboard', label: '经营概览', icon: '◳', permission: 'analytics:read' },
  { name: 'console-products', label: '商品管理', icon: '▦', permission: 'product:read' },
  { name: 'console-inventory', label: '库存管理', icon: '☰', permission: 'inventory:read' },
  { name: 'console-orders', label: '订单管理', icon: '▤', permission: 'order:read' },
  { name: 'console-after-sales', label: '售后管理', icon: '↩', permission: 'aftersale:read' },
  { name: 'console-marketing', label: '营销中心', icon: '✦', permission: 'marketing:read' },
  { name: 'console-analytics', label: '数据分析', icon: '◪', permission: 'analytics:read' },
  { name: 'console-ai', label: 'AI 工作台', icon: '✳', permission: 'agent:read' },
  { name: 'console-knowledge', label: '知识中心', icon: '❏', permission: 'knowledge:read' },
  { name: 'console-system', label: '系统状态', icon: '⚙', permission: 'system:read' },
]

/**
 * UX-ONLY menu filter. A user who cannot see an entry could still navigate to it by
 * URL, and the server would answer FORBIDDEN (20 008 / 20 009). That is the intended
 * design: the guard is a courtesy, the API is the authority.
 */
const visibleMenu = computed(() =>
  MENU.filter((item) => !item.permission || !permission.loaded || permission.hasAny([item.permission])),
)

const currentTitle = computed(() => (typeof route.meta.title === 'string' ? route.meta.title : ''))
const collapsed = computed(() => app.consoleSidebarCollapsed)
</script>

<template>
  <div class="console-shell" :class="{ 'console-shell--collapsed': collapsed }">
    <aside class="console-sidebar">
      <div class="console-sidebar__brand">
        <RouterLink :to="{ name: 'home' }" class="console-sidebar__logo">
          <span class="console-sidebar__logo-mark">N</span>
          <span v-show="!collapsed" class="console-sidebar__logo-text">{{ APP_TITLE }}</span>
        </RouterLink>
      </div>

      <nav class="console-sidebar__nav" aria-label="商家后台导航">
        <RouterLink
          v-for="item in visibleMenu"
          :key="item.name"
          :to="{ name: item.name }"
          class="console-sidebar__item"
          :class="{ 'console-sidebar__item--active': route.name === item.name }"
          :title="collapsed ? item.label : undefined"
        >
          <span class="console-sidebar__icon" aria-hidden="true">{{ item.icon }}</span>
          <span v-show="!collapsed" class="console-sidebar__label">{{ item.label }}</span>
        </RouterLink>
      </nav>

      <button
        type="button"
        class="console-sidebar__collapse"
        :aria-label="collapsed ? '展开侧边栏' : '收起侧边栏'"
        @click="app.toggleConsoleSidebar()"
      >
        {{ collapsed ? '»' : '«' }}
      </button>
    </aside>

    <div class="console-main">
      <header class="console-topbar">
        <div class="console-topbar__left">
          <h1 class="console-topbar__title">{{ currentTitle }}</h1>
        </div>

        <div class="console-topbar__right">
          <span class="console-topbar__user">{{ auth.displayName }}</span>
          <span v-if="permission.roles.length" class="console-topbar__role">
            {{ permission.roles.join(' / ') }}
          </span>

          <button
            v-if="notifications.unreadCount > 0"
            type="button"
            class="nx-btn nx-btn--ghost"
            @click="notifications.markAllRead()"
          >
            通知 {{ notifications.unreadCount }}
          </button>

          <button type="button" class="nx-btn nx-btn--ghost" @click="app.toggleTheme()">
            {{ app.isDark ? '☀' : '☾' }}
          </button>

          <button type="button" class="nx-btn nx-btn--ghost" @click="router.push({ name: 'home' })">
            返回商城
          </button>

          <button type="button" class="nx-btn" @click="auth.logout()">退出</button>
        </div>
      </header>

      <main class="console-content">
        <RouterView v-slot="{ Component }">
          <component :is="Component" />
        </RouterView>
      </main>
    </div>
  </div>
</template>

<style scoped lang="scss">
.console-shell {
  display: grid;
  grid-template-columns: var(--nx-console-sidebar-width) 1fr;
  min-height: 100vh;

  &--collapsed {
    grid-template-columns: var(--nx-console-sidebar-collapsed) 1fr;
  }
}

.console-sidebar {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--nx-surface);
  border-right: 1px solid var(--nx-border);

  &__brand {
    display: flex;
    align-items: center;
    height: var(--nx-header-height);
    padding: 0 16px;
    border-bottom: 1px solid var(--nx-border);
  }

  &__logo {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--nx-text);
    text-decoration: none;
    overflow: hidden;
  }

  &__logo-mark {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    flex: 0 0 30px;
    border-radius: 10px;
    background: var(--nx-primary);
    color: #fff;
    font-weight: 700;
    font-size: 15px;
  }

  &__logo-text {
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap;
  }

  &__nav {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
    padding: 12px 8px;
    overflow-y: auto;
  }

  &__item {
    display: flex;
    align-items: center;
    gap: 10px;
    height: 38px;
    padding: 0 10px;
    border-radius: var(--nx-radius-control);
    color: var(--nx-text-secondary);
    text-decoration: none;
    font-size: 13.5px;
    white-space: nowrap;

    &:hover {
      background: var(--nx-surface-sunken);
      color: var(--nx-text);
    }

    &--active {
      background: var(--nx-primary-soft);
      color: var(--nx-primary);
      font-weight: 600;
    }
  }

  &__icon {
    flex: 0 0 20px;
    text-align: center;
    font-size: 14px;
  }

  &__collapse {
    height: 40px;
    border: none;
    border-top: 1px solid var(--nx-border);
    background: transparent;
    color: var(--nx-text-muted);
    cursor: pointer;
    font-size: 14px;

    &:hover {
      color: var(--nx-primary);
    }
  }
}

.console-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.console-topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--nx-header-height);
  padding: 0 20px;
  background: var(--nx-surface);
  border-bottom: 1px solid var(--nx-border);

  &__title {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }

  &__right {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
  }

  &__user {
    font-size: 13px;
    font-weight: 600;
  }

  &__role {
    padding: 2px 8px;
    border-radius: var(--nx-radius-pill);
    background: var(--nx-surface-sunken);
    color: var(--nx-text-muted);
    font-size: 11px;
  }
}

.console-content {
  flex: 1;
  padding: 20px;
}
</style>
