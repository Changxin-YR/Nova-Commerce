<script setup lang="ts">
/**
 * Login. The redirect target comes from the query string so a guarded deep link
 * resumes after authentication.
 */
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const notifications = useNotificationStore()

const form = reactive({ username: '', password: '' })
const localError = ref('')

const redirect = computed(() => {
  const value = route.query.redirect
  return typeof value === 'string' && value.startsWith('/') ? value : '/'
})

async function submit(): Promise<void> {
  localError.value = ''
  if (!form.username || !form.password) {
    localError.value = '请输入账号和密码'
    return
  }
  try {
    await auth.login({ username: form.username, password: form.password })
    notifications.success('登录成功', `欢迎回来，${auth.displayName}`)
    await router.replace(redirect.value)
  } catch (e) {
    const normalized = normalizeError(e)
    localError.value = normalized.message
    // The trace id is what support needs; the raw code stays out of the copy.
    if (normalized.traceId) {
      notifications.error('登录失败', normalized.message, normalized.code, normalized.traceId)
    }
  }
}
</script>

<template>
  <div class="nx-container login">
    <form class="login__card nx-card" @submit.prevent="submit">
      <div class="nx-card__body">
        <h1 class="nx-page-title">登录 Nexora</h1>
        <p class="nx-muted login__hint">登录后可使用购物车、订单、售后与商家后台。</p>

        <label class="login__field">
          <span>账号</span>
          <input v-model="form.username" autocomplete="username" />
        </label>

        <label class="login__field">
          <span>密码</span>
          <input v-model="form.password" type="password" autocomplete="current-password" />
        </label>

        <p v-if="localError" class="login__error" role="alert">{{ localError }}</p>

        <button type="submit" class="nx-btn nx-btn--primary login__submit" :disabled="auth.loading">
          {{ auth.loading ? '登录中…' : '登录' }}
        </button>

        <p class="nx-muted login__note">
          权限（路由守卫、菜单、按钮）都只是体验层：真正的鉴权在服务端，每次请求都会重新校验。
        </p>
      </div>
    </form>
  </div>
</template>

<style scoped lang="scss">
.login {
  display: flex;
  justify-content: center;
  padding: 48px 20px;

  &__card {
    width: 100%;
    max-width: 400px;
  }

  &__hint {
    margin: 8px 0 20px;
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 14px;
    font-size: 13px;

    input {
      height: 38px;
      padding: 0 12px;
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
  }

  &__error {
    margin: 0 0 12px;
    color: var(--nx-danger);
    font-size: 13px;
  }

  &__submit {
    width: 100%;
    min-height: 40px;
  }

  &__note {
    margin: 16px 0 0;
    font-size: 11.5px;
    line-height: 1.6;
  }
}
</style>
