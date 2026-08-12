/**
 * 悬壶 WebUI —— 登录态管理（阶段 1 加固）
 *
 * token 存 sessionStorage（非 localStorage）——降低 XSS 持久化风险；
 * 页面会话结束即清空。登录页调用 POST /api/v1/auth/login 后将 token 存入。
 *
 * 401 处理：client.ts 收到 UNAUTHENTICATED / INVALID_TOKEN / TOKEN_EXPIRED
 * 时调用 `handleAuthExpired()`（默认跳转 /login），测试可替换该处理器。
 */

const TOKEN_STORAGE_KEY = 'xuanhu.access_token'

/** 读取当前 token（无登录态返回 null）。 */
export function getAuthToken(): string | null {
  if (typeof window === 'undefined') return null
  return window.sessionStorage.getItem(TOKEN_STORAGE_KEY)
}

/** 保存 token（登录成功后调用）。 */
export function setAuthToken(token: string): void {
  window.sessionStorage.setItem(TOKEN_STORAGE_KEY, token)
}

/** 清除 token（登出 / token 失效时调用）。 */
export function clearAuthToken(): void {
  window.sessionStorage.removeItem(TOKEN_STORAGE_KEY)
}

/** 是否已登录。 */
export function isAuthenticated(): boolean {
  return getAuthToken() !== null
}

/** 认证失效跳转处理器（测试可覆盖）。 */
let authExpiredHandler: (() => void) | null = null

/** 设置认证失效处理器（App 层注入，跳转登录页）。 */
export function setAuthExpiredHandler(handler: (() => void) | null): void {
  authExpiredHandler = handler
}

/** 触发认证失效（401）。默认跳转登录页。 */
export function handleAuthExpired(): void {
  clearAuthToken()
  if (authExpiredHandler) {
    authExpiredHandler()
    return
  }
  if (typeof window !== 'undefined' && window.location.pathname !== '/login') {
    window.location.assign('/login')
  }
}
