/**
 * App Shell 渲染测试（P8-1 验收：基础 App shell 可打开，不是空白页）
 *
 * Workbench 在 /workbench 路由下渲染；测试通过 MemoryRouter 进入该路由。
 * Ant Design Steps 组件渲染为无序列表，用 role 查询。
 */

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { ConfigProvider, App as AntdApp } from 'antd'
import App from '@/App'

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
  it('首页渲染品牌标题与脚手架占位说明', () => {
    renderAppAt('/')
    expect(screen.getByText('悬壶工作台')).toBeInTheDocument()
    expect(screen.getByText(/P8-1 脚手架已就绪/)).toBeInTheDocument()
  })

  it('工作台渲染品牌标题、免责声明与新建会话按钮', () => {
    renderAppAt('/workbench')
    expect(screen.getAllByText('悬壶').length).toBeGreaterThan(0)
    expect(screen.getByText(/辅助决策工具，所有结论仅供参考/)).toBeInTheDocument()
    expect(screen.getByText('新建会话')).toBeInTheDocument()
    expect(screen.getByText(/工作台占位视图/)).toBeInTheDocument()
  })

  it('工作台渲染步骤条节点', () => {
    renderAppAt('/workbench')
    // Ant Design Steps 渲染为 <ul role="list"> 嵌套 <li>，每个步骤标题在 .ant-steps-item-title 中。
    const steps = document.querySelectorAll('.ant-steps-item-title')
    const labels = Array.from(steps).map((el) => el.textContent?.trim())
    expect(labels).toContain('问诊')
    expect(labels).toContain('完备性')
    expect(labels).toContain('辨证')
    expect(labels).toContain('安全审核')
  })
})