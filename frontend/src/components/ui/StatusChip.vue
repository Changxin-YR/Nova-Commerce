<script setup lang="ts">
/**
 * Status chip for the frozen state enums (§105).
 *
 * Restyled for the dense look: 2px radius, 12px type, 1px border, colour carries the
 * meaning (green = done, orange = waiting on the user, red = failed, grey = inert).
 *
 * The tone tables are exhaustive over each union, so adding a status to a frozen enum
 * breaks the typecheck HERE rather than silently rendering a blank chip. An unknown value
 * is shown verbatim in a neutral chip — a support engineer needs to see the odd value.
 */
import { computed } from 'vue'
import type {
  AfterSaleStatus,
  FulfillmentStatus,
  KnowledgeDocStatus,
  OrderStatus,
  PaymentStatus,
  PendingActionStatus,
  RiskLevel,
} from '@/types/domain'

type StatusValue =
  | OrderStatus
  | PaymentStatus
  | FulfillmentStatus
  | AfterSaleStatus
  | KnowledgeDocStatus
  | PendingActionStatus
  | RiskLevel
  | string

const props = defineProps<{
  status: StatusValue
  kind?: 'order' | 'payment' | 'fulfillment' | 'aftersale' | 'doc' | 'action' | 'risk'
  /** Prefix a dot marker (used in dense console tables). */
  dot?: boolean
}>()

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'brand'

const ORDER_TONES: Record<OrderStatus, Tone> = {
  PENDING_PAYMENT: 'warning',
  PROCESSING: 'brand',
  COMPLETED: 'success',
  CANCELLED: 'neutral',
  CLOSED: 'neutral',
}

const PAYMENT_TONES: Record<PaymentStatus, Tone> = {
  UNPAID: 'neutral',
  PAYING: 'warning',
  PAID: 'success',
  PARTIAL_REFUNDED: 'info',
  REFUNDED: 'info',
}

const FULFILLMENT_TONES: Record<FulfillmentStatus, Tone> = {
  UNFULFILLED: 'neutral',
  PARTIAL_SHIPPED: 'info',
  SHIPPED: 'brand',
  DELIVERED: 'success',
}

const AFTER_SALE_TONES: Record<AfterSaleStatus, Tone> = {
  NONE: 'neutral',
  PROCESSING: 'warning',
  PARTIAL_REFUNDED: 'info',
  REFUNDED: 'info',
}

const DOC_TONES: Record<KnowledgeDocStatus, Tone> = {
  UPLOADED: 'neutral',
  PROCESSING: 'warning',
  READY: 'success',
  FAILED: 'danger',
  ARCHIVED: 'neutral',
}

const ACTION_TONES: Record<PendingActionStatus, Tone> = {
  PENDING: 'warning',
  APPROVED: 'info',
  REJECTED: 'neutral',
  EXECUTING: 'brand',
  SUCCEEDED: 'success',
  FAILED: 'danger',
  EXPIRED: 'neutral',
}

const RISK_TONES: Record<RiskLevel, Tone> = {
  READ: 'neutral',
  LOW: 'success',
  MEDIUM: 'warning',
  HIGH: 'danger',
  CRITICAL: 'danger',
}

/** Chinese labels for the frozen enums. The enum VALUE is the contract, not the label. */
const LABELS: Record<string, string> = {
  PENDING_PAYMENT: '待付款',
  PROCESSING: '处理中',
  COMPLETED: '已完成',
  CANCELLED: '已取消',
  CLOSED: '已关闭',
  UNPAID: '未支付',
  PAYING: '支付中',
  PAID: '已支付',
  PARTIAL_REFUNDED: '部分退款',
  REFUNDED: '已退款',
  UNFULFILLED: '未发货',
  PARTIAL_SHIPPED: '部分发货',
  SHIPPED: '已发货',
  DELIVERED: '已签收',
  NONE: '无售后',
  UPLOADED: '已上传',
  READY: '已就绪',
  FAILED: '失败',
  ARCHIVED: '已归档',
  PENDING: '待审批',
  APPROVED: '已通过',
  REJECTED: '已拒绝',
  EXECUTING: '执行中',
  SUCCEEDED: '已成功',
  EXPIRED: '已过期',
  READ: '只读',
  LOW: '低风险',
  MEDIUM: '中风险',
  HIGH: '高风险',
  CRITICAL: '极高风险',
}

function toneFor(value: string): Tone {
  const kind = props.kind
  if (kind === 'order') return ORDER_TONES[value as OrderStatus] ?? 'neutral'
  if (kind === 'payment') return PAYMENT_TONES[value as PaymentStatus] ?? 'neutral'
  if (kind === 'fulfillment') return FULFILLMENT_TONES[value as FulfillmentStatus] ?? 'neutral'
  if (kind === 'aftersale') return AFTER_SALE_TONES[value as AfterSaleStatus] ?? 'neutral'
  if (kind === 'doc') return DOC_TONES[value as KnowledgeDocStatus] ?? 'neutral'
  if (kind === 'action') return ACTION_TONES[value as PendingActionStatus] ?? 'neutral'
  if (kind === 'risk') return RISK_TONES[value as RiskLevel] ?? 'neutral'

  return (
    ORDER_TONES[value as OrderStatus] ??
    PAYMENT_TONES[value as PaymentStatus] ??
    ACTION_TONES[value as PendingActionStatus] ??
    RISK_TONES[value as RiskLevel] ??
    'neutral'
  )
}

const tone = computed(() => toneFor(String(props.status)))
const label = computed(() => LABELS[String(props.status)] ?? String(props.status))
</script>

<template>
  <span class="chip" :class="`chip--${tone}`">
    <span v-if="dot" class="chip__dot" aria-hidden="true" />
    {{ label }}
  </span>
</template>

<style scoped lang="scss">
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 18px;
  padding: 0 5px;
  border: 1px solid currentColor;
  border-radius: var(--nx-radius);
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;

  &__dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: currentColor;
  }

  &--neutral {
    color: var(--nx-text-secondary);
    background: var(--nx-surface-sunken);
  }

  &--info {
    color: var(--nx-info);
    background: var(--nx-info-soft);
  }

  &--brand {
    color: var(--nx-brand);
    background: var(--nx-brand-soft);
  }

  &--success {
    color: var(--nx-success);
    background: var(--nx-success-soft);
  }

  &--warning {
    color: var(--nx-warning);
    background: var(--nx-warning-soft);
  }

  &--danger {
    color: var(--nx-danger);
    background: var(--nx-danger-soft);
  }
}
</style>
