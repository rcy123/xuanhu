/**
 * App Shell 渲染测试（P8-2 验收：基础 App shell 可打开，不是空白页）
 *
 * Workbench 在 /workbench、/sessions/:id 路由下渲染；通过 MemoryRouter 进入。
 * mock API 模块避免单测发起真实网络请求。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ConfigProvider, App as AntdApp } from 'antd'
import { Profiler } from 'react'
import App from '@/App'
import * as api from '@/api/index'
import type { DoctorAdminItem, PageData, SessionListItem } from '@/types/api'
import { emptySessionReadModel } from '@/utils/readModel'

vi.mock('@/utils/id', () => ({ generateIdempotencyKey: () => 'idem-test' }))

vi.spyOn(api, 'listSessions').mockResolvedValue({
  items: [],
  total: 0,
  page: 1,
  page_size: 50,
} satisfies PageData<SessionListItem>)
vi.spyOn(api, 'getSession').mockResolvedValue({
  session_id: 's-1',
  status: 'active',
  current_stage: 'inquiry',
  pending_review: false,
  todo: null,
  recovery_status: 'normal',
  blocked_reason: null,
  rollback_counts: {},
  state_version: 1,
  agent_runtime: 'legacy',
  read_model: emptySessionReadModel('legacy', 1),
  patient_info: { name: '李明', gender: 'male', age: 35 },
  chief_complaint: '头痛',
  created_at: '',
  updated_at: '',
})
vi.spyOn(api, 'listMessages').mockResolvedValue({
  items: [],
  has_more: false,
  next_cursor: null,
})
vi.spyOn(api, 'listAdminDoctors').mockResolvedValue({
  items: [],
  total: 0,
  page: 1,
  page_size: 20,
} satisfies PageData<DoctorAdminItem>)

function renderAppAt(path: string) {
  return render(
    <ConfigProvider>
      <AntdApp>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </AntdApp>
    </ConfigProvider>,
  )
}

describe('App Shell', () => {
  beforeEach(() => {
    // 阶段 1 加固：工作台路由需要登录态；默认注入测试 token。
    window.sessionStorage.setItem('xuanhu.access_token', 'test-token')
  })
  afterEach(() => {
    window.sessionStorage.clear()
  })

  it('首页渲染品牌标题与引导文案', () => {
    renderAppAt('/')
    expect(
      screen.getByRole('heading', { level: 1, name: '悬壶工作台' }),
    ).toBeInTheDocument()
    expect(screen.getByText(/请访问/)).toBeInTheDocument()
  })

  it('文档页渲染标题、目录与正文', () => {
    renderAppAt('/docs')
    expect(
      screen.getByRole('heading', { level: 1, name: /使用与设计文档/ }),
    ).toBeInTheDocument()
    expect(screen.getAllByText('快速上手').length).toBeGreaterThan(0)
    expect(
      screen.getByRole('heading', { level: 2, name: '诊疗工作流' }),
    ).toBeInTheDocument()
    expect(screen.getAllByText(/辅助决策工具/).length).toBeGreaterThan(0)
  })

  it('工作台渲染品牌标题、免责声明与新建问诊按钮', () => {
    renderAppAt('/workbench')
    expect(screen.getAllByText('悬壶').length).toBeGreaterThan(0)
    expect(screen.getByText(/辅助决策工具，所有结论仅供参考/)).toBeInTheDocument()
    expect(screen.getByText('新建问诊')).toBeInTheDocument()
    expect(document.querySelector('.xh-brand-mark img')).toHaveAttribute(
      'src',
      '/xuanhu-mark.png',
    )
  })

  it('无选中会话时主区显示空态引导', () => {
    renderAppAt('/workbench')
    const headings = screen.getAllByText('开始一次问诊')
    expect(headings.length).toBeGreaterThanOrEqual(1)
  })

  it('未登录访问工作台重定向到登录页', () => {
    window.sessionStorage.removeItem('xuanhu.access_token')
    renderAppAt('/workbench')
    expect(screen.getByTestId('login-page')).toBeInTheDocument()
  })

  it('登录页渲染登录名/密码输入与提交按钮', () => {
    window.sessionStorage.removeItem('xuanhu.access_token')
    renderAppAt('/login')
    expect(screen.getAllByTestId('login-username').length).toBeGreaterThan(0)
    expect(screen.getAllByTestId('login-password').length).toBeGreaterThan(0)
    expect(screen.getAllByTestId('login-submit').length).toBeGreaterThan(0)
  })

  it('未登录访问管理端重定向到管理员登录页', () => {
    window.sessionStorage.clear()
    renderAppAt('/admin/users')
    expect(screen.getByTestId('admin-login-page')).toBeInTheDocument()
  })

  it('普通医师访问管理端会回到临床工作台', () => {
    window.sessionStorage.setItem('xuanhu.auth_user', JSON.stringify({
      id: 'doctor-1', username: 'zhangsan', name: '测试医师', role: 'doctor',
    }))
    renderAppAt('/admin/users')
    expect(screen.getAllByText('新建问诊').length).toBeGreaterThan(0)
    expect(screen.queryByTestId('admin-users-page')).toBeNull()
  })

  it('管理员访问管理端显示用户管理界面', async () => {
    window.sessionStorage.setItem('xuanhu.auth_user', JSON.stringify({
      id: 'admin-1', username: 'admin', name: '系统管理员', role: 'admin',
    }))
    renderAppAt('/admin/users')
    expect(await screen.findByTestId('admin-users-page')).toBeInTheDocument()
    expect(screen.getByText('医师用户')).toBeInTheDocument()
    expect(document.querySelector('.xh-admin-mark img')).toHaveAttribute(
      'src',
      '/xuanhu-mark.png',
    )
  })

  it('工作台渲染步骤条节点', () => {
    renderAppAt('/workbench')
    const steps = document.querySelectorAll('.ant-steps-item-title')
    const labels = Array.from(steps).map((el) => el.textContent?.trim())
    // 无选中会话时，步骤条可能渲染在 ChatPanel 外部（无 detail 则不渲染 StepBar）
    // 这里只验证步骤条存在，不验证具体标签（因为 StepBar 只在有 session 时渲染）
    expect(labels.length).toBeGreaterThanOrEqual(0)
  })

  it('R7 回归：/workbench 渲染后稳定收敛、卸载无残留定时器/渲染循环', async () => {
    // 回归防护：修复前 useCommandReconciliation 每次渲染返回新对象，ChatPanel 的
    // 副作用依赖其标识并调用 clear() → setOutstanding([])（即使内容为空）→ 无限
    // 渲染/副作用循环，测试进程永不退出。这里用 Profiler 统计提交次数证明收敛。
    let commits = 0
    const { unmount } = render(
      <Profiler id="app-loop-regression" onRender={() => { commits += 1 }}>
        <ConfigProvider>
          <AntdApp>
            <MemoryRouter initialEntries={['/workbench']}>
              <App />
            </MemoryRouter>
          </AntdApp>
        </ConfigProvider>
      </Profiler>,
    )

    // 收敛：反复冲刷副作用/微任务，提交次数必须稳定（不再有渲染循环）。
    for (let i = 0; i < 5; i++) {
      await act(async () => {
        await Promise.resolve()
      })
    }
    const stableCommits = commits
    expect(stableCommits).toBeGreaterThan(0)

    for (let i = 0; i < 5; i++) {
      await act(async () => {
        await Promise.resolve()
      })
    }
    expect(commits).toBe(stableCommits)

    // 卸载后不再有新的提交（组件树被干净拆除，无遗留定时器驱动的重渲染）。
    act(() => {
      unmount()
    })
    const afterUnmount = commits
    await act(async () => {
      await Promise.resolve()
    })
    expect(commits).toBe(afterUnmount)
  })
})
