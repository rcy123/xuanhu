import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ConfigProvider, App as AntdApp } from 'antd'
import { MessageList } from './MessageList'
import type { MessageItem } from '@/types/api'

function wrap(node: React.ReactNode) {
  return render(
    <ConfigProvider>
      <AntdApp>{node}</AntdApp>
    </ConfigProvider>,
  )
}

function makeMsg(id: string, role: MessageItem['role'], content: string): MessageItem {
  return {
    id,
    session_id: 's',
    role,
    stage: 'inquiry',
    content,
    created_at: '2026-07-03T10:31:00+08:00',
  }
}

describe('MessageList', () => {
  it('渲染消息气泡内容', () => {
    wrap(
      <MessageList
        messages={[makeMsg('m1', 'agent', '请问哪里不舒服'), makeMsg('m2', 'doctor', '头痛')]}
        loading={false}
        error={null}
        onRetry={() => {}}
      />,
    )
    expect(screen.getByText('请问哪里不舒服')).toBeInTheDocument()
    expect(screen.getByText('头痛')).toBeInTheDocument()
  })

  it('空消息显示空态引导', () => {
    wrap(
      <MessageList messages={[]} loading={false} error={null} onRetry={() => {}} />,
    )
    expect(screen.getByText('暂无问诊记录，开始第一条对话')).toBeInTheDocument()
  })

  it('首次加载（无消息）显示转圈', () => {
    wrap(
      <MessageList messages={[]} loading error={null} onRetry={() => {}} />,
    )
    expect(document.querySelector('.ant-spin')).toBeInTheDocument()
  })

  it('有历史消息时刷新不替换对话栏（保留气泡）', () => {
    wrap(
      <MessageList
        messages={[makeMsg('m1', 'agent', '请问哪里不舒服')]}
        loading
        error={null}
        onRetry={() => {}}
      />,
    )
    // 对话内容仍在
    expect(screen.getAllByText('请问哪里不舒服').length).toBeGreaterThan(0)
    // 走的是刷新分支（顶部轻量提示条），而非整栏转圈分支
    expect(screen.getByText('正在同步最新对话…')).toBeInTheDocument()
  })

  it('有历史消息时加载错误仅顶部提示，不清空对话', () => {
    wrap(
      <MessageList
        messages={[makeMsg('m1', 'agent', '请问哪里不舒服')]}
        loading={false}
        error={new Error('boom') as never}
        onRetry={() => {}}
      />,
    )
    expect(screen.getAllByText('请问哪里不舒服').length).toBeGreaterThan(0)
  })
})
