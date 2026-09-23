/**
 * Filter / query-param builder tests (products + orders + after-sales + inventory).
 *
 * The task asked for coverage of at least two list pages; these cover five, because the
 * failure modes are identical and cheap to pin down:
 *   * an empty select must become ABSENT, not `status=` (a validation error server-side),
 *   * `page` must stay >= 1 and `page_size` must be capped,
 *   * free text is trimmed, and whitespace-only input counts as absent,
 *   * unknown/empty values are dropped rather than forwarded.
 */

import { describe, expect, it } from 'vitest'
import {
  DEFAULT_PAGE_SIZE,
  MAX_PAGE_SIZE,
  buildAfterSaleListParams,
  buildAnalyticsParams,
  buildInventoryListParams,
  buildKnowledgeDocParams,
  buildOrderListParams,
  buildProductListParams,
  normalizePaging,
  optionalText,
} from '@/domain/listParams'

describe('normalizePaging', () => {
  it('defaults to page 1 with the default page size', () => {
    expect(normalizePaging()).toEqual({ page: 1, page_size: DEFAULT_PAGE_SIZE })
  })

  it('clamps page to at least 1 (a stale filter can leave page at 0 or negative)', () => {
    expect(normalizePaging({ page: 0 }).page).toBe(1)
    expect(normalizePaging({ page: -5 }).page).toBe(1)
  })

  it('caps page_size so a hand-edited URL cannot request everything', () => {
    expect(normalizePaging({ page_size: 100000 }).page_size).toBe(MAX_PAGE_SIZE)
    expect(normalizePaging({ page_size: 0 }).page_size).toBe(1)
  })

  it('truncates fractional paging values instead of forwarding them', () => {
    expect(normalizePaging({ page: 2.9, page_size: 10.7 })).toEqual({ page: 2, page_size: 10 })
  })

  it('falls back to defaults for non-numeric input', () => {
    expect(normalizePaging({ page: Number.NaN })).toEqual({ page: 1, page_size: DEFAULT_PAGE_SIZE })
  })
})

describe('optionalText', () => {
  it('trims and drops empty or whitespace-only input', () => {
    expect(optionalText('  abc ')).toBe('abc')
    expect(optionalText('')).toBeUndefined()
    expect(optionalText('   ')).toBeUndefined()
    expect(optionalText(null)).toBeUndefined()
    expect(optionalText(undefined)).toBeUndefined()
  })
})

describe('buildProductListParams', () => {
  it('omits the status key entirely when "全部" is selected', () => {
    const params = buildProductListParams({ keyword: '', status: '', page: 1 })
    // The critical assertion: `status` must be ABSENT, not an empty string.
    expect('status' in params).toBe(false)
    expect('keyword' in params).toBe(false)
  })

  it('forwards a chosen frozen status verbatim', () => {
    for (const status of ['DRAFT', 'PUBLISHED', 'UNPUBLISHED', 'ARCHIVED'] as const) {
      expect(buildProductListParams({ keyword: '', status, page: 1 }).status).toBe(status)
    }
  })

  it('trims the keyword and keeps paging', () => {
    const params = buildProductListParams({ keyword: '  蓝牙耳机 ', status: 'PUBLISHED', page: 3 })
    expect(params).toEqual({ page: 3, page_size: DEFAULT_PAGE_SIZE, keyword: '蓝牙耳机', status: 'PUBLISHED' })
  })

  it('treats a whitespace-only keyword as no filter', () => {
    expect('keyword' in buildProductListParams({ keyword: '   ', status: '', page: 1 })).toBe(false)
  })
})

describe('buildOrderListParams', () => {
  const base = {
    orderNo: '',
    status: '' as const,
    paymentStatus: '' as const,
    fulfillmentStatus: '' as const,
    startDate: '',
    endDate: '',
    page: 1,
  }

  it('sends only paging when no filter is set', () => {
    expect(buildOrderListParams(base)).toEqual({ page: 1, page_size: DEFAULT_PAGE_SIZE })
  })

  it('maps the order-number box onto the backend keyword parameter', () => {
    const params = buildOrderListParams({ ...base, orderNo: 'NX20250101' })
    expect(params.keyword).toBe('NX20250101')
    // The console's box is named orderNo, but the wire parameter is `keyword`.
    expect('order_no' in params).toBe(false)
  })

  it('forwards all three frozen status filters independently', () => {
    const params = buildOrderListParams({
      ...base,
      status: 'PROCESSING',
      paymentStatus: 'PAID',
      fulfillmentStatus: 'PARTIAL_SHIPPED',
    })
    expect(params.status).toBe('PROCESSING')
    expect(params.payment_status).toBe('PAID')
    expect(params.fulfillment_status).toBe('PARTIAL_SHIPPED')
  })

  it('forwards a date window and omits empty ends', () => {
    expect(
      buildOrderListParams({ ...base, startDate: '2025-01-01', endDate: '2025-01-31' }),
    ).toMatchObject({ start_date: '2025-01-01', end_date: '2025-01-31' })
    const onlyStart = buildOrderListParams({ ...base, startDate: '2025-01-01' })
    expect(onlyStart.start_date).toBe('2025-01-01')
    expect('end_date' in onlyStart).toBe(false)
  })

  it('accepts every frozen OrderStatus as a filter value', () => {
    for (const status of ['PENDING_PAYMENT', 'PROCESSING', 'COMPLETED', 'CANCELLED', 'CLOSED'] as const) {
      expect(buildOrderListParams({ ...base, status }).status).toBe(status)
    }
  })
})

describe('buildAfterSaleListParams', () => {
  it('omits empty filters and maps the number box onto keyword', () => {
    expect(buildAfterSaleListParams({ afterSaleNo: '', status: '', page: 1 })).toEqual({
      page: 1,
      page_size: DEFAULT_PAGE_SIZE,
    })
    expect(buildAfterSaleListParams({ afterSaleNo: 'AS001', status: '', page: 1 }).keyword).toBe('AS001')
  })

  it('forwards the frozen after-sale statuses', () => {
    for (const status of ['PROCESSING', 'PARTIAL_REFUNDED', 'REFUNDED'] as const) {
      expect(buildAfterSaleListParams({ afterSaleNo: '', status, page: 1 }).status).toBe(status)
    }
  })
})

describe('buildInventoryListParams', () => {
  it('sends low_stock_only only when the toggle is ON', () => {
    expect('low_stock_only' in buildInventoryListParams({ keyword: '', productId: '', lowStockOnly: false, page: 1 })).toBe(
      false,
    )
    expect(
      buildInventoryListParams({ keyword: '', productId: '', lowStockOnly: true, page: 1 }).low_stock_only,
    ).toBe(true)
  })

  it('maps the product filter onto product_id and the keyword separately', () => {
    const params = buildInventoryListParams({
      keyword: 'NX-1',
      productId: 'prod-9',
      lowStockOnly: false,
      page: 2,
    })
    expect(params).toMatchObject({ product_id: 'prod-9', keyword: 'NX-1', page: 2 })
  })
})

describe('buildKnowledgeDocParams + buildAnalyticsParams', () => {
  it('builds paging plus an optional keyword for documents', () => {
    expect(buildKnowledgeDocParams({ knowledgeBaseId: 'kb-1', keyword: '', page: 1 })).toEqual({
      page: 1,
      page_size: DEFAULT_PAGE_SIZE,
    })
    expect(
      buildKnowledgeDocParams({ knowledgeBaseId: 'kb-1', keyword: ' 售后 ', page: 2 }).keyword,
    ).toBe('售后')
  })

  it('always sends granularity, and dates only when set', () => {
    expect(buildAnalyticsParams({ startDate: '', endDate: '', granularity: 'day' })).toEqual({
      granularity: 'day',
    })
    expect(
      buildAnalyticsParams({ startDate: '2025-01-01', endDate: '', granularity: 'week' }),
    ).toEqual({ granularity: 'week', start_date: '2025-01-01' })
  })
})
