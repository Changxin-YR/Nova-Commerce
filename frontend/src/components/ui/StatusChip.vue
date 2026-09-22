<script setup lang="ts">
/**
 * Status chip for the frozen enums (§105).
 *
 * The mapping table is exhaustive over each union, so adding a status to the frozen
 * enum breaks the typecheck here rather than silently rendering a blank chip. Unknown
 * values are shown verbatim in a neutral chip instead of being hidden — a support
 * engineer needs to see the odd value, not a silent gap.
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

const props = defineProps<{ status: StatusValue; kind?: 'order' | 'payment' | 'fulfillment' | 'aftersale' | 'doc' | 'action' | 'risk' }>()

type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'primary'

const ORDER_TONES: Record<OrderStatus, Tone> = {
  PENDING_PAYMENT: 'warning',
  PROCESSING: 'primary',
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
  SHIPPED: 'primary',
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
  EXECUTING: 'primary',
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
  NONE: '无',
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

  // No `kind` given: try every table, then fall back to neutral.
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
  <span class="nx-status" :class="`nx-status--${tone}`">
    <span class="nx-status__dot" aria-hidden="true" />
    {{ label }}
  </span>
</template>

<style scoped lang="scss">
.nx-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 9px;
  border-radius: var(--nx-radius-pill);
  font-size: 12px;
  line-height: 18px;
  white-space: nowrap;
  background: var(--nx-surface-sunken);
  color: var(--nx-text-secondary);

  &__dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
    opacity: 0.75;
  }

  &--neutral {
    background: var(--nx-surface-sunken);
    color: var(--nx-text-muted);
  }

  &--info {
    background: var(--nx-info-soft);
    color: var(--nx-info);
  }

  &--primary {
    background: var(--nx-primary-soft);
    color: var(--nx-primary);
  }

  &--success {
    background: var(--nx-success-soft);
    color: var(--nx-success);
  }

  &--warning {
    background: var(--nx-warning-soft);
    color: var(--nx-warning);
  }

  &--danger {
    background: var(--nx-danger-soft);
    color: var(--nx-danger);
  }
}
</style>
