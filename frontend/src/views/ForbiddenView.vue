<script setup lang="ts">
/**
 * PermissionDenied page (§104, §108).
 *
 * Reached when the route guard decides the account has no console role or lacks the
 * page's permission code. That decision is UX ONLY: the same request attempted by URL
 * or curl still hits the server, which answers FORBIDDEN / INSUFFICIENT_PERMISSION.
 */
import { RouterLink } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { usePermissionStore } from '@/stores/permission'

const auth = useAuthStore()
const permission = usePermissionStore()
</script>

<template>
  <div class="nx-container">
    <div class="forbidden">
      <span class="forbidden__icon" aria-hidden="true">🔒</span>
      <h1 class="nx-page-title">没有访问权限</h1>
      <p class="nx-muted">
        当前账号（{{ auth.displayName }}）无权访问该页面。如确需访问，请联系管理员为账号开通对应权限。
      </p>
      <p v-if="permission.roles.length" class="forbidden__roles">
        当前角色：<code>{{ permission.roles.join(' / ') }}</code>
      </p>
      <div class="forbidden__actions">
        <RouterLink :to="{ name: 'home' }" class="nx-btn nx-btn--primary">返回商城</RouterLink>
        <RouterLink v-if="auth.isLoggedIn" :to="{ name: 'orders' }" class="nx-btn">我的订单</RouterLink>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.forbidden {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 72px 0;
  text-align: center;

  &__icon {
    font-size: 32px;
  }

  &__roles code {
    font-size: 12px;
  }

  &__actions {
    display: flex;
    gap: 8px;
    margin-top: 8px;
  }
}
</style>
