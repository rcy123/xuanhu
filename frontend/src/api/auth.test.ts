import { afterEach, describe, expect, it } from 'vitest'
import {
  clearAuthSession,
  getAuthToken,
  getAuthUser,
  isAdminAuthenticated,
  isAuthenticated,
  setAuthSession,
  setAuthToken,
} from './auth'

afterEach(() => {
  window.sessionStorage.clear()
})

describe('认证会话存储', () => {
  it('保存并读取最小账户身份', () => {
    setAuthSession('admin-token', { id: 'admin-1', username: 'admin', name: '管理员', role: 'admin' })

    expect(getAuthToken()).toBe('admin-token')
    expect(getAuthUser()).toEqual({ id: 'admin-1', username: 'admin', name: '管理员', role: 'admin' })
    expect(isAuthenticated()).toBe(true)
    expect(isAdminAuthenticated()).toBe(true)
  })

  it('损坏或非法的用户身份不被当作管理员', () => {
    window.sessionStorage.setItem('xuanhu.access_token', 'some-token')
    window.sessionStorage.setItem('xuanhu.auth_user', '{not-json')
    expect(getAuthUser()).toBeNull()
    expect(isAdminAuthenticated()).toBe(false)

    window.sessionStorage.setItem('xuanhu.auth_user', JSON.stringify({
      id: 'x', name: 'x', role: 'super-admin',
    }))
    expect(getAuthUser()).toBeNull()
    expect(isAdminAuthenticated()).toBe(false)
  })

  it('旧的仅 token 写入会清除过期角色信息', () => {
    setAuthSession('admin-token', { id: 'admin-1', username: 'admin', name: '管理员', role: 'admin' })
    setAuthToken('doctor-token')

    expect(getAuthToken()).toBe('doctor-token')
    expect(getAuthUser()).toBeNull()
    expect(isAdminAuthenticated()).toBe(false)
  })

  it('退出会同时清理 token 与用户身份', () => {
    setAuthSession('doctor-token', { id: 'doctor-1', username: 'zhangsan', name: '医师', role: 'doctor' })
    clearAuthSession()

    expect(getAuthToken()).toBeNull()
    expect(getAuthUser()).toBeNull()
    expect(isAuthenticated()).toBe(false)
  })
})
