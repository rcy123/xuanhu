import { describe, expect, it } from 'vitest'
import { generateIdempotencyKey } from './id'

describe('generateIdempotencyKey', () => {
  it('生成非空字符串', () => {
    const k = generateIdempotencyKey()
    expect(typeof k).toBe('string')
    expect(k.length).toBeGreaterThan(0)
  })

  it('每次生成不同值', () => {
    const a = generateIdempotencyKey()
    const b = generateIdempotencyKey()
    expect(a).not.toBe(b)
  })
})
