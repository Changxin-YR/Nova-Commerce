import { describe, expect, it } from 'vitest'
import {
  assertMoney,
  formatMoney,
  fromMajorString,
  percentOf,
  sumMoney,
  toMajorString,
} from '@/utils/money'

describe('money (integer minor units)', () => {
  it('renders cents without float artifacts', () => {
    expect(toMajorString(1)).toBe('0.01')
    expect(toMajorString(0)).toBe('0.00')
    expect(toMajorString(7)).toBe('0.07')
    expect(toMajorString(10)).toBe('0.10')
    expect(toMajorString(999)).toBe('9.99')
    expect(toMajorString(100000)).toBe('1000.00')
  })

  it('formats with symbol, grouping and sign', () => {
    expect(formatMoney(123456)).toBe('¥1,234.56')
    expect(formatMoney(123456, { grouping: false })).toBe('¥1234.56')
    // `withMinor: false` TRUNCATES rather than rounds: a checkout button must never
    // display an amount higher than what the server will charge.
    expect(formatMoney(123456, { withMinor: false })).toBe('¥1,234')
    expect(formatMoney(123499, { withMinor: false })).toBe('¥1,234')
    expect(formatMoney(123456, { withSymbol: false })).toBe('1,234.56')
    expect(formatMoney(-2500)).toBe('-¥25.00')
  })

  it('never produces a float artefact for awkward values', () => {
    for (const amount of [1, 3, 7, 29, 101, 999999999]) {
      expect(formatMoney(amount, { withSymbol: false })).toMatch(/^-?\d{1,3}(,\d{3})*\.\d{2}$/)
    }
  })

  it('parses user input in major units back to minor units', () => {
    expect(fromMajorString('12.34')).toBe(1234)
    expect(fromMajorString('12')).toBe(1200)
    expect(fromMajorString('¥1,234.56')).toBe(123456)
    expect(fromMajorString('0.5')).toBe(50)
    expect(fromMajorString('-3.05')).toBe(-305)
    expect(fromMajorString('')).toBeNull()
    expect(fromMajorString('abc')).toBeNull()
    expect(fromMajorString('1.234')).toBeNull()
  })

  it('round-trips through parse and format', () => {
    for (const amount of [1, 50, 199, 123456]) {
      expect(fromMajorString(formatMoney(amount, { withSymbol: false }))).toBe(amount)
    }
  })

  it('sums and computes percentages with integers', () => {
    expect(sumMoney([100, 250, 1])).toBe(351)
    expect(percentOf(25, 100)).toBe(25)
    expect(percentOf(1, 3)).toBe(33)
    expect(percentOf(10, 0)).toBe(0)
  })

  it('rejects non-integer amounts in dev', () => {
    expect(() => assertMoney(12.5)).toThrow(/integer in minor units/)
    expect(() => assertMoney(12)).not.toThrow()
  })
})
