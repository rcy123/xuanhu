import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ConfigProvider, App as AntdApp } from 'antd'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { LoginPage } from './LoginPage'
import * as api from '@/api/index'

function renderLogin(mode: 'doctor' | 'admin') {
  return render(
    <ConfigProvider>
      <AntdApp>
        <MemoryRouter initialEntries={['/login']}>
          <Routes>
            <Route path="/login" element={<LoginPage mode={mode} />} />
            <Route path="/workbench" element={<div data-testid="doctor-destination" />} />
            <Route path="/admin/users" element={<div data-testid="admin-destination" />} />
          </Routes>
        </MemoryRouter>
      </AntdApp>
    </ConfigProvider>,
  )
}

function submitLogin() {
  fireEvent.change(screen.getByTestId('login-username'), { target: { value: 'zhangsan' } })
  fireEvent.change(screen.getByTestId('login-password'), { target: { value: 'correct-password' } })
  fireEvent.click(screen.getByTestId('login-submit'))
}

afterEach(() => {
  vi.restoreAllMocks()
  window.sessionStorage.clear()
})

describe('LoginPage', () => {
  it('管理员即使从医师登录入口登录，也进入账户管理', async () => {
    const login = vi.spyOn(api, 'login').mockResolvedValue({
      access_token: 'admin-token',
      token_type: 'Bearer',
      expires_in: 28_800,
      user: { id: 'admin-1', username: 'admin', name: '系统管理员', role: 'admin' },
    })
    renderLogin('doctor')

    submitLogin()

    expect(await screen.findByTestId('admin-destination')).toBeInTheDocument()
    expect(login).toHaveBeenCalledWith('zhangsan', 'correct-password')
    expect(window.sessionStorage.getItem('xuanhu.access_token')).toBe('admin-token')
    expect(window.sessionStorage.getItem('xuanhu.auth_user')).toContain('admin')
  })

  it('医师从管理员登录入口登录时进入临床工作台', async () => {
    vi.spyOn(api, 'login').mockResolvedValue({
      access_token: 'doctor-token',
      token_type: 'Bearer',
      expires_in: 28_800,
      user: { id: 'doctor-1', username: 'zhangsan', name: '张医生', role: 'doctor' },
    })
    renderLogin('admin')

    expect(screen.getByTestId('admin-login-page')).toBeInTheDocument()
    submitLogin()

    await waitFor(() => {
      expect(screen.getByTestId('doctor-destination')).toBeInTheDocument()
    })
    expect(window.sessionStorage.getItem('xuanhu.auth_user')).toContain('doctor')
  })
})
