/**
 * 悬壶 WebUI —— 工作台 App Shell（P8-1）
 *
 * 按 UI 设计文档 §3 信息架构搭建：
 * - 左侧 280px 侧边栏（会话列表占位 + 新建会话入口）
 * - 右侧主区域（顶部品牌标题 + 全局免责声明 + 步骤条占位 + 内容区 + 底部操作栏）
 *
 * P8-1 不实现完整业务流：会话列表、问诊对话、SSE 阶段结果均为占位视图。
 * 路由：/ 与 /workbench、/sessions/:id（占位），后续 P8-2/P8-3 填充。
 */

import { useMemo } from 'react'
import { Layout, Typography, Steps, Empty, Button, Tag, theme } from 'antd'
import { PlusOutlined } from '@ant-design/icons'
import { Link, Route, Routes } from 'react-router-dom'
import { STEP_NODES } from '@/utils/stage'

const { Header, Sider, Content, Footer } = Layout
const { Title, Text } = Typography

function DisclaimerBar() {
  return (
    <div
      style={{
        background: 'var(--xh-border)',
        color: 'var(--xh-text)',
        padding: '4px var(--xh-space-l)',
        fontSize: 12,
        textAlign: 'center',
      }}
    >
      辅助决策工具，所有结论仅供参考，需经执业中医师确认后使用。
    </div>
  )
}

function BrandHeader() {
  const { token } = theme.useToken()
  return (
    <Header
      style={{
        background: token.colorBgContainer,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '0 var(--xh-space-l)',
        borderBottom: `1px solid var(--xh-border)`,
        height: 56,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontSize: 20 }}>🌿</span>
        <Title
          level={4}
          style={{
            margin: 0,
            fontFamily: 'var(--xh-font-serif)',
            color: 'var(--xh-primary)',
          }}
        >
          悬壶
        </Title>
        <Text type="secondary" style={{ fontSize: 12 }}>
          Xuanhu
        </Text>
      </div>
      <Text type="secondary" style={{ fontSize: 12 }}>
        中医 AI 辅助诊疗工作台
      </Text>
    </Header>
  )
}

function SessionSider() {
  const { token } = theme.useToken()
  return (
    <Sider
      width={280}
      style={{
        background: token.colorBgContainer,
        borderRight: `1px solid var(--xh-border)`,
        overflow: 'auto',
        padding: 'var(--xh-space-l)',
      }}
    >
      <Button type="primary" icon={<PlusOutlined />} block style={{ marginBottom: 16 }}>
        新建会话
      </Button>
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={<Text type="secondary">暂无会话</Text>}
      />
      <div style={{ marginTop: 16 }}>
        <Text type="secondary" style={{ fontSize: 11 }}>
          会话列表将在 P8-2 实现
        </Text>
      </div>
    </Sider>
  )
}

function StepBarPlaceholder() {
  const { token } = theme.useToken()
  const items = useMemo(() => STEP_NODES.map((n) => ({ title: n.label })), [])
  return (
    <div
      style={{
        background: token.colorBgContainer,
        border: `1px solid var(--xh-border)`,
        borderRadius: 'var(--xh-radius-card)',
        padding: 'var(--xh-space-l)',
        marginBottom: 'var(--xh-space-l)',
      }}
    >
      <Steps current={0} size="small" items={items} style={{ padding: 0 }} />
    </div>
  )
}

function ContentArea() {
  const { token } = theme.useToken()
  return (
    <Content
      style={{
        background: 'var(--xh-bg-page)',
        padding: 'var(--xh-space-l)',
        overflow: 'auto',
      }}
    >
      <StepBarPlaceholder />
      <div
        style={{
          background: token.colorBgContainer,
          border: `1px solid var(--xh-border)`,
          borderRadius: 'var(--xh-radius-card)',
          padding: 'var(--xh-space-xxl)',
          minHeight: 400,
          boxShadow: 'var(--xh-shadow-card)',
        }}
      >
        <Empty
          description={
            <div>
              <Text type="secondary">工作台占位视图</Text>
              <br />
              <Text type="secondary" style={{ fontSize: 12 }}>
                问诊对话（P8-2）、阶段结果与 SSE（P8-3）将在此区域实现。
              </Text>
            </div>
          }
        />
      </div>
    </Content>
  )
}

function BottomBar() {
  const { token } = theme.useToken()
  return (
    <Footer
      style={{
        background: token.colorBgContainer,
        borderTop: `1px solid var(--xh-border)`,
        padding: 'var(--xh-space-s) var(--xh-space-l)',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        height: 48,
      }}
    >
      <Text type="secondary" style={{ fontSize: 12 }}>
        底部操作栏占位（问诊输入将在 P8-2 实现）
      </Text>
      <Tag color="default">P8-1 脚手架</Tag>
    </Footer>
  )
}

function Workbench() {
  return (
    <Layout style={{ height: '100vh' }}>
      <BrandHeader />
      <DisclaimerBar />
      <Layout>
        <SessionSider />
        <Layout>
          <ContentArea />
          <BottomBar />
        </Layout>
      </Layout>
    </Layout>
  )
}

function PlaceholderHome() {
  return (
    <div style={{ padding: 32, textAlign: 'center' }}>
      <Title level={3} style={{ fontFamily: 'var(--xh-font-serif)' }}>
        悬壶工作台
      </Title>
      <Text type="secondary">
        P8-1 脚手架已就绪。请访问 <Link to="/workbench">工作台</Link> 查看 App Shell。
      </Text>
    </div>
  )
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<PlaceholderHome />} />
      <Route path="/workbench" element={<Workbench />} />
      <Route path="/sessions/:id" element={<Workbench />} />
    </Routes>
  )
}
