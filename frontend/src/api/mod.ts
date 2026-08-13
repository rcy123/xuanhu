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
  setAuthSession,
  getAuthUser,
  clearAuthToken,
  clearAuthSession,
  isAuthenticated,
  isAdminAuthenticated,
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
  listAdminDoctors,
  createAdminDoctor,
  disableAdminDoctor,
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
  AuthenticatedUser,
  UserRole,
  DoctorAdminItem,
  AdminDoctorListParams,
  CreateAdminDoctorRequest,
} from '@/types/api'
export { downloadFileResponse } from './download'
