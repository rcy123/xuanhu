/**
 * 测试入口：注入 jsdom 环境所需的 polyfill（matchMedia / IntersectionObserver），
 * 加载 jest-dom 断言扩展。
 */

import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, vi } from 'vitest'

// vitest 使用 globals: false，RTL 不会自动注册 afterEach 清理；
// 显式注册以避免同一文件内多个测试的 DOM 相互累积。
afterEach(() => cleanup())

// Ant Design queries pseudo-element styles; jsdom logs a noisy "not implemented"
// diagnostic for the optional second argument even though tests only need the
// base computed style.  Drop that argument deterministically in the test DOM.
if (typeof window !== 'undefined') {
  const getComputedStyle = window.getComputedStyle.bind(window)
  window.getComputedStyle = (element: Element) => getComputedStyle(element)
}

// jsdom 未实现 matchMedia，Ant Design 响应式监听会调用它。
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })
}

// jsdom 未实现 IntersectionObserver；Ant Design 部分 Popover/Tooltip 依赖。
if (typeof globalThis.IntersectionObserver === 'undefined') {
  class MockIntersectionObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return []
    }
    root = null
    rootMargin = ''
    thresholds = []
  }
  globalThis.IntersectionObserver = MockIntersectionObserver as unknown as typeof IntersectionObserver
}

// jsdom 未实现 ResizeObserver。
if (typeof globalThis.ResizeObserver === 'undefined') {
  class MockResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  globalThis.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver
}

// jsdom 未实现 EventSource；SSE 客户端与 useSessionStream 依赖它。
// 测试中通常通过 vi.spyOn(connectSessionStream) 在调用前拦截，但
// useSessionStream 在建立连接前会检查 typeof EventSource，缺失则直接降级为轮询。
// 这里提供一个可被测试覆盖/删除的占位实现。
if (typeof globalThis.EventSource === 'undefined') {
  // eslint-disable-next-line @typescript-eslint/no-extraneous-class
  class MockEventSource {
    static readonly CONNECTING = 0
    static readonly OPEN = 1
    static readonly CLOSED = 2
    readonly CONNECTING = 0
    readonly OPEN = 1
    readonly CLOSED = 2
    onopen: ((ev: Event) => void) | null = null
    onmessage: ((ev: MessageEvent) => void) | null = null
    onerror: ((ev: Event) => void) | null = null
    readyState = 0
    url = ''
    withCredentials = false
    addEventListener() {}
    removeEventListener() {}
    dispatchEvent() {
      return false
    }
    close() {}
    constructor(url: string) {
      this.url = url
    }
  }
  globalThis.EventSource = MockEventSource as unknown as typeof EventSource
}

// 静默未处理的控制台错误，避免 Ant Design 内部 warning 噪声。
const originalError = console.error
console.error = (...args: unknown[]) => {
  const first = args[0]
  if (typeof first === 'string' && first.includes('not wrapped in act')) {
    return
  }
  originalError(...args)
}

// 防止 vi 全局未使用告警。
void vi
