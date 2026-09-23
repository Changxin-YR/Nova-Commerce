<script setup lang="ts">
/**
 * Console shell — the data-dense operations layout:
 *
 *   [ fixed left menu 200px ][ topbar 50px: breadcrumb + context ]
 *   [ content: filter bar -> dense table -> pager ]
 *
 * Conventions that make it an operations tool rather than a marketing site: a persistent
 * left menu (no hamburger), a breadcrumb trail instead of a big page header, 12px type,
 * hairline table rows, and status/labels that are always visible rather than hover-revealed.
 *
 * The menu is FILTERED by permission codes. That filtering is UX ONLY — the backend
 * re-checks every request, and hiding a menu entry protects nothing (§104).
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
  permission?: string
}

/** Menu grouped the way an operator thinks about the work. */
const MENU_GROUPS: { title: string; items: MenuItem[] }[] = [
  {
    title: '经营',
    items: [
      { name: 'console-dashboard', label: '经营概览', permission: 'analytics:read' },
      { name: 'console-analytics', label: '数据分析', permission: 'analytics:read' },
    ],
  },
  {
    title: '交易',
    items: [
      { name: 'console-orders', label: '订单管理', permission: 'order:read' },
      { name: 'console-after-sales', label: '售后管理', permission: 'aftersale:read' },
    ],
  },
  {
    title: '商品',
    items: [
      { name: 'console-products', label: '商品管理', permission: 'product:read' },
      { name: 'console-inventory', label: '库存管理', permission: 'inventory:read' },
      { name: 'console-marketing', label: '营销中心', permission: 'marketing:read' },
    ],
  },
  {
    title: '智能',
    items: [
      { name: 'console-ai', label: 'AI 工作台', permission: 'agent:read' },
      { name: 'console-knowledge', label: '知识中心', permission: 'knowledge:read' },
    ],
  },
  {
    title: '系统',
    items: [{ name: 'console-system', label: '系统状态', permission: 'system:read' }],
  },
]

/**
 * UX-ONLY filter (§104). A user who cannot see an entry could still navigate to it by
 * URL, and the server would answer FORBIDDEN (20008) / INSUFFICIENT_PERMISSION (20009).
 * That is the intended design: the guard is a courtesy, the API is the authority.
 */
const visibleGroups = computed(() =>
  MENU_GROUPS.map((group) => ({
    title: group.title,
    items: group.items.filter(
      (item) => !item.permission || !permission.loaded || permission.hasAny([item.permission]),
    ),
  })).filter((group) => group.items.length > 0),
)

const currentTitle = computed(() => (typeof route.meta.title === 'string' ? route.meta.title : ''))
const collapsed = computed(() => app.consoleSidebarCollapsed)

/** Breadcrumb: 首页 / 分组 / 当前页. */
const activeGroup = computed(
  () => visibleGroups.value.find((g) => g.items.some((i) => i.name === route.name))?.title ?? '',
)
</script>

<template>
  <div class="console" :class="{ 'console--collapsed': collapsed }">
    <!-- left menu --------------------------------------------------------- -->
    <aside class="csidebar">
      <div class="csidebar__brand">
        <span class="csidebar__mark">N</span>
        <span v-show="!collapsed" class="csidebar__name">{{ APP_TITLE }} 商家后台</span>
      </div>

      <nav class="csidebar__nav" aria-label="商家后台导航">
        <template v-for="group in visibleGroups" :key="group.title">
          <p v-show="!collapsed" class="csidebar__group">{{ group.title }}</p>
          <RouterLink
            v-for="item in group.items"
            :key="item.name"
            :to="{ name: item.name }"
            class="csidebar__item"
            :class="{ 'csidebar__item--active': route.name === item.name }"
            :title="collapsed ? item.label : undefined"
          >
            {{ item.label }}
          </RouterLink>
        </template>
      </nav>

      <button
        type="button"
        class="csidebar__collapse"
        :aria-label="collapsed ? '展开菜单' : '收起菜单'"
        @click="app.toggleConsoleSidebar()"
      >
        {{ collapsed ? '»' : '«' }}
      </button>
    </aside>

    <div class="cmain">
      <!-- topbar ---------------------------------------------------------- -->
      <header class="ctopbar">
        <nav class="ctopbar__crumb" aria-label="面包屑">
          <span class="ctopbar__crumb-item">首页</span>
          <template v-if="activeGroup">
            <span class="ctopbar__crumb-sep" aria-hidden="true">/</span>
            <span class="ctopbar__crumb-item">{{ activeGroup }}</span>
          </template>
          <span class="ctopbar__crumb-sep" aria-hidden="true">/</span>
          <span class="ctopbar__crumb-item ctopbar__crumb-item--current">{{ currentTitle }}</span>
        </nav>

        <div class="ctopbar__right">
          <span class="ctopbar__op">
            {{ auth.displayName }}
            <em v-if="permission.roles.length">{{ permission.roles.join(' / ') }}</em>
          </span>
          <button
            v-if="notifications.unreadCount > 0"
            type="button"
            class="nx-btn nx-btn--sm"
            @click="notifications.markAllRead()"
          >
            消息 {{ notifications.unreadCount }}
          </button>
          <button type="button" class="nx-btn nx-btn--sm" @click="app.toggleTheme()">
            {{ app.isDark ? '浅色' : '深色' }}
          </button>
          <button type="button" class="nx-btn nx-btn--sm" @click="router.push({ name: 'home' })">
            前台商城
          </button>
          <button type="button" class="nx-btn nx-btn--sm" @click="auth.logout()">退出</button>
        </div>
      </header>

      <main class="ccontent">
        <RouterView />
      </main>
    </div>
  </div>
</template>

<style scoped lang="scss">
.console {
  display: grid;
  grid-template-columns: var(--nx-console-sidebar-width) 1fr;
  min-height: 100vh;
  background: var(--nx-page-bg);

  &--collapsed {
    grid-template-columns: var(--nx-console-sidebar-collapsed) 1fr;
  }
}

/* -- left menu ------------------------------------------------------------- */
.csidebar {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: #2b2f38;
  color: #c9ced8;

  &__brand {
    display: flex;
    align-items: center;
    gap: 8px;
    height: var(--nx-console-topbar-height);
    padding: 0 12px;
    border-bottom: 1px solid #3a3f4a;
    overflow: hidden;
  }

  &__mark {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    flex: 0 0 24px;
    background: var(--nx-brand);
    color: #fff;
    font-size: 14px;
    font-weight: 700;
  }

  &__name {
    font-size: 13px;
    font-weight: 700;
    color: #fff;
    white-space: nowrap;
  }

  &__nav {
    flex: 1;
    padding: 8px 0;
    overflow-y: auto;
  }

  &__group {
    margin: 8px 0 4px;
    padding: 0 12px;
    color: #7b8291;
    font-size: 12px;
  }

  &__item {
    display: block;
    padding: 8px 12px;
    border-left: 3px solid transparent;
    color: #c9ced8;
    font-size: 13px;
    white-space: nowrap;

    &:hover {
      background: #343945;
      color: #fff;
    }

    &--active {
      background: #343945;
      border-left-color: var(--nx-brand);
      color: #fff;
      font-weight: 700;
    }
  }

  &__collapse {
    height: 34px;
    border: none;
    border-top: 1px solid #3a3f4a;
    background: #24272f;
    color: #7b8291;
    font-family: inherit;
    font-size: 14px;
    cursor: pointer;

    &:hover {
      color: #fff;
    }
  }
}

/* -- topbar ---------------------------------------------------------------- */
.cmain {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.ctopbar {
  position: sticky;
  top: 0;
  z-index: 15;
  display: flex;
  align-items: center;
  gap: 16px;
  height: var(--nx-console-topbar-height);
  padding: 0 16px;
  background: var(--nx-surface);
  border-bottom: 1px solid var(--nx-border);

  &__crumb {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--nx-text-muted);
  }

  &__crumb-item--current {
    color: var(--nx-text);
    font-weight: 700;
  }

  &__crumb-sep {
    color: var(--nx-border-strong);
  }

  &__right {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-left: auto;
  }

  &__op {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;

    em {
      padding: 1px 5px;
      border: 1px solid var(--nx-border-strong);
      color: var(--nx-text-muted);
      font-style: normal;
      font-size: 12px;
    }
  }
}

.ccontent {
  flex: 1;
  padding: 12px;
  min-width: 0;
}
</style>
