<script setup lang="ts">
/**
 * Address book. The default-address toggle is a task-style POST (`/default`), not a
 * generic PATCH of a whole address body, so a partial form cannot blank a field.
 */
import { computed, reactive, ref } from 'vue'
import { addressApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import type { AddressPayload } from '@/types/api-contract'

const notifications = useNotificationStore()

const {
  data: addressData,
  status,
  error,
  execute,
} = useAsyncState(() => addressApi.list(), { immediate: true })

const addresses = computed(() => addressData.value ?? [])
const busy = ref(false)
const editingId = ref('')
const formOpen = ref(false)

const blank: AddressPayload = {
  receiver_name: '',
  receiver_phone: '',
  province: '',
  city: '',
  district: '',
  detail: '',
  postal_code: '',
  is_default: false,
  tag: '',
}

const form = reactive<AddressPayload>({ ...blank })

function resetForm(): void {
  Object.assign(form, blank)
  editingId.value = ''
  formOpen.value = false
}

function startEdit(id: string): void {
  const address = addresses.value.find((item) => item.id === id)
  if (!address) return
  editingId.value = id
  formOpen.value = true
  Object.assign(form, {
    receiver_name: address.receiver_name,
    receiver_phone: address.receiver_phone,
    province: address.province,
    city: address.city,
    district: address.district,
    detail: address.detail,
    postal_code: address.postal_code ?? '',
    is_default: address.is_default,
    tag: address.tag ?? '',
  })
}

async function submit(): Promise<void> {
  busy.value = true
  try {
    if (editingId.value) {
      await addressApi.update(editingId.value, { ...form })
      notifications.success('地址已更新')
    } else {
      await addressApi.create({ ...form })
      notifications.success('地址已添加')
    }
    resetForm()
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('保存失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}

async function remove(id: string): Promise<void> {
  busy.value = true
  try {
    await addressApi.remove(id)
    notifications.success('地址已删除')
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('删除失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}

async function setDefault(id: string): Promise<void> {
  busy.value = true
  try {
    await addressApi.setDefault(id)
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('设置默认地址失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="nx-container addresses">
    <div class="addresses__head">
      <h1 class="nx-page-title">收货地址</h1>
      <button type="button" class="nx-btn nx-btn--primary" @click="formOpen ? resetForm() : (formOpen = true)">
        {{ formOpen ? '取消' : '新增地址' }}
      </button>
    </div>

    <form v-if="formOpen" class="nx-card addresses__form" @submit.prevent="submit">
      <div class="nx-card__body">
        <h2 class="nx-section-title">{{ editingId ? '编辑地址' : '新增地址' }}</h2>
        <div class="addresses__grid">
          <label>
            <span>收货人</span>
            <input v-model="form.receiver_name" required maxlength="40" />
          </label>
          <label>
            <span>手机号</span>
            <input v-model="form.receiver_phone" required maxlength="20" />
          </label>
          <label>
            <span>省份</span>
            <input v-model="form.province" required maxlength="30" />
          </label>
          <label>
            <span>城市</span>
            <input v-model="form.city" required maxlength="30" />
          </label>
          <label>
            <span>区县</span>
            <input v-model="form.district" required maxlength="30" />
          </label>
          <label>
            <span>邮编</span>
            <input v-model="form.postal_code" maxlength="10" />
          </label>
          <label class="addresses__full">
            <span>详细地址</span>
            <input v-model="form.detail" required maxlength="120" />
          </label>
          <label class="addresses__checkbox">
            <input v-model="form.is_default" type="checkbox" />
            <span>设为默认地址</span>
          </label>
        </div>
        <button type="submit" class="nx-btn nx-btn--primary" :disabled="busy">保存</button>
      </div>
    </form>

    <StateView
      :state="status"
      :error="error"
      :title="status === 'empty' ? '还没有收货地址' : undefined"
      :description="status === 'empty' ? '添加一个地址后即可下单。' : undefined"
      @retry="execute()"
    >
      <div class="addresses__list">
        <article v-for="address in addresses" :key="address.id" class="addresses__item nx-card">
          <div class="nx-card__body">
            <div class="addresses__item-head">
              <strong>{{ address.receiver_name }}</strong>
              <span class="nx-muted">{{ address.receiver_phone }}</span>
              <span v-if="address.is_default" class="addresses__default">默认</span>
            </div>
            <p class="addresses__detail">
              {{ address.province }}{{ address.city }}{{ address.district }}{{ address.detail }}
            </p>
            <div class="addresses__actions">
              <button type="button" class="nx-btn nx-btn--ghost" :disabled="busy" @click="startEdit(address.id)">
                编辑
              </button>
              <button
                v-if="!address.is_default"
                type="button"
                class="nx-btn nx-btn--ghost"
                :disabled="busy"
                @click="setDefault(address.id)"
              >
                设为默认
              </button>
              <button type="button" class="nx-btn nx-btn--ghost" :disabled="busy" @click="remove(address.id)">
                删除
              </button>
            </div>
          </div>
        </article>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.addresses {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
  }

  &__form {
    margin-bottom: 18px;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
    margin-bottom: 16px;

    label {
      display: flex;
      flex-direction: column;
      gap: 5px;
      font-size: 13px;
    }

    input[type='text'],
    input:not([type]) {
      height: 34px;
      padding: 0 10px;
      border: 1px solid var(--nx-border-strong);
      border-radius: var(--nx-radius-control);
      background: var(--nx-surface);
      color: var(--nx-text);
      font-family: inherit;
      font-size: 13px;
    }

    /* Text-ish inputs share one rule; checkbox is handled separately below. */
    input:not([type='checkbox']) {
      height: 34px;
      padding: 0 10px;
      border: 1px solid var(--nx-border-strong);
      border-radius: var(--nx-radius-control);
      background: var(--nx-surface);
      color: var(--nx-text);
      font-family: inherit;
      font-size: 13px;
    }
  }

  &__full {
    grid-column: 1 / -1;
  }

  &__checkbox {
    flex-direction: row !important;
    align-items: center;
    gap: 8px !important;
  }

  &__list {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 14px;
  }

  &__item-head {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 14px;
  }

  &__default {
    padding: 1px 8px;
    border-radius: var(--nx-radius-pill);
    background: var(--nx-primary);
    color: #fff;
    font-size: 11px;
  }

  &__detail {
    margin: 8px 0 12px;
    font-size: 13px;
    color: var(--nx-text-secondary);
  }

  &__actions {
    display: flex;
    gap: 4px;
  }
}
</style>
