<script setup lang="ts">
/**
 * `<PriceText>` — the ONE price renderer for the whole app.
 *
 * RENDER RULE (commerce convention: the integer dominates, the currency symbol and the
 * cents recede):
 *
 *     ¥ 2,999 .00
 *     ↑   ↑     ↑
 *     |   |     └── small, muted-ish red  (.00)
 *     |   └──────── large + bold          (2,999)
 *     └──────────── small                 (¥)
 *
 * WHY IT EXISTS: money is integer minor units end to end (BIGINT cents on the server, §
 * money rule). Conversion to major units happens in exactly ONE place — `utils/money.ts`
 * (`splitMoney`) — and this component is the only UI surface that consumes it. Business
 * components pass `299900` and never write `/ 100`, `toFixed()` or a `¥` literal, so
 * rounding and formatting cannot drift between pages.
 *
 * The rendered value is presentational only: the authoritative amount is always the
 * server's. Never compute a total in a template and pass it here.
 */
import { computed } from 'vue'
import { splitMoney } from '@/utils/money'

const props = withDefaults(
  defineProps<{
    /** Integer minor units (cents). Required — there is no float overload. */
    amount: number
    /** Visual scale. `md` is the default card price. */
    size?: 'sm' | 'md' | 'lg' | 'xl'
    /** Render the cents. Off for dense lists where alignment matters more. */
    showDecimal?: boolean
    /** Thousands separators. Off inside narrow table cells. */
    grouping?: boolean
    /** Struck-through list price shown after the current price. */
    originalAmount?: number
    /** Grey instead of brand red, for de-emphasised figures (e.g. subtotals in a table). */
    muted?: boolean
    /** Hide the currency symbol (used when a column header already says "金额"). */
    noSymbol?: boolean
  }>(),
  {
    size: 'md',
    showDecimal: true,
    grouping: true,
    originalAmount: undefined,
    muted: false,
    noSymbol: false,
  },
)

const parts = computed(() => splitMoney(props.amount, { grouping: props.grouping }))
/** The struck-through list price keeps the same convention, at a smaller scale. */
const originalParts = computed(() =>
  props.originalAmount === undefined ? null : splitMoney(props.originalAmount, { grouping: props.grouping }),
)
const showOriginal = computed(
  () => props.originalAmount !== undefined && props.originalAmount > props.amount,
)
</script>

<template>
  <span class="price" :class="[`price--${size}`, { 'price--muted': muted }]">
    <span v-if="!noSymbol" class="price__symbol" aria-hidden="true">¥</span>
    <span class="price__integer">{{ parts.sign }}{{ parts.integer }}</span>
    <span v-if="showDecimal" class="price__decimal">.{{ parts.decimal }}</span>

    <span v-if="showOriginal && originalParts" class="price__original">
      <span aria-hidden="true">¥</span>{{ originalParts.sign }}{{ originalParts.integer }}.{{ originalParts.decimal }}
    </span>

    <!--
      Screen readers get one unambiguous sentence instead of "yen two comma nine nine
      nine dot zero zero", which reads as nonsense.
    -->
    <span class="price__sr">
      价格 {{ parts.sign }}{{ parts.integer }}.{{ showDecimal ? parts.decimal : '00' }} 元
      <template v-if="showOriginal && originalParts">
        ，原价 {{ originalParts.integer }}.{{ originalParts.decimal }} 元
      </template>
    </span>
  </span>
</template>

<style scoped lang="scss">
.price {
  display: inline-flex;
  align-items: baseline;
  color: var(--nx-price);
  font-family: var(--nx-font-sans);
  white-space: nowrap;

  /* Integer digits align vertically when prices are stacked in a list. */
  &__integer {
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  &__symbol,
  &__decimal {
    font-weight: 400;
    font-variant-numeric: tabular-nums;
  }

  &__original {
    margin-left: 6px;
    color: var(--nx-text-muted);
    font-weight: 400;
    font-size: 12px;
    text-decoration: line-through;
  }

  /* Visually hidden, kept for assistive tech. */
  &__sr {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  &--muted {
    color: var(--nx-text);
  }

  /* -- scale ramp: 12 / 16 / 22 / 30 px integers ------------------------- */
  &--sm {
    .price__integer {
      font-size: 14px;
    }
    .price__symbol,
    .price__decimal {
      font-size: 12px;
    }
    .price__original {
      font-size: 12px;
    }
  }

  &--md {
    .price__integer {
      font-size: 16px;
    }
    .price__symbol,
    .price__decimal {
      font-size: 12px;
    }
  }

  &--lg {
    .price__integer {
      font-size: 22px;
    }
    .price__symbol,
    .price__decimal {
      font-size: 14px;
    }
  }

  &--xl {
    .price__integer {
      font-size: 30px;
    }
    .price__symbol,
    .price__decimal {
      font-size: 16px;
    }
  }
}

.price--muted .price__original {
  color: var(--nx-text-muted);
}
</style>
