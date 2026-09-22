/**
 * Money helpers.
 *
 * MONEY IS INTEGER MINOR UNITS (cents) everywhere: in types, in the API, in stores
 * and in arithmetic. Conversion only happens here, at the render boundary.
 *
 * `formatMoney` never returns a float-ish string by accident: it formats the minor
 * units with integer arithmetic (`Math.trunc` on the quotient) and pads the
 * remainder, so 1 cent renders as `¥0.01` and never as `¥0.009999999`.
 */

import type { MoneyAmount } from '@/types/domain'

/** Amounts per major unit. CNY has 100 fen per yuan. */
export const MINOR_UNITS_PER_MAJOR = 100

/** Dev-only guard so a float sneaking in from a mock is caught early. */
export function assertMoney(amount: number, label = 'amount'): void {
  if (!Number.isInteger(amount)) {
    throw new Error(`${label} must be an integer in minor units, received ${amount}`)
  }
}

/** 1234 -> "12.34". Integer-only arithmetic; never `amount / 100` for display. */
export function toMajorString(amount: MoneyAmount): string {
  const negative = amount < 0
  const abs = Math.abs(Math.trunc(amount))
  const major = Math.floor(abs / MINOR_UNITS_PER_MAJOR)
  const minor = abs % MINOR_UNITS_PER_MAJOR
  return `${negative ? '-' : ''}${major}.${minor.toString().padStart(2, '0')}`
}

export interface FormatMoneyOptions {
  /** Currency symbol. Defaults to `¥`. */
  symbol?: string
  /** Show the symbol. Defaults to true. */
  withSymbol?: boolean
  /**
   * Print the minor part (`¥12.34` vs `¥12`). Defaults to true.
   * When false the minor units are TRUNCATED, never rounded: a price the user is about
   * to pay must not appear higher than the amount the server will charge.
   */
  withMinor?: boolean
  /** Thousands separators. Defaults to true. */
  grouping?: boolean
}

/** Render an integer minor-unit amount as a display string. */
export function formatMoney(amount: MoneyAmount, options: FormatMoneyOptions = {}): string {
  const { symbol = '¥', withSymbol = true, withMinor = true, grouping = true } = options
  // NOTE the order: pad to two digits BEFORE slicing, otherwise "1.5" would become
  // "1.50" correctly but "1.005"-style input would slice the wrong side.
  const asString = toMajorString(amount)
  const [majorPart = '0', minorRaw = '00'] = asString.split('.')
  const minorPart = minorRaw.padStart(2, '0').slice(0, 2)
  const sign = majorPart.startsWith('-') ? '-' : ''
  const digits = majorPart.replace('-', '')
  const grouped = grouping ? digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',') : digits
  const body = withMinor ? `${grouped}.${minorPart}` : grouped
  return `${sign}${withSymbol ? symbol : ''}${body}`
}

/** "12.34" (user input, yuan) -> 1234 minor units. Returns null when invalid. */
export function fromMajorString(input: string): MoneyAmount | null {
  const trimmed = input.trim().replace(/[¥,\s]/g, '')
  if (!/^-?\d+(\.\d{0,2})?$/.test(trimmed)) return null
  const negative = trimmed.startsWith('-')
  const [major = '0', minor = ''] = trimmed.replace('-', '').split('.')
  const cents = Number(major) * MINOR_UNITS_PER_MAJOR + Number(minor.padEnd(2, '0') || '0')
  if (!Number.isFinite(cents)) return null
  return negative ? -cents : cents
}

/** Sum a list of minor-unit amounts. */
export function sumMoney(amounts: readonly MoneyAmount[]): MoneyAmount {
  return amounts.reduce((total, amount) => total + amount, 0)
}

/** Percentage of `part` within `total`, as an integer 0..100 (no float display). */
export function percentOf(part: MoneyAmount, total: MoneyAmount): number {
  if (total <= 0) return 0
  return Math.round((part / total) * 100)
}
