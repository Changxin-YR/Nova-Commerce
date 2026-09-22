<script setup lang="ts">
/** Profile: identity + entry points. Permission codes are shown for transparency. */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { usePermissionStore } from '@/stores/permission'
import StateView from '@/components/ui/StateView.vue'

const auth = useAuthStore()
const permission = usePermissionStore()

const status = computed(() => (auth.user ? 'success' : auth.loading ? 'loading' : 'permission_denied'))
</script>

<template>
  <div class="nx-container profile">
    <h1 class="nx-page-title">个人中心</h1>

    <StateView :state="status" compact>
      <div class="profile__grid">
        <section class="nx-card">
          <div class="nx-card__body">
            <h2 class="nx-section-title">账号信息</h2>
            <dl class="profile__list">
              <div><dt>用户名</dt><dd>{{ auth.user?.username }}</dd></div>
              <div><dt>昵称</dt><dd>{{ auth.user?.display_name }}</dd></div>
              <div><dt>手机号</dt><dd>{{ auth.user?.phone ?? '未绑定' }}</dd></div>
              <div><dt>邮箱</dt><dd>{{ auth.user?.email ?? '未绑定' }}</dd></div>
              <div>
                <dt>角色</dt>
                <dd>{{ permission.roles.join(' / ') || '普通用户' }}</dd>
              </div>
            </dl>
          </div>
        </section>

        <section class="nx-card">
          <div class="nx-card__body">
            <h2 class="nx-section-title">快捷入口</h2>
            <div class="profile__links">
              <RouterLink :to="{ name: 'orders' }" class="nx-btn">我的订单</RouterLink>
              <RouterLink :to="{ name: 'after-sales' }" class="nx-btn">售后申请</RouterLink>
              <RouterLink :to="{ name: 'addresses' }" class="nx-btn">收货地址</RouterLink>
              <RouterLink :to="{ name: 'ai-assistant' }" class="nx-btn">AI 助手</RouterLink>
            </div>
            <p class="nx-muted profile__note">
              按钮与菜单的显示由前端按权限码控制，仅为体验；服务端对每个请求独立鉴权。
            </p>
          </div>
        </section>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.profile {
  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px;
    margin-top: 14px;
  }

  &__list {
    margin: 0;

    > div {
      display: flex;
      justify-content: space-between;
      padding: 7px 0;
      border-bottom: 1px dashed var(--nx-border);
      font-size: 13px;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
    }
  }

  &__links {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  &__note {
    margin: 14px 0 0;
    font-size: 11.5px;
    line-height: 1.6;
  }
}
</style>
