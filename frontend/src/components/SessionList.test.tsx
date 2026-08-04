import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, fireEvent, act, within } from '@testing-library/react'
import { ConfigProvider, App as AntdApp } from 'antd'
import { SessionList } from './SessionList'
import type { SessionListItem } from '@/types/api'
import { ApiRequestError } from '@/api/errors'

afterEach(() => {
  cleanup()
})

function wrap(node: React.ReactNode) {
  return render(
    <ConfigProvider>
      <AntdApp>{node}</AntdApp>
    </ConfigProvider>,
  )
}

function makeSession(id: string, over: Partial<SessionListItem> = {}): SessionListItem {
  const result: SessionListItem = {
    session_id: id,
    patient_info: { name: '李明', gender: 'male', age: 35 },
    chief_complaint: '头痛',
    current_stage: 'inquiry',
    status: 'active',
    agent_runtime: 'legacy',
    pending_review: false,
    created_at: '2026-07-03T10:30:00+08:00',
    updated_at: '2026-07-03T10:35:00+08:00',
    ...over,
  }
  return result
}

describe('SessionList', () => {
  it('渲染列表项与新建按钮', () => {
    wrap(
      <SessionList
        sessions={[makeSession('s1'), makeSession('s2')]}
        loading={false}
        error={null}
        selectedId="s1"
        onSelect={() => {}}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )
    expect(screen.getByText('新建问诊')).toBeInTheDocument()
    const items = screen.getAllByText('李明 · 男 · 35岁')
    expect(items.length).toBe(2)
    expect(screen.getAllByText('07-03 10:35').length).toBe(2)
    expect(screen.getAllByText('Legacy').length).toBe(2)
  })

  it('标记 LangGraph v2 会话运行时', () => {
    wrap(
      <SessionList
        sessions={[makeSession('s-lg', { agent_runtime: 'langgraph' })]}
        loading={false}
        error={null}
        selectedId={null}
        onSelect={() => {}}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )
    expect(screen.getByTestId('runtime-s-lg')).toHaveTextContent('LangGraph v2')
  })

  it('点击列表项触发 onSelect', () => {
    const onSelect = vi.fn()
    wrap(
      <SessionList
        sessions={[makeSession('s1')]}
        loading={false}
        error={null}
        selectedId={null}
        onSelect={onSelect}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )
    // 通过 test-id 查询列表项 div
    const lists = screen.getAllByTestId('session-list')
    const item = lists[lists.length - 1].querySelector('[data-session-id="s1"]') as HTMLElement
    expect(item).not.toBeNull()
    act(() => {
      fireEvent.click(item)
    })
    expect(onSelect).toHaveBeenCalledWith('s1')
  })

  it('点击删除按钮仅隐藏该会话（不触发 onSelect）', () => {
    const onSelect = vi.fn()
    wrap(
      <SessionList
        sessions={[makeSession('s1'), makeSession('s2')]}
        loading={false}
        error={null}
        selectedId={null}
        onSelect={onSelect}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )
    act(() => {
      fireEvent.click(screen.getByTestId('remove-s1'))
    })
    // s1 从展示中消失，s2 保留
    const lists = screen.getAllByTestId('session-list')
    const itemList = lists[lists.length - 1]
    expect(itemList.querySelector('[data-session-id="s1"]')).toBeNull()
    expect(itemList.querySelector('[data-session-id="s2"]')).not.toBeNull()
    // 删除不触发选中
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('全部删除后显示空态', () => {
    wrap(
      <SessionList
        sessions={[makeSession('s1')]}
        loading={false}
        error={null}
        selectedId={null}
        onSelect={() => {}}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )
    act(() => {
      fireEvent.click(screen.getByTestId('remove-s1'))
    })
    expect(screen.getByText('暂无会话')).toBeInTheDocument()
  })

  it('collapsed 模式：会话缩成小块（姓名首字）', () => {
    wrap(
      <SessionList
        sessions={[makeSession('s1', { patient_info: { name: '王芳', gender: 'female', age: 28 } })]}
        loading={false}
        error={null}
        selectedId={null}
        collapsed
        onSelect={() => {}}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )
    // 首字小块存在
    expect(screen.getByText('王')).toBeInTheDocument()
    // 完整摘要不再显示
    expect(screen.queryByText('王芳 · 女 · 28岁')).toBeNull()
    // 新建按钮收起为图标（无文字）
    expect(screen.queryByText('新建问诊')).toBeNull()
  })

  it('collapsed 模式：点击小块触发 onSelect，删除仍有效', () => {
    const onSelect = vi.fn()
    const { unmount } = wrap(
      <SessionList
        sessions={[makeSession('s1'), makeSession('s2')]}
        loading={false}
        error={null}
        selectedId={null}
        collapsed
        onSelect={onSelect}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )
    act(() => {
      fireEvent.click(screen.getByTestId('session-list').querySelector('[data-session-id="s1"]') as HTMLElement)
    })
    expect(onSelect).toHaveBeenCalledWith('s1')
    act(() => {
      fireEvent.click(screen.getByTestId('remove-s2'))
    })
    const list = screen.getByTestId('session-list')
    expect(list.querySelector('[data-session-id="s2"]')).toBeNull()
    expect(list.querySelector('[data-session-id="s1"]')).not.toBeNull()
    unmount()
  })

  it('可按患者与主诉快速筛选会话', () => {
    wrap(
      <SessionList
        sessions={[
          makeSession('s1'),
          makeSession('s2', {
            patient_info: { name: '王芳', gender: 'female', age: 42 },
            chief_complaint: '咳嗽两周',
          }),
        ]}
        loading={false}
        error={null}
        selectedId={null}
        onSelect={() => {}}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )

    const lists = screen.getAllByTestId('session-list')
    const currentList = lists[lists.length - 1]
    fireEvent.change(within(currentList).getByLabelText('搜索会话'), {
      target: { value: '咳嗽' },
    })
    expect(currentList.querySelector('[data-session-id="s2"]')).toBeInTheDocument()
    expect(currentList.querySelector('[data-session-id="s1"]')).not.toBeInTheDocument()
  })

  it('空列表显示空态', () => {
    wrap(
      <SessionList
        sessions={[]}
        loading={false}
        error={null}
        selectedId={null}
        onSelect={() => {}}
        onRefresh={() => {}}
        onCreate={() => {}}
      />,
    )
    expect(screen.getByText('暂无会话')).toBeInTheDocument()
  })

  it('错误态显示重试按钮', () => {
    const err = new ApiRequestError({
      code: 'INTERNAL_ERROR',
      userMessage: '服务器错误',
      status: 500,
      retryable: true,
      traceId: 'trace-x',
    })
    const onRefresh = vi.fn()
    wrap(
      <SessionList
        sessions={[]}
        loading={false}
        error={err}
        selectedId={null}
        onSelect={() => {}}
        onRefresh={onRefresh}
        onCreate={() => {}}
      />,
    )
    expect(screen.getByText(/服务器错误/)).toBeInTheDocument()
    expect(screen.getByTestId('error-trace-id')).toHaveTextContent('trace-x')
    fireEvent.click(screen.getByTestId('error-retry'))
    expect(onRefresh).toHaveBeenCalled()
  })
})
