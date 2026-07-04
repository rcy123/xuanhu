/**
 * 悬壶 WebUI —— SSE 客户端
 *
 * 后端 SSE 端点：GET /api/v1/consult/sessions/{id}/stream
 *
 * 与接口设计文档 §5 对齐。事件格式：
 * ```
 * event: {event_type}
 * id: {event_id}
 * data: {json_payload}
 *
 * ```
 *
 * 断线重连：浏览器 EventSource 自动重连并携带 Last-Event-ID；服务端也支持
 * `last_event_id` 查询参数。本封装在 onmessage 中记录最新 event_id，并提供
 * `resync` 事件回调（收到 resync 应调用 GET /sessions/{id} 全量同步）。
 *
 * 当 EventSource 不可用或持续失败时，上层应降级为轮询 GET /sessions/{id}。
 */

import type { EventType, SessionEvent } from '@/types/api'

export interface SseHandlers {
  /** 收到事件。 */
  onEvent: (event: SessionEvent) => void
  /** 收到 resync 事件（需触发全量同步）。 */
  onResync?: (event: SessionEvent) => void
  /** 连接打开。 */
  onOpen?: () => void
  /** 连接出错（浏览器会自动重连；此处仅通知）。 */
  onError?: (err: Event) => void
}

export interface SseConnection {
  /** 主动关闭连接。 */
  close: () => void
  /** 当前是否已关闭。 */
  readonly closed: boolean
  /** 最近一次收到的事件 id（用于降级轮询时的游标参考）。 */
  lastEventId: string | null
}

/**
 * 连接会话 SSE 事件流。
 *
 * @param sessionId 会话 ID
 * @param handlers 事件回调
 * @param lastEventId 可选：断线重连时传入上次最后收到的事件 id
 */
export function connectSessionStream(
  sessionId: string,
  handlers: SseHandlers,
  lastEventId?: string,
): SseConnection {
  const base = getBaseUrl()
  const url = `${base}/consult/sessions/${sessionId}/stream`

  // EventSource 不支持自定义 header，但浏览器会自动携带 Last-Event-ID。
  // 服务端 stream.py 同时支持 last_event_id 查询参数，这里也带上以便首次连接。
  const sep = url.includes('?') ? '&' : '?'
  const fullUrl = lastEventId ? `${url}${sep}last_event_id=${encodeURIComponent(lastEventId)}` : url

  const source = new EventSource(fullUrl, { withCredentials: false })

  const connection: SseConnection = {
    closed: false,
    lastEventId: lastEventId ?? null,
    close: () => {
      ;(connection as { closed: boolean }).closed = true
      source.close()
    },
  }

  source.onopen = () => {
    handlers.onOpen?.()
  }

  source.onerror = (ev: Event) => {
    handlers.onError?.(ev)
    // EventSource 会自动重连，不在此处主动 close。
  }

  source.onmessage = (ev: MessageEvent) => {
    // 无 event: 的行会落到 onmessage（heartbeat 等没有显式 event 时）。
    // 但后端所有事件都设了 event: 字段，这里兜底处理。
    connection.lastEventId = ev.lastEventId || connection.lastEventId
    dispatch('message.created', ev.data, connection.lastEventId)
  }

  // 为每个已知事件类型注册监听。
  for (const type of KNOWN_EVENT_TYPES) {
    source.addEventListener(type, (ev: Event) => {
      const me = ev as MessageEvent
      connection.lastEventId = me.lastEventId || connection.lastEventId
      dispatch(type, me.data, connection.lastEventId)
    })
  }

  function dispatch(type: string, data: string | null, eventId: string | null): void {
    let payload: Record<string, unknown> = {}
    if (data) {
      try {
        // eslint-disable-next-line @typescript-eslint/no-unsafe-assignment
        payload = JSON.parse(data)
      } catch {
        payload = { _raw: data }
      }
    }
    const event: SessionEvent = {
      event_id: eventId ?? '',
      event_type: type as EventType,
      payload,
    }
    if (type === 'resync') {
      handlers.onResync?.(event)
    }
    handlers.onEvent(event)
  }

  return connection
}

// 复用 client.ts 的 base URL 解析。这里单独内联一份避免循环依赖复杂度。
function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL ?? '/api/v1'
}

const KNOWN_EVENT_TYPES: EventType[] = [
  'stage.changed',
  'message.created',
  'agent.started',
  'agent.finished',
  'agent.failed',
  'review.required',
  'safety.blocked',
  'session.blocked',
  'session.done',
  'session.terminated',
  'doctor.reviewed',
  'heartbeat',
  'resync',
]
