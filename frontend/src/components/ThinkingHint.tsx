/**
 * 悬壶 WebUI —— 假装思考提示条（Claude Code 风格）
 *
 * 问诊消息提交后 / agent 运行期间，在输入框上方循环切换提示短语，
 * 让等待过程更自然。短语按 agent 名称分组，未匹配时走通用短语池。
 */

import { useEffect, useMemo, useState } from 'react'
import { Typography } from 'antd'

const { Text } = Typography

/** 每个 agent 一组循环短语；名称按后端 agent_name 约定。 */
const AGENT_PHRASES: Record<string, string[]> = {
  intake: [
    '正在理解患者描述…',
    '正在梳理症状要点…',
    '正在提取关键信息…',
    '正在核对问诊要素…',
  ],
  question_composer: [
    '正在分析问诊进度…',
    '正在检查遗漏要素…',
    '正在构思下一个问题…',
  ],
  syndrome_draft: [
    '正在辨证分析…',
    '正在对照中医知识库…',
    '正在权衡证型归属…',
  ],
  base_formula_draft: [
    '正在拟定基础方…',
    '正在核对方剂配伍…',
  ],
  modification_draft: [
    '正在加减化裁…',
    '正在核对药物剂量…',
    '正在检查药性冲突…',
  ],
  safety: [
    '正在执行安全复核…',
    '正在检查药物禁忌…',
  ],
  record_narrative: [
    '正在组织诊疗信息…',
    '正在撰写病历…',
  ],
}

/** 通用短语池（无 agent 信息或未知 agent 时循环）。 */
const FALLBACK_PHRASES = [
  '正在思考…',
  '正在分析…',
  '正在整理思路…',
  '马上就好…',
]

/** 短语切换间隔（毫秒）。 */
const ROTATE_INTERVAL_MS = 2_200

interface ThinkingHintProps {
  /** 是否显示（提交中或 agent 运行中）。 */
  active: boolean
  /** 当前运行的 agent 名称（可选）。 */
  agent?: string | null
}

export function ThinkingHint({ active, agent }: ThinkingHintProps) {
  const phrases = useMemo(() => {
    if (agent && AGENT_PHRASES[agent]) return AGENT_PHRASES[agent]
    return FALLBACK_PHRASES
  }, [agent])
  const [index, setIndex] = useState(0)

  useEffect(() => {
    if (!active) {
      setIndex(0)
      return
    }
    setIndex(0)
    const timer = window.setInterval(() => {
      setIndex((prev) => (prev + 1) % phrases.length)
    }, ROTATE_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [active, phrases.length])

  if (!active) return null

  return (
    <div className="xh-thinking-hint" role="status" aria-live="polite">
      <span className="xh-thinking-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <Text className="xh-thinking-copy" type="secondary">
        <span key={index} className="xh-thinking-phrase">
          {phrases[index]}
        </span>
      </Text>
    </div>
  )
}

export default ThinkingHint
