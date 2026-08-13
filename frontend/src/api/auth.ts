/**
 * 悬壶 WebUI —— 登录态管理（阶段 1 加固）
 *
 * token 存 sessionStorage（非 localStorage）——降低 XSS 持久化风险；
 * 页面会话结束即清空。登录页调用 POST /api/v1/auth/login 后将 token 存入。
 *
 * 401 处理：client.ts 收到 UNAUTHENTICATED / INVALID_TOKEN / TOKEN_EXPIRED
 * 时调用 `handleAuthExpired()`（默认跳转 /login），测试可替换该处理器。
 */

import type { AuthenticatedUser } from '@/types/api'

const TOKEN_STORAGE_KEY = 'xuanhu.access_token'
const USER_STORAGE_KEY = 'xuanhu.auth_user'

/** 读取当前 token（无登录态返回 null）。 */
export function getAuthToken(): string | null {
  if (typeof window === 'undefined') return null
  return window.sessionStorage.getItem(TOKEN_STORAGE_KEY)
}

/** 保存 token（登录成功后调用）。 */
export function setAuthToken(token: string): void {
  window.sessionStorage.setItem(TOKEN_STORAGE_KEY, token)
  // 兼容旧调用方：只更新 token 时不能沿用上一次会话的角色信息。
  window.sessionStorage.removeItem(USER_STORAGE_KEY)
}

/** 保存完整的最小登录会话。账户信息仅供前端展示和路由体验使用。 */
export function setAuthSession(token: string, user: AuthenticatedUser): void {
  window.sessionStorage.setItem(TOKEN_STORAGE_KEY, token)
  window.sessionStorage.setItem(USER_STORAGE_KEY, JSON.stringify(user))
}

/** 读取最小账户信息；无法解析或字段非法时按无身份处理。 */
export function getAuthUser(): AuthenticatedUser | null {
  if (typeof window === 'undefined') return null
  const raw = window.sessionStorage.getItem(USER_STORAGE_KEY)
  if (!raw) return null
  try {
    const value: unknown = JSON.parse(raw)
    if (
      typeof value !== 'object'
      || value === null
      || typeof (value as Record<string, unknown>).id !== 'string'
      || typeof (value as Record<string, unknown>).username !== 'string'
      || typeof (value as Record<string, unknown>).name !== 'string'
      || !['doctor', 'admin'].includes((value as Record<string, unknown>).role as string)
    ) {
      return null
    }
    return value as AuthenticatedUser
  } catch {
    return null
  }
}

/** 清除 token（登出 / token 失效时调用）。 */
export function clearAuthToken(): void {
  window.sessionStorage.removeItem(TOKEN_STORAGE_KEY)
  window.sessionStorage.removeItem(USER_STORAGE_KEY)
}

/** 清除当前浏览器会话（退出登录）。 */
export const clearAuthSession = clearAuthToken

/** 是否已登录。 */
export function isAuthenticated(): boolean {
  return getAuthToken() !== null
}

/** 本地仅用于界面路由体验；后端仍必须验证管理员权限。 */
export function isAdminAuthenticated(): boolean {
  return isAuthenticated() && getAuthUser()?.role === 'admin'
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
