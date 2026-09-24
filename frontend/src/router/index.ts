/**
 * Routes for BOTH shells in ONE build (§100, REQ-FE-002).
 *
 *   /            consumer store  (StoreLayout)
 *   /console/*   merchant console (ConsoleLayout)
 *
 * !!! PERMISSION IS UX ONLY (§104) !!!
 * The `meta.permission` / `meta.requiresAuth` checks below decide what to SHOW. They
 * are not a security boundary: a user can edit the URL, and the backend still
 * re-checks every request and returns FORBIDDEN when appropriate. Never move an
 * authorization decision from the server into this file.
 *
 * Every route is lazily imported so both shells share one bundle without paying for
 * the other's pages up front.
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import { usePermissionStore } from '@/stores/permission'

const StoreLayout = () => import('@/layouts/StoreLayout.vue')
const ConsoleLayout = () => import('@/layouts/ConsoleLayout.vue')

const routes: RouteRecordRaw[] = [
  // -- consumer store -------------------------------------------------------
  {
    path: '/',
    component: StoreLayout,
    children: [
      {
        path: '',
        name: 'home',
        component: () => import('@/views/consumer/HomeView.vue'),
        meta: { title: '首页' },
      },
      {
        path: 'search',
        name: 'search',
        component: () => import('@/views/consumer/SearchView.vue'),
        meta: { title: '搜索' },
      },
      {
        path: 'product/:id',
        name: 'product',
        component: () => import('@/views/consumer/ProductView.vue'),
        meta: { title: '商品详情' },
      },
      {
        path: 'cart',
        name: 'cart',
        component: () => import('@/views/consumer/CartView.vue'),
        meta: { title: '购物车', requiresAuth: true },
      },
      {
        path: 'checkout',
        name: 'checkout',
        component: () => import('@/views/consumer/CheckoutView.vue'),
        meta: { title: '确认订单', requiresAuth: true },
      },
      {
        path: 'mock-pay/:paymentId',
        name: 'mock-pay',
        component: () => import('@/views/consumer/MockPayView.vue'),
        meta: { title: '模拟支付', requiresAuth: true },
      },
      {
        path: 'orders',
        name: 'orders',
        component: () => import('@/views/consumer/OrdersView.vue'),
        meta: { title: '我的订单', requiresAuth: true },
      },
      {
        path: 'orders/:orderNo',
        name: 'order-detail',
        component: () => import('@/views/consumer/OrderDetailView.vue'),
        meta: { title: '订单详情', requiresAuth: true },
      },
      {
        path: 'after-sales',
        name: 'after-sales',
        component: () => import('@/views/consumer/AfterSalesView.vue'),
        meta: { title: '售后服务', requiresAuth: true },
      },
      {
        path: 'after-sales/:afterSaleNo',
        name: 'after-sale-detail',
        component: () => import('@/views/consumer/AfterSaleDetailView.vue'),
        meta: { title: '售后详情', requiresAuth: true },
      },
      {
        path: 'profile',
        name: 'profile',
        component: () => import('@/views/consumer/ProfileView.vue'),
        meta: { title: '个人中心', requiresAuth: true },
      },
      {
        path: 'addresses',
        name: 'addresses',
        component: () => import('@/views/consumer/AddressesView.vue'),
        meta: { title: '收货地址', requiresAuth: true },
      },
      {
        path: 'coupons',
        name: 'coupons',
        component: () => import('@/views/consumer/CouponsView.vue'),
        meta: { title: '优惠券', requiresAuth: true },
      },
      {
        path: 'assistant',
        name: 'ai-assistant',
        component: () => import('@/views/consumer/AssistantView.vue'),
        meta: { title: 'AI 助手' },
      },
    ],
  },

  // -- merchant console -----------------------------------------------------
  {
    path: '/console',
    component: ConsoleLayout,
    meta: { requiresAuth: true, console: true },
    children: [
      { path: '', redirect: { name: 'console-dashboard' } },
      {
        path: 'dashboard',
        name: 'console-dashboard',
        component: () => import('@/views/console/DashboardView.vue'),
        meta: { title: '经营概览', permission: 'analytics:read' },
      },
      {
        path: 'products',
        name: 'console-products',
        component: () => import('@/views/console/ProductsView.vue'),
        meta: { title: '商品管理', permission: 'product:read' },
      },
      {
        path: 'inventory',
        name: 'console-inventory',
        component: () => import('@/views/console/InventoryView.vue'),
        meta: { title: '库存管理', permission: 'inventory:read' },
      },
      {
        path: 'orders',
        name: 'console-orders',
        component: () => import('@/views/console/OrdersView.vue'),
        meta: { title: '订单管理', permission: 'order:read' },
      },
      {
        path: 'after-sales',
        name: 'console-after-sales',
        component: () => import('@/views/console/AfterSalesView.vue'),
        meta: { title: '售后管理', permission: 'aftersale:read' },
      },
      {
        path: 'marketing',
        name: 'console-marketing',
        component: () => import('@/views/console/MarketingView.vue'),
        meta: { title: '营销中心', permission: 'marketing:read' },
      },
      {
        path: 'analytics',
        name: 'console-analytics',
        component: () => import('@/views/console/AnalyticsView.vue'),
        meta: { title: '数据分析', permission: 'analytics:read' },
      },
      {
        path: 'ai',
        name: 'console-ai',
        component: () => import('@/views/console/AiWorkspaceView.vue'),
        meta: { title: 'AI 工作台', permission: 'agent:read' },
      },
      {
        path: 'knowledge',
        name: 'console-knowledge',
        component: () => import('@/views/console/KnowledgeView.vue'),
        meta: { title: '知识中心', permission: 'knowledge:read' },
      },
      {
        path: 'system',
        name: 'console-system',
        component: () => import('@/views/console/SystemView.vue'),
        meta: { title: '系统状态', permission: 'system:read' },
      },
    ],
  },

  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/consumer/LoginView.vue'),
    meta: { title: '登录', public: true },
  },

  {
    path: '/forbidden',
    name: 'forbidden',
    component: () => import('@/views/ForbiddenView.vue'),
    meta: { title: '无权限', public: true },
  },

  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在', public: true },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior(_to, _from, savedPosition) {
    return savedPosition ?? { top: 0 }
  },
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const permission = usePermissionStore()

  // Restore the session once per page load (a stored token survives a refresh).
  if (!auth.bootstrapped) await auth.bootstrap()

  // --- UX-ONLY GATE (the backend is the real authority, §104) --------------
  // Everything below decides what to RENDER, never what is ALLOWED. A user who edits
  // the URL or replays the request with curl reaches the API exactly the same way; the
  // server then answers FORBIDDEN (20008) / INSUFFICIENT_PERMISSION (20009) /
  // DATA_SCOPE_VIOLATION (20010). Do not move an authorization decision into this file.
  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  if (to.meta.console) {
    // UX only (§104): hides the console from accounts without a console role. The API
    // still enforces merchant scope on every console request.
    if (!permission.canAccessConsole) return { name: 'forbidden' }

    // UX only (§104): hides a page the account has no permission code for. The same
    // GET/POST replayed directly still returns FORBIDDEN from the server.
    const required = to.meta.permission
    if (typeof required === 'string' && permission.loaded && !permission.hasAny([required])) {
      return { name: 'forbidden' }
    }
  }

  return true
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : undefined
  useAppStore().setDocumentTitle(title)
})

export default router
