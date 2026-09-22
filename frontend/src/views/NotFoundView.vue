<script setup lang="ts">
/** 404. Rendered inside the store shell so the user can navigate away. */
import { RouterLink } from 'vue-router'
</script>

<template>
  <div class="nx-container">
    <div class="not-found">
      <h1 class="nx-page-title">页面不存在</h1>
      <p class="nx-muted">你访问的地址没有对应的页面，可能是链接已失效。</p>
      <RouterLink :to="{ name: 'home' }" class="nx-btn nx-btn--primary">返回首页</RouterLink>
    </div>
  </div>
</template>

<style scoped lang="scss">
.not-found {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  padding: 80px 0;
  text-align: center;
}
</style>
