import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ConfigProvider, App as AntdApp } from 'antd'
import { MemoryRouter } from 'react-router-dom'
import { AdminUsersPage } from './AdminUsersPage'
import * as api from '@/api/index'
import { setAuthSession } from '@/api/auth'
import type { DoctorAdminItem } from '@/types/api'

vi.mock('@/utils/id', () => ({ generateIdempotencyKey: () => 'idem-admin-test' }))

const admin: DoctorAdminItem = {
  id: 'admin-1',
  username: 'admin',
  name: '系统管理员',
  role: 'admin',
  enabled: true,
  last_login_at: '2026-08-13T10:15:00+08:00',
  created_at: '2026-08-01T09:00:00+08:00',
}

const doctor: DoctorAdminItem = {
  id: 'doctor-1',
  username: 'zhangsan',
  name: '张医生',
  role: 'doctor',
  enabled: true,
  last_login_at: null,
  created_at: '2026-08-12T09:00:00+08:00',
}

function wrap() {
  return render(
    <ConfigProvider>
      <AntdApp>
        <MemoryRouter>
          <AdminUsersPage />
        </MemoryRouter>
      </AntdApp>
    </ConfigProvider>,
  )
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.sessionStorage.clear()
})

describe('AdminUsersPage', () => {
  it('加载用户、搜索并呈现账户信息', async () => {
    setAuthSession('admin-token', { id: admin.id, username: admin.username, name: admin.name, role: 'admin' })
    const list = vi.spyOn(api, 'listAdminDoctors').mockResolvedValue({
      items: [admin, doctor], total: 2, page: 1, page_size: 20,
    })

    wrap()

    expect(await screen.findByText('张医生')).toBeInTheDocument()
    expect(screen.getByText('系统管理员')).toBeInTheDocument()
    expect(screen.getByText('从未登录')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('搜索用户'), { target: { value: '张' } })
    fireEvent.keyDown(screen.getByLabelText('搜索用户'), { key: 'Enter', code: 'Enter' })
    await waitFor(() => {
      expect(list).toHaveBeenLastCalledWith({ page: 1, page_size: 20, query: '张' })
    })
  })

  it('创建医师账号后刷新列表，并执行至少 12 位的密码校验', async () => {
    setAuthSession('admin-token', { id: admin.id, username: admin.username, name: admin.name, role: 'admin' })
    vi.spyOn(api, 'listAdminDoctors').mockResolvedValue({
      items: [admin], total: 1, page: 1, page_size: 20,
    })
    const create = vi.spyOn(api, 'createAdminDoctor').mockResolvedValue(doctor)
    wrap()
    await screen.findByText('系统管理员')

    fireEvent.click(screen.getByTestId('create-user'))
    fireEvent.change(screen.getByLabelText('登录名'), { target: { value: 'zhangsan' } })
    fireEvent.change(screen.getByLabelText('姓名'), { target: { value: '张医生' } })
    fireEvent.change(screen.getByLabelText('初始密码'), { target: { value: 'short' } })
    fireEvent.click(screen.getByRole('button', { name: '创建用户' }))
    expect(await screen.findByText('密码至少需要 12 个字符')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()

    fireEvent.change(screen.getByLabelText('初始密码'), { target: { value: 'long-password-12' } })
    fireEvent.click(screen.getByRole('button', { name: '创建用户' }))
    await waitFor(() => {
      expect(create).toHaveBeenCalledWith({ username: 'zhangsan', name: '张医生', password: 'long-password-12' })
    })
  })

  it('停用用户前要求二次确认，确认后调用软停用接口', async () => {
    setAuthSession('admin-token', { id: admin.id, username: admin.username, name: admin.name, role: 'admin' })
    vi.spyOn(api, 'listAdminDoctors').mockResolvedValue({
      items: [admin, doctor], total: 2, page: 1, page_size: 20,
    })
    const disable = vi.spyOn(api, 'disableAdminDoctor').mockResolvedValue({ ...doctor, enabled: false })
    wrap()
    await screen.findByText('张医生')

    fireEvent.click(screen.getByTestId(`disable-user-${doctor.id}`))
    expect(screen.getByText('确认停用用户')).toBeInTheDocument()
    expect(disable).not.toHaveBeenCalled()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: '确认停用' }))
    })
    await waitFor(() => expect(disable).toHaveBeenCalledWith(doctor.id))
  })

  it('不允许在前端停用当前管理员或其他管理员账户', async () => {
    setAuthSession('admin-token', { id: admin.id, username: admin.username, name: admin.name, role: 'admin' })
    const anotherAdmin: DoctorAdminItem = { ...admin, id: 'admin-2', name: '另一管理员' }
    vi.spyOn(api, 'listAdminDoctors').mockResolvedValue({
      items: [admin, anotherAdmin], total: 2, page: 1, page_size: 20,
    })
    wrap()
    await screen.findByText('系统管理员')

    expect(screen.getByTestId(`disable-user-${admin.id}`)).toBeDisabled()
    expect(screen.getByTestId(`disable-user-${anotherAdmin.id}`)).toBeDisabled()
  })
})
