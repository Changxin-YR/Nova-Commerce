/**
 * Error mapper (§106, §109).
 *
 * Turns *anything* axios can throw into one stable `NormalizedApiError` shape that
 * the UI can render without another `try/catch` layer per component.
 *
 * Rules:
 *  1. The backend envelope is authoritative: `{code, message, data, trace_id}`.
 *     `message` is already safe to display (§109 forbids leaking internals).
 *  2. Transport failures get SYNTHETIC NEGATIVE codes so they can never be
 *     confused with a real business code (all of which are >= 0).
 *  3. A 5xx message is never trusted verbatim — a proxy or a misconfigured gateway
 *     may return HTML or a stack trace. Those are replaced with a neutral message;
 *     the real payload stays in `raw` for logging only.
 *  4. `unauthorized` is only true for genuine session failures. A 403 must NOT be
 *     treated as "please log in again", or the UI will bounce the user to /login
 *     while they still have a valid token.
 */

import axios from 'axios'
import {
  ErrorCode,
  TRANSPORT_CODES,
  type ApiEnvelope,
  type NormalizedApiError,
} from '@/types/api'
import { FORBIDDEN_CODES, REFRESHABLE_CODES } from '@/config/constants'

/** Human strings keyed by business code. Falls back to the server message. */
const CODE_MESSAGES: Record<number, string> = {
  [ErrorCode.INTERNAL_ERROR]: '服务内部错误，请稍后重试',
  [ErrorCode.VALIDATION_ERROR]: '请求参数不合法',
  [ErrorCode.NOT_FOUND]: '资源不存在',
  [ErrorCode.CONFLICT]: '数据状态冲突，请刷新后重试',
  [ErrorCode.RATE_LIMITED]: '请求过于频繁，请稍后重试',
  [ErrorCode.SERVICE_UNAVAILABLE]: '服务暂时不可用，请稍后重试',
  [ErrorCode.DEPENDENCY_UNAVAILABLE]: '依赖服务不可用，请稍后重试',
  [ErrorCode.IDEMPOTENCY_KEY_REQUIRED]: '缺少幂等键，无法执行该写操作',
  [ErrorCode.IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD]: '幂等键被复用于不同的请求内容',
  [ErrorCode.IDEMPOTENCY_REQUEST_IN_PROGRESS]: '相同请求正在处理中，请勿重复提交',

  [ErrorCode.UNAUTHENTICATED]: '请先登录',
  [ErrorCode.INVALID_CREDENTIALS]: '账号或密码错误',
  [ErrorCode.TOKEN_EXPIRED]: '登录已过期，请重新登录',
  [ErrorCode.TOKEN_INVALID]: '登录凭证无效，请重新登录',
  [ErrorCode.REFRESH_TOKEN_REUSED]: '登录凭证已被使用，出于安全考虑需要重新登录',
  [ErrorCode.SESSION_REVOKED]: '会话已失效，请重新登录',
  [ErrorCode.ACCOUNT_LOCKED]: '账号已被锁定，请联系管理员',
  [ErrorCode.ACCOUNT_DISABLED]: '账号已被禁用，请联系管理员',
  [ErrorCode.FORBIDDEN]: '没有权限执行该操作',
  [ErrorCode.INSUFFICIENT_PERMISSION]: '当前角色权限不足',
  [ErrorCode.DATA_SCOPE_VIOLATION]: '数据范围不足，无法访问该数据',
  [ErrorCode.MERCHANT_MISMATCH]: '该数据不属于当前商户',

  [ErrorCode.PRODUCT_NOT_FOUND]: '商品不存在或已下架',
  [ErrorCode.PRODUCT_NOT_PUBLISHED]: '商品未发布',
  [ErrorCode.SKU_NOT_AVAILABLE]: '该规格暂不可售',
  [ErrorCode.IMAGE_UPLOAD_REJECTED]: '图片不合规，已拒绝上传',

  [ErrorCode.INSUFFICIENT_STOCK]: '库存不足',
  [ErrorCode.INVENTORY_CONFLICT_STALE_VERSION]: '库存已被其他人修改，请刷新后重试',

  [ErrorCode.CART_EMPTY]: '购物车为空',
  [ErrorCode.ORDER_NOT_FOUND]: '订单不存在',
  [ErrorCode.ORDER_STATE_INVALID]: '当前订单状态不允许该操作',
  [ErrorCode.ORDER_ALREADY_EXPIRED]: '订单已超时关闭',
  [ErrorCode.ORDER_AMOUNT_MISMATCH]: '订单金额校验不通过，请重新下单',
  [ErrorCode.PRICE_CHANGED]: '商品价格已变化，请确认后重新下单',
  [ErrorCode.ORDER_NOT_CANCELLABLE]: '该订单当前不可取消',
  [ErrorCode.ORDER_NOT_CONFIRMABLE]: '该订单当前不可确认收货',

  [ErrorCode.PAYMENT_ALREADY_PAID]: '订单已支付，请勿重复支付',
  [ErrorCode.PAYMENT_AMOUNT_MISMATCH]: '支付金额与订单金额不一致',
  [ErrorCode.PAYMENT_MOCK_DISABLED]: '模拟支付通道未开启',

  [ErrorCode.FULFILLMENT_QUANTITY_EXCEEDS_ORDER]: '发货数量超过订单数量',
  [ErrorCode.FULFILLMENT_ALREADY_SHIPPED]: '该订单已发货',

  [ErrorCode.AFTER_SALE_NOT_ELIGIBLE]: '该订单不符合售后条件',
  [ErrorCode.AFTER_SALE_STATE_INVALID]: '当前售后单状态不允许该操作',
  [ErrorCode.REFUND_EXCEEDS_PAID_AMOUNT]: '退款金额超过实付金额',
  [ErrorCode.REFUND_EXCEEDS_ITEM_AMOUNT]: '退款金额超过该商品可退金额',
  [ErrorCode.REFUND_AMOUNT_INVALID]: '退款金额不合法',
  [ErrorCode.REFUND_ALREADY_COMPLETED]: '退款已完成，请勿重复退款',

  [ErrorCode.COUPON_NOT_APPLICABLE]: '优惠券不适用于当前商品',
  [ErrorCode.COUPON_EXPIRED]: '优惠券已过期',
  [ErrorCode.COUPON_ALREADY_USED]: '优惠券已被使用',
  [ErrorCode.COUPON_ALREADY_LOCKED]: '优惠券已被其他订单占用',
  [ErrorCode.COUPON_THRESHOLD_NOT_MET]: '未达到优惠券使用门槛',

  [ErrorCode.DOCUMENT_PARSE_FAILED]: '文档解析失败',
  [ErrorCode.DOCUMENT_UNSUPPORTED_TYPE]: '不支持的文档类型',
  [ErrorCode.DOCUMENT_TOO_LARGE]: '文档超过大小限制',
  [ErrorCode.INSUFFICIENT_EVIDENCE]: '知识库中没有足够证据回答该问题',

  [ErrorCode.AGENT_BUDGET_EXCEEDED]: '本次任务超出预算上限，已终止',
  [ErrorCode.AGENT_ACTION_REQUIRES_APPROVAL]: '该操作需要人工审批',
  [ErrorCode.AGENT_ACTION_BLOCKED_BY_RISK]: '该操作被风控规则拦截',
  [ErrorCode.AGENT_PROMPT_INJECTION_DETECTED]: '检测到提示注入，已阻断',
  [ErrorCode.AGENT_REVALIDATION_FAILED]: '操作前的校验失败，已阻止执行',

  [ErrorCode.PENDING_ACTION_EXPIRED]: '审批请求已过期',
  [ErrorCode.PENDING_ACTION_ALREADY_DECIDED]: '该审批已处理',
  [ErrorCode.PENDING_ACTION_PAYLOAD_CHANGED]: '审批内容已变化，请重新确认',
}

const TRANSPORT_MESSAGES: Record<number, string> = {
  [TRANSPORT_CODES.NETWORK_ERROR]: '网络连接失败，请检查网络后重试',
  [TRANSPORT_CODES.TIMEOUT]: '请求超时，请稍后重试',
  [TRANSPORT_CODES.CANCELLED]: '请求已取消',
  [TRANSPORT_CODES.MALFORMED_RESPONSE]: '服务返回格式异常，请联系技术支持',
  [TRANSPORT_CODES.UNKNOWN]: '请求失败，请稍后重试',
  [TRANSPORT_CODES.SESSION_EXPIRED]: '登录已过期，请重新登录',
}

/**
 * User-facing message for a business code, or `''` when this code has no curated
 * text (the caller then falls back to the server's own message).
 */
export function messageForCode(code: number): string {
  return CODE_MESSAGES[code] ?? ''
}

export function messageForTransportCode(code: number): string {
  return TRANSPORT_MESSAGES[code] ?? TRANSPORT_MESSAGES[TRANSPORT_CODES.UNKNOWN] ?? ''
}

export function isForbiddenCode(code: number): boolean {
  return FORBIDDEN_CODES.includes(code) || code === ErrorCode.FORBIDDEN
}

export function isUnauthenticatedCode(code: number): boolean {
  return code === ErrorCode.UNAUTHENTICATED
    || code === ErrorCode.TOKEN_EXPIRED
    || code === ErrorCode.TOKEN_INVALID
    || code === ErrorCode.SESSION_REVOKED
    || code === ErrorCode.REFRESH_TOKEN_REUSED
    || code === ErrorCode.ACCOUNT_DISABLED
}

export function isRefreshableCode(code: number): boolean {
  return REFRESHABLE_CODES.includes(code)
}

/** Guards the envelope shape. `data` stays `unknown`; callers narrow it. */
export function isApiEnvelope(value: unknown): value is ApiEnvelope<unknown> {
  if (!value || typeof value !== 'object') return false
  const v = value as Record<string, unknown>
  return typeof v.code === 'number' && typeof v.message === 'string' && 'data' in v
}

/** HTTP statuses that are worth retrying. */
function isRetryableStatus(status: number): boolean {
  return status === 408 || status === 425 || status === 429 || status >= 500
}

function build(partial: NormalizedApiError): NormalizedApiError {
  return partial
}

/**
 * Map an unknown thrown value into `NormalizedApiError`.
 *
 * @param error  anything caught from the HTTP client
 * @param traceId trace id captured by the interceptor (envelope or header)
 */
export function normalizeError(error: unknown, traceId = ''): NormalizedApiError {
  if (isAlreadyNormalized(error)) return error

  if (axios.isCancel(error)) {
    return build({
      code: TRANSPORT_CODES.CANCELLED,
      message: messageForTransportCode(TRANSPORT_CODES.CANCELLED),
      traceId,
      httpStatus: 0,
      retryable: false,
      forbidden: false,
      unauthenticated: false,
      raw: error,
    })
  }

  if (axios.isAxiosError(error)) {
    const response = error.response
    const envelope = isApiEnvelope(response?.data) ? response.data : undefined
    const trace = envelope?.trace_id || traceId
    const status = response?.status ?? 0

    if (!response) {
      const isTimeout = error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT'
      const code = isTimeout ? TRANSPORT_CODES.TIMEOUT : TRANSPORT_CODES.NETWORK_ERROR
      return build({
        code,
        message: messageForTransportCode(code),
        traceId: trace,
        httpStatus: 0,
        retryable: true,
        forbidden: false,
        unauthenticated: false,
        raw: error,
      })
    }

    if (envelope) {
      const businessCode = envelope.code
      const curated = messageForCode(businessCode)
      // 5xx envelopes may be produced by infrastructure, not by `AppError`.
      const serverMessage = status >= 500 ? '' : envelope.message
      const message =
        curated ||
        serverMessage ||
        (status >= 500
          ? '服务器异常，请稍后重试'
          : messageForTransportCode(TRANSPORT_CODES.MALFORMED_RESPONSE))

      return build({
        code: businessCode,
        message,
        traceId: trace,
        httpStatus: status,
        retryable: businessCode === ErrorCode.RATE_LIMITED || isRetryableStatus(status),
        forbidden: isForbiddenCode(businessCode) || status === 403,
        unauthenticated: isUnauthenticatedCode(businessCode) || status === 401,
        raw: error,
      })
    }

    // No envelope: the response came from something other than the API
    // (gateway HTML, empty 502, ...). Never surface the body to the user.
    const fallbackCode =
      status === 401
        ? ErrorCode.UNAUTHENTICATED
        : status === 403
          ? ErrorCode.FORBIDDEN
          : status === 404
            ? ErrorCode.NOT_FOUND
            : status === 429
              ? ErrorCode.RATE_LIMITED
              : status >= 500
                ? ErrorCode.SERVICE_UNAVAILABLE
                : TRANSPORT_CODES.MALFORMED_RESPONSE

    return build({
      code: fallbackCode,
      message:
        messageForCode(fallbackCode) ||
        messageForTransportCode(TRANSPORT_CODES.MALFORMED_RESPONSE),
      traceId: trace,
      httpStatus: status,
      retryable: isRetryableStatus(status),
      forbidden: status === 403,
      unauthenticated: status === 401,
      raw: error,
    })
  }

  if (error instanceof Error && error.message) {
    return build({
      code: TRANSPORT_CODES.UNKNOWN,
      message: error.message,
      traceId,
      httpStatus: 0,
      retryable: false,
      forbidden: false,
      unauthenticated: false,
      raw: error,
    })
  }

  return build({
    code: TRANSPORT_CODES.UNKNOWN,
    message: messageForTransportCode(TRANSPORT_CODES.UNKNOWN),
    traceId,
    httpStatus: 0,
    retryable: false,
    forbidden: false,
    unauthenticated: false,
    raw: error,
  })
}

function isAlreadyNormalized(value: unknown): value is NormalizedApiError {
  if (!value || typeof value !== 'object') return false
  const v = value as Record<string, unknown>
  return (
    typeof v.code === 'number' &&
    typeof v.message === 'string' &&
    typeof v.retryable === 'boolean' &&
    typeof v.forbidden === 'boolean' &&
    typeof v.unauthenticated === 'boolean'
  )
}

/**
 * This module is a PURE mapper: it never mutates tokens, never redirects and never
 * touches the store. Session teardown belongs to the interceptor in
 * `src/api/client.ts`, which must first attempt a refresh — clearing the session
 * here would make the refresh impossible to complete.
 */
export const __errorMapperIsPure = true
