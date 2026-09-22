<script setup lang="ts">
/**
 * Root component: a thin shell.
 *
 * It renders a global progress bar while any request is in flight, a global error
 * banner, and the routed view. Keeping it thin is deliberate — layout belongs to the
 * two layout shells, not here.
 */
import { RouterView } from 'vue-router'
import { useAppStore } from '@/stores/app'

const app = useAppStore()
</script>

<template>
  <div class="nx-root">
    <div v-if="app.isBusy" class="nx-progress" role="progressbar" aria-label="请求进行中" />
    <div v-if="app.globalError" class="nx-global-error" role="alert">
      {{ app.globalError }}
      <button type="button" class="nx-btn nx-btn--ghost" @click="app.setGlobalError('')">关闭</button>
    </div>
    <RouterView />
  </div>
</template>

<style scoped lang="scss">
.nx-root {
  min-height: 100vh;
}

.nx-progress {
  position: fixed;
  top: 0;
  left: 0;
  z-index: 4000;
  width: 100%;
  height: 2px;
  background: linear-gradient(90deg, var(--nx-primary), var(--nx-info));
  animation: nx-progress-slide 1.2s ease-in-out infinite;
}

.nx-global-error {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 16px;
  background: var(--nx-danger-soft);
  color: var(--nx-danger);
  font-size: 13px;
}

@keyframes nx-progress-slide {
  0% {
    transform: translateX(-40%);
  }
  100% {
    transform: translateX(100%);
  }
}
</style>
