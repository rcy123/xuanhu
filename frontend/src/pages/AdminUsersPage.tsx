/**
 * 悬壶 WebUI —— 管理员用户管理页。
 *
 * 本页的本地角色信息仅用于路由与展示体验；每一项读取、创建、停用操作均由服务端
 * 通过 Bearer token 再次校验管理员权限。
 */

import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Form,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  LogoutOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
  UserAddOutlined,
  UserOutlined,
} from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { clearAuthSession, getAuthUser } from '@/api/auth'
import { ApiRequestError } from '@/api/errors'
import { createAdminDoctor, disableAdminDoctor, listAdminDoctors } from '@/api/index'
import type { CreateAdminDoctorRequest, DoctorAdminItem, PageData } from '@/types/api'
import './styles/admin.css'

const { Title, Text } = Typography

const DEFAULT_PAGE_SIZE = 20

interface CreateUserFormValues {
  username: string
  name: string
  password: string
}

function formatDateTime(value: string | null): string {
  if (!value) return '从未登录'
  const matched = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(value)
  return matched ? `${matched[1]} ${matched[2]}` : value
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiRequestError
    ? error.userMessage
    : error instanceof Error
      ? error.message
      : fallback
}

export function AdminUsersPage() {
  const navigate = useNavigate()
  const currentUser = getAuthUser()
  const [form] = Form.useForm<CreateUserFormValues>()
  const [page, setPage] = useState(1)
  const [query, setQuery] = useState('')
  const [appliedQuery, setAppliedQuery] = useState('')
  const [data, setData] = useState<PageData<DoctorAdminItem>>({
    items: [],
    total: 0,
    page: 1,
    page_size: DEFAULT_PAGE_SIZE,
  })
  const [loading, setLoading] = useState(true)
  const [listError, setListError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [disableTarget, setDisableTarget] = useState<DoctorAdminItem | null>(null)
  const [disabling, setDisabling] = useState(false)

  const handleAdminAuthFailure = useCallback((error: unknown): boolean => {
    if (
      error instanceof ApiRequestError
      && (error.code === 'ADMIN_REQUIRED' || error.code === 'ACCOUNT_DISABLED')
    ) {
      clearAuthSession()
      navigate('/admin/login', {
        replace: true,
        state: { authError: '管理员权限已失效，请重新登录。' },
      })
      return true
    }
    return false
  }, [navigate])

  const loadUsers = useCallback(async (nextPage = page, nextQuery = appliedQuery) => {
    setLoading(true)
    setListError(null)
    try {
      const result = await listAdminDoctors({
        page: nextPage,
        page_size: DEFAULT_PAGE_SIZE,
        query: nextQuery || undefined,
      })
      setData(result)
    } catch (error: unknown) {
      if (handleAdminAuthFailure(error)) return
      setListError(errorMessage(error, '用户列表加载失败，请稍后重试'))
    } finally {
      setLoading(false)
    }
  }, [appliedQuery, handleAdminAuthFailure, page])

  useEffect(() => {
    void loadUsers()
  }, [loadUsers])

  const handleSearch = (value: string) => {
    const nextQuery = value.trim()
    setQuery(value)
    setPage(1)
    setAppliedQuery(nextQuery)
  }

  const handleCreate = async (values: CreateUserFormValues) => {
    setCreating(true)
    setActionError(null)
    try {
      const body: CreateAdminDoctorRequest = {
        username: values.username.trim(),
        name: values.name.trim(),
        password: values.password,
      }
      await createAdminDoctor(body)
      form.resetFields()
      setCreateOpen(false)
      setPage(1)
      await loadUsers(1, appliedQuery)
    } catch (error: unknown) {
      if (handleAdminAuthFailure(error)) return
      setActionError(errorMessage(error, '添加用户失败，请稍后重试'))
    } finally {
      setCreating(false)
    }
  }

  const handleDisable = async () => {
    if (!disableTarget) return
    setDisabling(true)
    setActionError(null)
    try {
      await disableAdminDoctor(disableTarget.id)
      setDisableTarget(null)
      await loadUsers()
    } catch (error: unknown) {
      if (handleAdminAuthFailure(error)) return
      setActionError(errorMessage(error, '停用用户失败，请稍后重试'))
    } finally {
      setDisabling(false)
    }
  }

  const handleLogout = () => {
    clearAuthSession()
    navigate('/admin/login', { replace: true })
  }

  const columns: ColumnsType<DoctorAdminItem> = [
    {
      title: '姓名',
      dataIndex: 'name',
      key: 'name',
      render: (name: string) => (
        <Space size={8}>
          <span className="xh-admin-user-avatar" aria-hidden="true"><UserOutlined /></span>
          <strong>{name}</strong>
        </Space>
      ),
    },
    {
      title: '登录名',
      dataIndex: 'username',
      key: 'username',
      width: 160,
      render: (username: string) => <Text className="xh-admin-username" copyable>{username}</Text>,
    },
    {
      title: '账户标识',
      dataIndex: 'id',
      key: 'id',
      render: (id: string) => (
        <Text className="xh-admin-account-id" copyable={{ text: id }}>{id.slice(0, 8)}…</Text>
      ),
    },
    {
      title: '角色',
      dataIndex: 'role',
      key: 'role',
      width: 100,
      render: (role: DoctorAdminItem['role']) => (
        <Tag color={role === 'admin' ? 'purple' : 'blue'}>{role === 'admin' ? '管理员' : '医师'}</Tag>
      ),
    },
    {
      title: '状态',
      dataIndex: 'enabled',
      key: 'enabled',
      width: 96,
      render: (enabled: boolean) => (
        <Tag color={enabled ? 'success' : 'default'}>{enabled ? '已启用' : '已停用'}</Tag>
      ),
    },
    {
      title: '最近登录',
      dataIndex: 'last_login_at',
      key: 'last_login_at',
      width: 150,
      render: (value: string | null) => formatDateTime(value),
    },
    {
      title: '创建时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 150,
      render: (value: string) => formatDateTime(value),
    },
    {
      title: '操作',
      key: 'actions',
      width: 112,
      fixed: 'right',
      render: (_: unknown, record: DoctorAdminItem) => (
        <Button
          type="link"
          danger
          size="small"
          icon={<StopOutlined />}
          disabled={!record.enabled || record.id === currentUser?.id || record.role === 'admin'}
          onClick={() => setDisableTarget(record)}
          data-testid={`disable-user-${record.id}`}
        >
          停用
        </Button>
      ),
    },
  ]

  return (
    <main className="xh-admin-page" data-testid="admin-users-page">
      <header className="xh-admin-header">
        <div className="xh-admin-brand">
          <div className="xh-admin-mark" aria-hidden="true">悬</div>
          <div>
            <Text className="xh-admin-eyebrow">XUANHU ADMIN</Text>
            <Title level={4}>账户管理</Title>
          </div>
        </div>
        <Space size="middle" className="xh-admin-account-actions">
          <Text type="secondary">当前管理员：{currentUser?.name ?? '—'}</Text>
          <Button icon={<LogoutOutlined />} onClick={handleLogout} data-testid="admin-logout">
            退出登录
          </Button>
        </Space>
      </header>

      <section className="xh-admin-content" aria-label="医师用户管理">
        <div className="xh-admin-page-heading">
          <div>
            <Title level={2}>医师用户</Title>
            <Text type="secondary">添加、查询或停用可登录系统的医师账户。</Text>
          </div>
          <Button
            type="primary"
            icon={<UserAddOutlined />}
            onClick={() => {
              setActionError(null)
              setCreateOpen(true)
            }}
            data-testid="create-user"
          >
            添加用户
          </Button>
        </div>

        {actionError ? (
          <Alert
            className="xh-admin-action-error"
            type="error"
            showIcon
            closable
            message={actionError}
            onClose={() => setActionError(null)}
            data-testid="admin-action-error"
          />
        ) : null}

        <Card className="xh-admin-users-card" bordered={false}>
          <div className="xh-admin-toolbar">
            <Input.Search
              allowClear
              value={query}
              placeholder="按姓名或账户标识搜索"
              prefix={<SearchOutlined />}
              enterButton="搜索"
              onChange={(event) => setQuery(event.target.value)}
              onSearch={handleSearch}
              aria-label="搜索用户"
            />
            <Button
              icon={<ReloadOutlined />}
              loading={loading}
              onClick={() => void loadUsers()}
              aria-label="刷新用户列表"
              data-testid="refresh-users"
            >
              刷新
            </Button>
          </div>

          {listError ? (
            <Alert
              type="error"
              showIcon
              message={listError}
              action={<Button size="small" onClick={() => void loadUsers()}>重试</Button>}
              data-testid="admin-list-error"
            />
          ) : null}

          <Table<DoctorAdminItem>
            className="xh-admin-users-table"
            columns={columns}
            dataSource={data.items}
            rowKey="id"
            loading={loading}
            scroll={{ x: 960 }}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无匹配用户" /> }}
            pagination={{
              current: data.page || page,
              pageSize: data.page_size || DEFAULT_PAGE_SIZE,
              total: data.total,
              showSizeChanger: false,
              showTotal: (total) => `共 ${total} 个用户`,
              onChange: (nextPage) => setPage(nextPage),
            }}
          />
        </Card>

        <p className="xh-admin-help">
          “停用”会禁止该账户后续登录，不会删除其已有的临床记录和审计信息。
        </p>
      </section>

      <Modal
        title="添加医师用户"
        open={createOpen}
        okText="创建用户"
        cancelText="取消"
        confirmLoading={creating}
        destroyOnHidden
        onOk={() => form.submit()}
        onCancel={() => {
          if (creating) return
          form.resetFields()
          setCreateOpen(false)
        }}
      >
        <Form<CreateUserFormValues>
          form={form}
          layout="vertical"
          requiredMark={false}
          onFinish={(values) => void handleCreate(values)}
        >
          <Form.Item
            label="登录名"
            name="username"
            rules={[
              { required: true, message: '请输入登录名' },
              {
                pattern: /^[a-zA-Z0-9][a-zA-Z0-9._-]{2,63}$/,
                message: '登录名须为 3-64 位字母/数字/._-，且以字母或数字开头',
              },
            ]}
            extra="医师登录时使用；例如拼音或工号，创建后可用于登录。"
          >
            <Input autoFocus maxLength={64} placeholder="例如：zhangsan 或 D0012" autoComplete="off" data-testid="create-username" />
          </Form.Item>
          <Form.Item
            label="姓名"
            name="name"
            rules={[
              { required: true, whitespace: true, message: '请输入姓名' },
              { max: 64, message: '姓名不能超过 64 个字符' },
            ]}
          >
            <Input maxLength={64} placeholder="例如：张医生" autoComplete="name" />
          </Form.Item>
          <Form.Item
            label="初始密码"
            name="password"
            rules={[
              { required: true, message: '请输入初始密码' },
              { min: 12, message: '密码至少需要 12 个字符' },
              { max: 256, message: '密码不能超过 256 个字符' },
            ]}
            extra="密码只会发送一次到服务端，不会在用户列表中显示。"
          >
            <Input.Password autoComplete="new-password" placeholder="设置初始密码" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="确认停用用户"
        open={disableTarget !== null}
        okText="确认停用"
        okButtonProps={{ danger: true }}
        cancelText="取消"
        confirmLoading={disabling}
        onOk={() => void handleDisable()}
        onCancel={() => {
          if (!disabling) setDisableTarget(null)
        }}
      >
        <p>
          确认停用“{disableTarget?.name}”吗？该用户将无法再登录，已有临床记录和审计信息会保留。
        </p>
      </Modal>
    </main>
  )
}

export default AdminUsersPage
