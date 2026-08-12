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
  getAuthToken,
  setAuthToken,
  clearAuthToken,
  isAuthenticated,
  setAuthExpiredHandler,
  handleAuthExpired,
} from './auth'
export {
  createSession,
  listSessions,
  getSession,
  terminateSession,
  advanceSession,
  submitMessage,
  submitMessageWithRetry,
  listMessages,
  listSafetyAssertions,
  confirmSafetyAssertion,
  rejectSafetyAssertion,
  recoverSession,
  reviewPrescription,
  getCommandStatus,
  getRecord,
  updateRecord,
  exportRecord,
  getHealth,
  login,
  toQuery,
} from './index'
export type { LoginResult } from './index'
export {
  isAsyncCommandAccepted,
} from '@/types/api'
export type {
  AsyncCommandAccepted,
  CommandMutationResult,
  MessageSubmitResult,
  AdvanceMutationResult,
  ReviewMutationResult,
} from '@/types/api'
export { downloadFileResponse } from './download'
