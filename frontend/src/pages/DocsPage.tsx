/**
 * 悬壶 WebUI —— 使用与设计文档页
 *
 * 面向使用者与协作者的两用文档：
 * - 前半部分：工作台使用指南（登录、问诊、诊疗工作流、权限）
 * - 后半部分：界面设计系统（理念、色彩、字体、间距、动效、质感、可访问性）
 *
 * 页面延续水墨编辑式设计语言（tokens.css 驱动），纯语义化标签 + 自有样式。
 */

import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import './styles/docs.css'

interface Swatch {
  name: string
  variable: string
  hex: string
  usage: string
}

const COLOR_SWATCHES: Swatch[] = [
  { name: '朱砂', variable: '--xh-primary', hex: '#C8442C', usage: '主按钮、印章、强调标记' },
  { name: '朱砂·深', variable: '--xh-primary-hover', hex: '#A9382B', usage: '主按钮悬停' },
  { name: '朱砂·浅', variable: '--xh-primary-soft', hex: '#F4DCD3', usage: '弱化底色、提示底' },
  { name: '松针绿', variable: '--xh-secondary', hex: '#6F8256', usage: '链接、辅助标记、安全提示' },
  { name: '墨黑', variable: '--xh-text', hex: '#1B1A17', usage: '标题与正文' },
  { name: '次级文本', variable: '--xh-text-secondary', hex: '#5A5751', usage: '说明文字' },
  { name: '三级文本', variable: '--xh-text-tertiary', hex: '#8A8679', usage: '注释、编号、水印' },
  { name: '旧宣纸', variable: '--xh-bg-page', hex: '#F2EDE0', usage: '页面底色' },
  { name: '精制宣纸', variable: '--xh-bg-card', hex: '#FFFEFA', usage: '卡片、输入框' },
  { name: '装饰纸', variable: '--xh-bg-paper', hex: '#FAF6E9', usage: '内衬、装裱层' },
  { name: '边框', variable: '--xh-border', hex: '#DCD4BF', usage: '分割线、描边' },
  { name: '成功', variable: '--xh-success', hex: '#5C7F4F', usage: '通过、归档' },
  { name: '警告', variable: '--xh-warning', hex: '#B58032', usage: '待复核、注意' },
  { name: '错误', variable: '--xh-error', hex: '#B04139', usage: '红线、失败' },
]

interface StageItem {
  code: string
  name: string
  description: string
}

const STAGES: StageItem[] = [
  {
    code: 'inquiry',
    name: '问诊',
    description: '系统围绕主诉开展多轮问诊，自动归档主诉与四诊要点，对话全程留痕。',
  },
  {
    code: 'syndrome',
    name: '辨证',
    description: '汇总四诊信息，给出辨证思路与证型判断，推理过程逐句可追溯。',
  },
  {
    code: 'formula',
    name: '方药',
    description: '在辨证结论上拟定方剂。医师可直接修改方药内容后再进入下一阶段。',
  },
  {
    code: 'safety',
    name: '安全审核',
    description: '妊娠、肝肾等红线与禁忌独立成栏，逐项校验后方可进入复核。',
  },
  {
    code: 'review',
    name: '医师复核',
    description: '确认处方通过；或补充信息回到辨证重新开方；或驳回修改后重审。',
  },
  {
    code: 'record',
    name: '病历',
    description: '方剂、医嘱、随访问卷一键归档，电子病历在一处完成。',
  },
]

interface RouteItem {
  path: string
  name: string
  auth: string
}

const ROUTES: RouteItem[] = [
  { path: '/', name: '首页', auth: '公开' },
  { path: '/docs', name: '使用与设计文档（本页）', auth: '公开' },
  { path: '/login', name: '医师登录', auth: '公开' },
  { path: '/admin/login', name: '管理员登录', auth: '公开' },
  { path: '/workbench', name: '临床工作台', auth: '需医师登录' },
  { path: '/sessions/:id', name: '问诊会话详情', auth: '需医师登录' },
  { path: '/admin/users', name: '账户管理', auth: '需管理员' },
]

const TOC = [
  { id: 'quickstart', num: '壹', label: '快速上手' },
  { id: 'workflow', num: '贰', label: '诊疗工作流' },
  { id: 'accounts', num: '叁', label: '账户与权限' },
  { id: 'design', num: '肆', label: '设计系统' },
  { id: 'routes', num: '伍', label: '页面地图' },
  { id: 'disclaimer', num: '陆', label: '免责声明' },
]

function DocsSection({
  id,
  num,
  title,
  children,
}: {
  id: string
  num: string
  title: string
  children: ReactNode
}) {
  return (
    <section className="xh-docs-section" id={id} aria-labelledby={`${id}-title`}>
      <header className="xh-docs-section-head">
        <span className="xh-docs-section-num">{num}</span>
        <h2 className="xh-docs-section-title" id={`${id}-title`}>
          {title}
        </h2>
      </header>
      {children}
    </section>
  )
}

export function DocsPage() {
  return (
    <div className="xh-docs">
      <header className="xh-docs-nav">
        <div className="xh-docs-nav-inner">
          <div className="xh-docs-nav-brand">
            <div className="xh-docs-nav-mark" aria-hidden="true">
              <img src="/xuanhu-mark.png" alt="" />
            </div>
            <div>
              <div className="xh-docs-nav-name">悬壶</div>
              <span className="xh-docs-nav-sub">使用与设计文档</span>
            </div>
          </div>
          <nav className="xh-docs-nav-actions" aria-label="文档导航">
            <Link className="xh-docs-nav-link" to="/">
              ← 返回首页
            </Link>
            <span className="xh-docs-nav-version">v1.0</span>
          </nav>
        </div>
      </header>

      <main className="xh-docs-main">
        <header className="xh-docs-hero">
          <span className="xh-docs-eyebrow">Documentation</span>
          <h1 className="xh-docs-title">
            悬壶 · <em>使用与设计文档</em>
          </h1>
          <p className="xh-docs-lede">
            这份文档分两部分：前半部分说明工作台怎么用，后半部分说明界面为什么长这样。
            无论是第一次打开工作台，还是参与界面迭代，都从这里开始。
          </p>
          <div className="xh-docs-meta">
            <span>版本 v1.0</span>
            <span aria-hidden="true">·</span>
            <span>适用于临床工作台与账户管理</span>
          </div>
        </header>

        <div className="xh-docs-layout">
          <aside className="xh-docs-toc" aria-label="文档目录">
            <span className="xh-docs-toc-label">目录</span>
            <ol>
              {TOC.map((item) => (
                <li key={item.id}>
                  <a href={`#${item.id}`}>
                    <span className="xh-docs-toc-num">{item.num}</span>
                    {item.label}
                  </a>
                </li>
              ))}
            </ol>
          </aside>

          <article className="xh-docs-article">
            <DocsSection id="quickstart" num="壹" title="快速上手">
              <h3>登录</h3>
              <p>
                在首页点击「进入工作台」，或直接访问 <code className="xh-docs-code">/workbench</code>。
                未登录时会自动跳转到登录页，输入登录名（拼音或工号）与密码即可。
              </p>
              <p>
                登录凭证只保存在本次浏览器会话中（sessionStorage），关闭浏览器后需要重新登录；
                凭证失效时系统会自动带你回到登录页。
              </p>

              <h3>工作台布局</h3>
              <ul className="xh-docs-list">
                <li>
                  <strong>顶部品牌栏</strong> —— 品牌标识与当前工作区（临床工作区）。
                </li>
                <li>
                  <strong>左侧会话栏</strong> —— 历史问诊会话列表，可收起；顶部按钮新建问诊。
                </li>
                <li>
                  <strong>中央对话区</strong> —— 多轮问诊对话、六阶段步骤条与各阶段结果。
                </li>
                <li>
                  <strong>底部输入区</strong> —— 消息输入与发送，流式回复实时呈现。
                </li>
              </ul>

              <h3>新建问诊</h3>
              <p>
                点击「新建问诊」，填写患者姓名、性别、年龄与主诉，创建会话后即可开始多轮对话。
                会话会自动出现在左侧列表，随时可以切回继续。
              </p>
            </DocsSection>

            <DocsSection id="workflow" num="贰" title="诊疗工作流">
              <p>
                一次问诊依次经过六个阶段，系统在阶段间自动推进，医师在关键节点复核：
              </p>
              <ol className="xh-docs-stages">
                {STAGES.map((stage, index) => (
                  <li key={stage.code}>
                    <span className="xh-docs-stage-index">
                      {String(index + 1).padStart(2, '0')}
                    </span>
                    <div>
                      <h3>
                        {stage.name}
                        <code className="xh-docs-code">{stage.code}</code>
                      </h3>
                      <p>{stage.description}</p>
                    </div>
                  </li>
                ))}
              </ol>
              <p>
                医师复核阶段有三个出口：<strong>确认处方</strong>、<strong>补充信息</strong>
                （回到辨证阶段重新开方）与<strong>驳回修改</strong>（修改后重新执行安全审核）。
                未通过安全审核的处方不会进入病历阶段。
              </p>
            </DocsSection>

            <DocsSection id="accounts" num="叁" title="账户与权限">
              <div className="xh-docs-role-grid">
                <div className="xh-docs-role">
                  <h3>医师 · doctor</h3>
                  <p>
                    登录后进入临床工作台，新建并跟进问诊会话，执行安全审核与处方复核。
                    医师账户由管理员创建与启用。
                  </p>
                </div>
                <div className="xh-docs-role">
                  <h3>管理员 · admin</h3>
                  <p>
                    登录后进入账户管理（<code className="xh-docs-code">/admin/users</code>），
                    负责医师账户的开通、启用与禁用。管理员账户不参与临床问诊。
                  </p>
                </div>
              </div>
              <p>
                服务端对每条接口做权限校验：医师访问管理接口会回到工作台，
                管理员访问临床接口会回到账户管理。前端跳转只是引导，后端才是权限的唯一权威来源。
              </p>
            </DocsSection>

            <DocsSection id="design" num="肆" title="设计系统">
              <h3>设计理念</h3>
              <p>
                整套界面锚定品牌资产的三个元素：<strong>葫芦水墨、松针绿叶、朱砂方印</strong>。
                底色是旧宣纸，墨色是暖黑——刻意避开纯白纯黑；排版借鉴编辑式版式：
                衬线大标题、等宽编号、竖排装饰字与巨型水墨底字。
              </p>

              <h3>色彩</h3>
              <p>
                主色只有一个：朱砂。松针绿作为辅助色承担链接与安全提示，不参与视觉竞争。
                所有中性色统一偏暖（宣纸灰），不混入冷灰。
              </p>
              <ul className="xh-docs-swatches">
                {COLOR_SWATCHES.map((swatch) => (
                  <li className="xh-docs-swatch" key={swatch.variable}>
                    <span
                      className="xh-docs-swatch-chip"
                      style={{ backgroundColor: swatch.hex }}
                      aria-hidden="true"
                    />
                    <div className="xh-docs-swatch-info">
                      <span className="xh-docs-swatch-name">{swatch.name}</span>
                      <code className="xh-docs-code">{swatch.variable}</code>
                      <span className="xh-docs-swatch-hex">{swatch.hex}</span>
                      <span className="xh-docs-swatch-usage">{swatch.usage}</span>
                    </div>
                  </li>
                ))}
              </ul>

              <h3>字体</h3>
              <ul className="xh-docs-list">
                <li>
                  <strong>Noto Serif SC</strong> —— 标题、品牌名与引言。大字号配合紧凑行高与负字距。
                </li>
                <li>
                  <strong>Noto Sans SC</strong> —— 正文与界面控件。正文行高约 1.7–1.85。
                </li>
                <li>
                  <strong>JetBrains Mono</strong> —— 编号、版本号、路径与代码。标签使用正字距 + 全大写。
                </li>
              </ul>

              <h3>间距与圆角</h3>
              <p>
                间距遵循 8px 基准网格（<code className="xh-docs-code">--xh-space-*</code>），
                页面级留白拉到 48–120px，让宣纸呼吸。圆角是单一半径体系：
                卡片 <code className="xh-docs-code">16px</code>、按钮与输入{' '}
                <code className="xh-docs-code">10px</code>、胶囊{' '}
                <code className="xh-docs-code">999px</code>，同一界面不混用多套半径。
              </p>

              <h3>动效</h3>
              <p>
                两条缓动曲线：<code className="xh-docs-code">--xh-ease-out (0.16, 1, 0.3, 1)</code>{' '}
                用于常规过渡，<code className="xh-docs-code">--xh-ease-spring (0.34, 1.56, 0.64, 1)</code>{' '}
                用于印章盖章等弹性动作。入场动画按 80–120ms 错峰编排，不一次性全部出现；
                区块浮现使用滚动驱动动画（animation-timeline）作为渐进增强。
                所有动效在 <code className="xh-docs-code">prefers-reduced-motion</code> 下全局降级。
              </p>

              <h3>质感手法</h3>
              <ul className="xh-docs-list">
                <li><strong>纸纹噪点</strong> —— SVG turbulence 叠加，混合模式 multiply。</li>
                <li><strong>水墨晕染</strong> —— 多层径向渐变，模拟宣纸渗墨。</li>
                <li><strong>朱砂笔触下划线</strong> —— 手绘 SVG 描边，替代直线高亮。</li>
                <li><strong>禅圆残环</strong> —— 带缺口的墨环，呼应毛笔起收笔。</li>
                <li><strong>印章</strong> —— 朱砂描边印，盖章式入场动效。</li>
                <li><strong>巨型底字与竖排字</strong> —— 编辑式版面的水印与页边注。</li>
              </ul>

              <h3>可访问性</h3>
              <ul className="xh-docs-list">
                <li>主按钮文字对比度约 6:1（WCAG AA 要求 ≥ 4.5:1）。</li>
                <li>所有可交互元素有可见焦点环与 <code className="xh-docs-code">:focus-visible</code> 处理。</li>
                <li>装饰性元素一律 <code className="xh-docs-code">aria-hidden</code>，导航与表单使用语义化标签。</li>
              </ul>
            </DocsSection>

            <DocsSection id="routes" num="伍" title="页面地图">
              <table className="xh-docs-table">
                <thead>
                  <tr>
                    <th scope="col">路径</th>
                    <th scope="col">页面</th>
                    <th scope="col">权限</th>
                  </tr>
                </thead>
                <tbody>
                  {ROUTES.map((route) => (
                    <tr key={route.path}>
                      <td>
                        <code className="xh-docs-code">{route.path}</code>
                      </td>
                      <td>{route.name}</td>
                      <td>{route.auth}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </DocsSection>

            <DocsSection id="disclaimer" num="陆" title="免责声明">
              <div className="xh-docs-callout">
                <p>
                  悬壶是<strong>辅助决策工具</strong>：问诊、辨证、方药与安全审核的全部结论仅供参考，
                  必须经执业中医师确认后方可使用。安全审核结果不能替代医师的临床判断，
                  红线提示以外的情况同样需要结合患者实际斟酌。
                </p>
              </div>
            </DocsSection>
          </article>
        </div>
      </main>

      <footer className="xh-docs-foot">
        <div className="xh-docs-foot-inner">
          <div className="xh-docs-foot-meta">
            <span className="xh-docs-foot-seal" aria-hidden="true">壶</span>
            <span>悬壶 · 中医 AI 临床助理</span>
          </div>
          <span>辅助决策工具，所有结论仅供参考，需经执业中医师确认后使用。</span>
        </div>
      </footer>
    </div>
  )
}

export default DocsPage
