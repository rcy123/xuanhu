/**
 * 测试入口：注入 jsdom 环境所需的 polyfill（matchMedia / IntersectionObserver），
 * 加载 jest-dom 断言扩展。
 */

import '@testing-library/jest-dom/vitest'
import { vi } from 'vitest'

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
