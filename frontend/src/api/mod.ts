export {
  request,
  requestWithRetry,
  getBaseUrl,
  __setBaseUrlResolver,
  getTraceIdFromResponse,
} from './client'
export type { RequestContext } from './client'
export { ApiRequestError, TransportErrorCode, shouldRetry } from './errors'
export { connectSessionStream } from './sse'
export type { SseConnection, SseHandlers } from './sse'
export {
  createSession,
  listSessions,
  getSession,
  terminateSession,
  submitMessage,
  submitMessageWithRetry,
  listMessages,
  recoverSession,
  reviewPrescription,
  getHealth,
  toQuery,
} from './index'