import type { AdminGrant, AdqAccountCatalog, AdqAccountMaterialMetrics, AdqAdgroup, AdqHierarchy, AdqStatus, AdqTarget, AdqTask, Asset, ChannelsAccount, ChannelsAuthSession, ChannelsProduct, ChannelsPromotionAuth, ChannelsPromotionAuthSession, ChannelsPromotionOrder, ChannelsPromotionPaymentSession, ChannelsPromotionQuoteInput, ChannelsPromotionQuoteResult, ChannelsStatus, ChannelsTask, ChannelsVideoAnnotationInput, EffectiveClipLibraryStatus, Facets, JianyingDevice, JianyingImportTask, JianyingPairing, MultipartPartUrl, MultipartUploadedPart, MultipartUploadSession, OaAccessGrant, OaAccessGrantResult, OaPermissions, OaUser, OperationLog, PersonalSalesSnapshot, ProductImageCategory, PushPreference, PushScheme, QianchuanAccount, QianchuanPlanMaterialResult, QianchuanProductPlanMap, QianchuanPlanResult, QianchuanStatus, QianchuanTarget, QianchuanTask, ReviewAiRule, ReviewNamingContext, ReviewNamingEvidence, ReviewRoleMember, ReviewSubmission, ReviewWorkflowConfig, Stats, SyncStatus, UploadAnalyticsResult, UploadTicket, UserNotification, VideoRequestAssignee, VideoRequestItem, VideoRequestPage } from './types'

export class ApiError extends Error {
  constructor(message: string, public status: number, public attempts = 1) {
    super(message)
    this.name = 'ApiError'
  }
}

const RETRYABLE_READ_STATUSES = new Set([0, 408, 425, 429, 500, 502, 503, 504])
const READ_REQUEST_ATTEMPTS = 3

const waitForRetry = (attempt: number) => new Promise<void>((resolve) => {
  const delay = 320 * (2 ** attempt) + Math.floor(Math.random() * 160)
  window.setTimeout(resolve, delay)
})

type RequestPolicy = { timeoutMs?: number; attempts?: number }

const requestOnce = async <T>(url: string, options?: RequestInit, timeoutMs = 90000): Promise<T> => {
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs)
  const relayAbort = () => controller.abort()
  if (options?.signal?.aborted) controller.abort()
  else options?.signal?.addEventListener('abort', relayAbort, { once: true })
  try {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(options?.headers || {}) },
      ...options,
      signal: controller.signal,
    })
    const contentType = response.headers.get('content-type') || ''
    if (response.redirected && new URL(response.url).pathname.startsWith('/_auth/login')) {
      window.location.assign(response.url)
      throw new ApiError('正在跳转到统一 OA 登录…', 401)
    }
    if (!response.ok) {
      const data = await response.json().catch((error: unknown) => {
        if (controller.signal.aborted) throw error
        return {}
      })
      const validationDetail = Array.isArray(data.detail)
        ? data.detail.map((item: { msg?: string; loc?: unknown[] }) => {
            const field = Array.isArray(item?.loc) ? item.loc.filter(value => value !== 'body').join('.') : ''
            return `${field ? `${field}：` : ''}${item?.msg || '字段格式不正确'}`
          }).join('；')
        : ''
      throw new ApiError(typeof data.detail === 'string' ? data.detail : validationDetail || '请求失败', response.status)
    }
    if (!contentType.includes('application/json')) throw new ApiError('登录状态已失效，请重新进入统一 OA 登录', 401)
    // Keep the deadline and caller cancellation active until the body is read.
    return await response.json()
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw new ApiError(`请求等待超过 ${timeoutMs / 1000} 秒，请重试`, 408)
    if (error instanceof TypeError) throw new ApiError('网络连接中断，请稍后重试', 0)
    throw error
  } finally {
    window.clearTimeout(timeout)
    options?.signal?.removeEventListener('abort', relayAbort)
  }
}

const request = async <T>(url: string, options?: RequestInit, policy: RequestPolicy = {}): Promise<T> => {
  const method = (options?.method || 'GET').toUpperCase()
  const isReadRequest = method === 'GET' || method === 'HEAD'
  const maxAttempts = policy.attempts ?? (isReadRequest ? READ_REQUEST_ATTEMPTS : 1)
  let lastError: unknown
  let attemptsUsed = 0

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    attemptsUsed = attempt + 1
    try {
      return await requestOnce<T>(url, options, policy.timeoutMs)
    } catch (cause) {
      lastError = cause
      const abortedByCaller = Boolean(options?.signal?.aborted)
      const retryable = cause instanceof ApiError && RETRYABLE_READ_STATUSES.has(cause.status)
      if (!retryable || abortedByCaller || attempt >= maxAttempts - 1) break
      await waitForRetry(attempt)
    }
  }

  if (lastError instanceof ApiError && attemptsUsed > 1 && RETRYABLE_READ_STATUSES.has(lastError.status)) {
    throw new ApiError(`${lastError.message}；已自动重试 ${attemptsUsed - 1} 次`, lastError.status, attemptsUsed)
  }
  throw lastError
}

export const api = {
  me: () => request<{ user: OaUser; permissions: OaPermissions }>('api/auth/me', undefined, { timeoutMs: 15000, attempts: 1 }),
  login: (username: string, password: string, captcha = '') => request<{ user: OaUser; permissions: OaPermissions }>('api/auth/login', { method: 'POST', body: JSON.stringify({ username, password, captcha }) }),
  captcha: (username: string) => request<{ ok: boolean; message: string }>('api/auth/captcha', { method: 'POST', body: JSON.stringify({ username }) }),
  logout: () => request<{ ok: boolean }>('api/auth/logout', { method: 'POST' }),
  stats: () => request<Stats>('api/stats'),
  facets: (libraryType = '', assetScope = '') => {
    const params = new URLSearchParams()
    if (libraryType) params.set('library_type', libraryType)
    if (assetScope) params.set('asset_scope', assetScope)
    return request<Facets>(`api/facets${params.size ? `?${params}` : ''}`)
  },
  facetBundle: () => request<{ all: Facets; source: Facets; remix: Facets }>('api/facets/bundle'),
  productImageCategories: () => request<{ items: ProductImageCategory[]; total: number }>('api/product-images/categories'),
  personalSales: (startDate = '', endDate = '') => {
    const params = new URLSearchParams()
    if (startDate) params.set('start_date', startDate)
    if (endDate) params.set('end_date', endDate)
    return request<PersonalSalesSnapshot>(`api/personal-sales${params.size ? `?${params}` : ''}`)
  },
  uploadAnalytics: (startDate: string, endDate: string) => {
    const params = new URLSearchParams({ start_date: startDate, end_date: endDate })
    return request<UploadAnalyticsResult>(`api/analytics/uploads?${params}`)
  },
  assets: (params: URLSearchParams) => request<{ items: Asset[]; total: number; page: number; page_size: number; source: string; source_updated_at?: string }>(`api/assets?${params}`),
  asset: (id: number) => request<Asset>(`api/assets/${id}?include_performance=false`),
  trash: (params: URLSearchParams) => request<{ items: Asset[]; total: number; page: number; page_size: number; source: string; source_updated_at?: string }>(`api/trash?${params}`),
  update: (id: number, data: Partial<Pick<Asset, 'filename' | 'category' | 'content_type' | 'status' | 'library_type' | 'asset_subtype' | 'folder_name' | 'tags' | 'reference_url' | 'reference_video_key' | 'reference_video_name' | 'material_description' | 'performance_screenshots'>>) =>
    request<Asset>(`api/assets/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  favorite: (id: number) => request<Asset>(`api/assets/${id}/favorite`, { method: 'POST' }),
  effective: (id: number, effective: boolean) => request<Asset>(`api/assets/${id}/effective`, {
    method: 'PUT', body: JSON.stringify({ effective }),
  }),
  effectiveClipImport: (id: number) => request<{ asset_id: number; status: string; message: string }>(`api/assets/${id}/clip-library/import`, { method: 'POST' }),
  effectiveClipStatus: (id: number) => request<{ status: EffectiveClipLibraryStatus }>(`api/assets/${id}/clip-library/status`),
  delete: (id: number) => request<Asset>(`api/assets/${id}`, { method: 'DELETE' }),
  batchDelete: (assetIds: number[]) => request<{ ok: boolean; count: number; asset_ids: number[] }>('api/assets/batch-delete', {
    method: 'POST', body: JSON.stringify({ asset_ids: assetIds }),
  }),
  batchTags: (assetIds: number[], tags: string[], mode: 'add' | 'remove' | 'replace' = 'add') => request<{ ok: boolean; count: number; asset_ids: number[]; tags: string[] }>('api/assets/batch-tags', {
    method: 'POST', body: JSON.stringify({ asset_ids: assetIds, tags, mode }),
  }),
  batchFolder: (assetIds: number[], folderName: string) => request<{ ok: boolean; count: number; asset_ids: number[]; folder_name: string }>('api/assets/batch-folder', {
    method: 'POST', body: JSON.stringify({ asset_ids: assetIds, folder_name: folderName }),
  }),
  restore: (id: number) => request<Asset>(`api/trash/${id}/restore`, { method: 'POST' }),
  purge: (id: number) => request<{ ok: boolean; asset_id: number }>(`api/trash/${id}`, { method: 'DELETE' }),
  purgeTrashAll: (confirmText: string) => request<{ ok: boolean; checked: number; purged: number; purged_ids: number[]; failed: Array<{ asset_id: number; filename: string; detail: string }> }>('api/trash/purge-all', {
    method: 'POST', body: JSON.stringify({ confirm_text: confirmText }),
  }),
  refresh: () => request<SyncStatus>('api/sync/oss', { method: 'POST' }),
  refreshStatus: () => request<SyncStatus>('api/sync/oss/status'),
  createUpload: (file: File, assetScope: 'marketing_video' | 'product_image' = 'marketing_video', category = '待分类') => request<UploadTicket>('api/uploads/presign', {
    method: 'POST',
    body: JSON.stringify({ filename: file.name, content_type: file.type || 'application/octet-stream', size: file.size, asset_scope: assetScope, category }),
  }),
  createReferenceUpload: (file: File) => request<UploadTicket>('api/uploads/presign', {
    method: 'POST',
    body: JSON.stringify({ filename: file.name, content_type: file.type || 'application/octet-stream', size: file.size, asset_scope: 'reference_video', category: '参考视频' }),
  }),
  completeUpload: (data: { object_key: string; filename: string; category: string; content_type: string; asset_scope: 'marketing_video' | 'product_image'; library_type: 'source' | 'remix'; asset_subtype: string; folder_name?: string; tags: string[]; reference_url?: string; reference_video_key?: string; reference_video_name?: string; material_description?: string; performance_screenshots?: { object_key: string; filename: string }[] }) =>
    request<Asset>('api/uploads/complete', { method: 'POST', body: JSON.stringify(data) }),
  createMultipartUpload: (data: { filename: string; content_type: string; size: number; sha256: string; asset_scope: 'marketing_video' | 'product_image' | 'reference_video'; category: string }) =>
    request<MultipartUploadSession>('api/uploads/multipart/sessions', { method: 'POST', body: JSON.stringify(data) }),
  lookupMultipartUpload: (data: { filename: string; content_type: string; size: number; sha256: string; legacy_sha256: string; asset_scope: 'marketing_video' | 'product_image' | 'reference_video'; category: string }) =>
    request<{ session: MultipartUploadSession | null; recovery_required?: boolean }>('api/uploads/multipart/lookup', { method: 'POST', body: JSON.stringify(data) }),
  multipartUploadStatus: (sessionId: string) => request<MultipartUploadSession>(`api/uploads/multipart/sessions/${encodeURIComponent(sessionId)}`),
  multipartPartUrls: (sessionId: string, partNumbers: number[]) => request<{ items: MultipartPartUrl[]; total: number }>(`api/uploads/multipart/sessions/${encodeURIComponent(sessionId)}/parts`, {
    method: 'POST', body: JSON.stringify({ part_numbers: partNumbers }),
  }),
  completeMultipartUpload: (sessionId: string, parts: MultipartUploadedPart[], sha256: string) => request<MultipartUploadSession>(`api/uploads/multipart/sessions/${encodeURIComponent(sessionId)}/complete`, {
    method: 'POST', body: JSON.stringify({ parts, sha256 }),
  }),
  cancelMultipartUpload: (sessionId: string) => request<{ ok: boolean; session_id: string; status: string }>(`api/uploads/multipart/sessions/${encodeURIComponent(sessionId)}`, { method: 'DELETE' }),
  acquireUploadLease: (sessionId: string, requestId: string) => request<{ acquired: boolean; lease_id?: string; retry_after_ms?: number; expires_in?: number; queue_position?: number; queue_total?: number; global_in_use?: number; global_limit?: number; user_in_use?: number; user_limit?: number }>('api/uploads/part-leases/acquire', {
    method: 'POST', body: JSON.stringify({ session_id: sessionId, request_id: requestId }),
  }),
  renewUploadLease: (leaseId: string) => request<{ ok: boolean; expires_in: number }>('api/uploads/part-leases/renew', { method: 'POST', body: JSON.stringify({ lease_id: leaseId }) }),
  releaseUploadLease: (leaseId: string) => request<{ ok: boolean }>('api/uploads/part-leases/release', { method: 'POST', body: JSON.stringify({ lease_id: leaseId }) }),
  jianyingPairingCreate: () => request<JianyingPairing>('api/jianying/pairings', { method: 'POST' }),
  jianyingPairingStatus: (pairingId: string) => request<{ id: string; status: string; device_id: string; expires_at: string; claimed_at?: string | null }>(`api/jianying/pairings/${encodeURIComponent(pairingId)}`),
  jianyingDevices: () => request<{ items: JianyingDevice[]; total: number }>('api/jianying/devices'),
  jianyingDeviceRevoke: (deviceId: string) => request<{ ok: boolean; device_id: string }>(`api/jianying/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE' }),
  jianyingImport: (assetId: number) => request<JianyingImportTask>(`api/assets/${assetId}/jianying-import`, { method: 'POST' }),
  jianyingImportStatus: (ticketId: string) => request<JianyingImportTask>(`api/jianying/imports/${encodeURIComponent(ticketId)}`),
  accessGrants: (q = '') => request<OaAccessGrantResult>(`api/admin/access-grants?q=${encodeURIComponent(q)}`),
  accessGrantCreate: (realName: string, department = '') => request<OaAccessGrant>('api/admin/access-grants', {
    method: 'POST', body: JSON.stringify({ real_name: realName, department }),
  }),
  accessGrantRevoke: (identifier: string) => request<OaAccessGrant>(`api/admin/access-grants/${encodeURIComponent(identifier)}`, { method: 'DELETE' }),
  adminGrants: (q = '') => request<{ items: AdminGrant[]; total: number }>(`api/admin/admin-grants?q=${encodeURIComponent(q)}`),
  adminGrantCreate: (realName: string, department = '') => request<AdminGrant>('api/admin/admin-grants', {
    method: 'POST', body: JSON.stringify({ real_name: realName, department }),
  }),
  adminGrantRevoke: (identifier: string) => request<AdminGrant>(`api/admin/admin-grants/${encodeURIComponent(identifier)}`, { method: 'DELETE' }),
  operationLogs: (q = '', module = '', result = '', page = 1, pageSize = 20) => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) })
    if (q.trim()) params.set('q', q.trim())
    if (module) params.set('module', module)
    if (result) params.set('result', result)
    return request<{ items: OperationLog[]; total: number; page: number; page_size: number; total_pages: number; modules: string[] }>(`api/admin/operation-logs?${params}`)
  },
  reviewConfig: () => request<ReviewWorkflowConfig>('api/reviews/config'),
  reviewConfigUpdate: (enabled: boolean, namingEnabled: boolean, aiRedlineEnabled: boolean, requiredRoles: string[]) => request<ReviewWorkflowConfig>('api/reviews/config', {
    method: 'PUT', body: JSON.stringify({ enabled, naming_enabled: namingEnabled, ai_redline_enabled: aiRedlineEnabled, required_roles: requiredRoles }),
  }),
  reviewRedlineRulesUpdate: (rules: ReviewAiRule[]) => request<ReviewWorkflowConfig>('api/reviews/redline-rules', {
    method: 'PUT', body: JSON.stringify({ rules: rules.map(({ code, category, severity, title, pattern, enabled }) => ({ code, category, severity, title, pattern, enabled })) }),
  }),
  reviewRoleCreate: (data: { role_code: string; user_name: string; user_number?: string; department?: string; center?: string; group_name?: string }) => request<ReviewRoleMember>('api/reviews/roles', {
    method: 'POST', body: JSON.stringify(data),
  }),
  reviewRoleDelete: (id: number) => request<{ ok: boolean; id: number }>(`api/reviews/roles/${id}`, { method: 'DELETE' }),
  reviewPendingCount: () => request<{ count: number; roles: string[]; rejected_count: number; ai_attention_count: number }>('api/reviews/pending-count'),
  reviewSubmissions: (q = '', status = 'all', page = 1, pageSize = 20, reviewer = '') => {
    const params = new URLSearchParams({ q, reviewer, status, page: String(page), page_size: String(pageSize) })
    return request<{ items: ReviewSubmission[]; total: number; page: number; page_size: number; total_pages: number; workflow: ReviewWorkflowConfig }>(`api/reviews/submissions?${params}`)
  },
  reviewNamingContext: (assetId: number) => request<ReviewNamingContext>(`api/reviews/assets/${assetId}/naming-context`),
  reviewSubmit: (assetId: number, data: { note?: string; naming_evidence?: ReviewNamingEvidence[]; no_applicable_sources?: boolean; assignment_mode?: 'organization' | 'designated'; designated_reviewer_number?: string; designated_reviewer_name?: string }) => request<ReviewSubmission>(`api/reviews/assets/${assetId}/submit`, {
    method: 'POST', body: JSON.stringify(data),
  }),
  reviewBatchSubmit: (items: Array<{ asset_id: number; note?: string; naming_evidence?: ReviewNamingEvidence[]; no_applicable_sources?: boolean; assignment_mode?: 'organization' | 'designated'; designated_reviewer_number?: string; designated_reviewer_name?: string }>) => request<{ ok: boolean; total: number; succeeded: Array<{ asset_id: number; submission_id: string; asset_name: string; status: string; route_center: string; route_group: string; assignment_mode: 'organization' | 'designated'; designated_reviewer_number: string; designated_reviewer_name: string; current_reviewers: Array<{ user_name: string; user_number: string; center: string; group_name: string }> }>; failed: Array<{ asset_id: number; detail: string }> }>('api/reviews/assets/batch-submit', {
    method: 'POST', body: JSON.stringify({ items }),
  }),
  reviewApprove: (submissionId: string, roleCode: string, note = '', qualityScores: Record<string, number> = {}) => request<ReviewSubmission>(`api/reviews/submissions/${encodeURIComponent(submissionId)}/approve`, {
    method: 'POST', body: JSON.stringify({ role_code: roleCode, note, quality_scores: qualityScores }),
  }),
  reviewReject: (submissionId: string, roleCode: string, note: string, qualityScores: Record<string, number> = {}) => request<ReviewSubmission>(`api/reviews/submissions/${encodeURIComponent(submissionId)}/reject`, {
    method: 'POST', body: JSON.stringify({ role_code: roleCode, note, quality_scores: qualityScores }),
  }),
  reviewAiRetry: (submissionId: string) => request<ReviewSubmission>(`api/reviews/submissions/${encodeURIComponent(submissionId)}/retry-ai`, { method: 'POST' }),
  reviewBatchAct: (decision: 'approve' | 'reject', items: Array<{ submission_id: string; role_code: string; note?: string; quality_scores?: Record<string, number> }>) => request<{ ok: boolean; decision: string; succeeded: Array<{ submission_id: string; asset_name: string; status: string }>; failed: Array<{ submission_id: string; detail: string }> }>('api/reviews/submissions/batch-act', {
    method: 'POST', body: JSON.stringify({ decision, items }),
  }),
  qianchuanStatus: () => request<QianchuanStatus>('api/qianchuan/status'),
  pushSchemes: () => request<{ items: PushScheme[] }>('api/push-schemes/qianchuan'),
  savePushScheme: (data: { name: string; targets: QianchuanTarget[] }) => request<PushScheme>('api/push-schemes/qianchuan', { method: 'POST', body: JSON.stringify(data) }),
  deletePushScheme: (id: string, revision: number) => request<{ deleted: boolean }>(`api/push-schemes/qianchuan/${encodeURIComponent(id)}?revision=${revision}`, { method: 'DELETE' }),
  pushPreferences: (platform: 'qianchuan' | 'adq') => request<{ items: PushPreference[]; total: number }>(`api/push-preferences/${platform}`),
  togglePushPreference: (platform: 'qianchuan' | 'adq', data: { account_id: string; account_name: string; target_id?: string; target_name?: string; target_type?: string }) => request<PushPreference>(`api/push-preferences/${platform}/toggle`, {
    method: 'POST', body: JSON.stringify(data),
  }),
  qianchuanAuthorize: () => request<{ url: string }>('api/qianchuan/oauth/start'),
  qianchuanAccounts: () => request<{ items: QianchuanAccount[]; total: number }>('api/qianchuan/accounts'),
  qianchuanProductPlanMap: () => request<QianchuanProductPlanMap>('api/qianchuan/product-plan-map'),
  qianchuanPlans: (
    advertiserId: string,
    q = '',
    scope: 'all' | 'multiplication' | 'full_domain' | 'standard' = 'all',
    refresh = false,
    cachedOnly = false,
  ) => request<QianchuanPlanResult>(`api/qianchuan/plans?advertiser_id=${encodeURIComponent(advertiserId)}&q=${encodeURIComponent(q)}&scope=${scope}&refresh=${refresh ? 'true' : 'false'}&cached_only=${cachedOnly ? 'true' : 'false'}`),
  qianchuanPlanMaterials: (advertiserId: string, planId: string, startDate = '', endDate = '') => {
    const params = new URLSearchParams({ advertiser_id: advertiserId, plan_id: planId })
    if (startDate) params.set('start_date', startDate)
    if (endDate) params.set('end_date', endDate)
    return request<QianchuanPlanMaterialResult>(`api/qianchuan/plan-materials?${params}`)
  },
  qianchuanPush: (assetIds: number[], targets: QianchuanTarget[]) => request<{ batch_id: string; task_ids: string[]; asset_count: number; target_count: number; new_task_count: number; status: string }>('api/qianchuan/push', {
    method: 'POST', body: JSON.stringify({ asset_ids: assetIds, targets }),
  }),
  qianchuanTasks: (assetId?: number, q = '', status = 'all', page = 1, pageSize: 5 | 10 | 50 | 100 = 10, startDate = '', endDate = '') => {
    const params = new URLSearchParams()
    if (assetId) params.set('asset_id', String(assetId))
    if (q.trim()) params.set('q', q.trim())
    if (status !== 'all') params.set('status', status)
    params.set('page', String(page))
    params.set('page_size', String(pageSize))
    if (startDate) params.set('push_start_date', startDate)
    if (endDate) params.set('push_end_date', endDate)
    const suffix = params.toString()
    return request<{ items: QianchuanTask[]; total: number; page: number; page_size: number; total_pages: number; viewer_scope: 'personal' | 'all' }>(`api/qianchuan/tasks${suffix ? `?${suffix}` : ''}`)
  },
  qianchuanRetry: (taskId: string) => request<{ status: string; task_id: string }>(`api/qianchuan/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST' }),
  qianchuanCancel: (taskId: string) => request<{ status: string; task_id: string; message: string }>(`api/qianchuan/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),
  qianchuanRetryBatch: (batchId: string) => request<{
    status: string
    batch_id: string
    queued_count: number
    queued_task_ids: string[]
    preserved_success_count: number
    skipped_count: number
    skipped: { task_id: string; reason: string }[]
    message: string
  }>(`api/qianchuan/batches/${encodeURIComponent(batchId)}/retry-failed`, { method: 'POST' }),
  qianchuanDeleteTask: (taskId: string) => request<{ ok: boolean; task_id: string; message: string }>(`api/qianchuan/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
  qianchuanSyncMetrics: (taskIds: string[] = [], startDate?: string, endDate?: string) => request<{ status: string; task_ids: string[]; message: string; next_available_at?: string | null }>('api/qianchuan/metrics/sync', {
    method: 'POST', body: JSON.stringify({ task_ids: taskIds, start_date: startDate || null, end_date: endDate || null }),
  }),
  adqStatus: () => request<AdqStatus>('api/adq/status'),
  adqUserAuthorizationStart: () => request<{ url: string }>('api/adq/user-authorization/start'),
  adqSharedConfig: () => request<{ configured: boolean; authorized: boolean; source_account_id: string; mdm_id: string; scope: string; asset_type: string; original_only: boolean; max_video_mb: number; business_units: { id: string; name: string }[]; known_account_total: number; account_scope_source: string }>('api/adq/shared-library/config'),
  adqSharedUpload: (assetIds: number[]) => request<{ batch_id: string; task_ids: string[]; reused_task_ids: string[]; asset_count: number; new_task_count: number; status: string; message: string }>('api/adq/shared-library/upload', {
    method: 'POST', body: JSON.stringify({ asset_ids: assetIds }),
  }),
  adqAccounts: () => request<AdqAccountCatalog>('api/adq/accounts'),
  adqHierarchy: (q = '', refresh = false) => {
    const params = new URLSearchParams()
    if (q.trim()) params.set('q', q.trim())
    if (refresh) params.set('refresh', 'true')
    return request<AdqHierarchy>(`api/adq/hierarchy${params.size ? `?${params}` : ''}`)
  },
  adqAdgroups: (accountId: string, q = '') => request<{ items: AdqAdgroup[]; total: number; source_total: number; attachable_total: number; complete: boolean; message: string }>(`api/adq/adgroups?account_id=${encodeURIComponent(accountId)}&q=${encodeURIComponent(q)}`),
  adqAccountMaterialMetrics: (accountId: string, startDate = '', endDate = '', q = '', page = 1, pageSize = 20) => {
    const params = new URLSearchParams({ account_id: accountId, page: String(page), page_size: String(pageSize) })
    if (startDate) params.set('start_date', startDate)
    if (endDate) params.set('end_date', endDate)
    if (q.trim()) params.set('q', q.trim())
    return request<AdqAccountMaterialMetrics>(`api/adq/account-material-metrics?${params}`)
  },
  adqPush: (assetIds: number[], targets: AdqTarget[]) => request<{ batch_id: string; task_ids: string[]; asset_count: number; target_count: number; new_task_count: number; status: string }>('api/adq/push', {
    method: 'POST', body: JSON.stringify({ asset_ids: assetIds, targets }),
  }),
  adqTasks: (assetId?: number, q = '', status = 'all', page = 1, pageSize: 5 | 10 = 10, libraryOnly = false, startDate = '', endDate = '') => {
    const params = new URLSearchParams()
    if (assetId) params.set('asset_id', String(assetId))
    if (q.trim()) params.set('q', q.trim())
    if (status !== 'all') params.set('status', status)
    params.set('page', String(page))
    params.set('page_size', String(pageSize))
    if (libraryOnly) params.set('library_only', 'true')
    if (startDate) params.set('push_start_date', startDate)
    if (endDate) params.set('push_end_date', endDate)
    return request<{ items: AdqTask[]; total: number; page: number; page_size: number; total_pages: number; viewer_scope: 'personal' | 'all' }>(`api/adq/tasks?${params}`)
  },
  adqRetry: (taskId: string) => request<{ status: string; task_id: string }>(`api/adq/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST' }),
  adqRetryBatch: (batchId: string) => request<{ status: string; batch_id: string; queued_count: number; queued_task_ids: string[]; preserved_success_count: number; skipped_count: number; skipped: { task_id: string; reason: string }[]; message: string }>(`api/adq/batches/${encodeURIComponent(batchId)}/retry-failed`, { method: 'POST' }),
  adqDeleteTask: (taskId: string) => request<{ ok: boolean; task_id: string; message: string }>(`api/adq/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
  adqSyncMetrics: (taskIds: string[] = [], startDate?: string, endDate?: string) => request<{ status: string; task_ids: string[]; message: string; next_available_at?: string | null }>('api/adq/metrics/sync', {
    method: 'POST', body: JSON.stringify({ task_ids: taskIds, start_date: startDate || null, end_date: endDate || null }),
  }),
  channelsStatus: () => request<ChannelsStatus>('api/channels/status'),
  channelsAuthStart: () => request<ChannelsAuthSession>('api/channels/auth/start', { method: 'POST' }),
  channelsAuthSession: (sessionId: string) => request<ChannelsAuthSession>(`api/channels/auth/sessions/${encodeURIComponent(sessionId)}`),
  channelsAuthInteract: (sessionId: string, x: number, y: number, action: 'click' | 'scroll' | 'select' | 'select_account' | 'scroll_accounts' = 'click', deltaY = 0, choiceId = '') => request<ChannelsAuthSession>(`api/channels/auth/sessions/${encodeURIComponent(sessionId)}/interact`, {
    method: 'POST', body: JSON.stringify({ action, x, y, delta_y: deltaY, choice_id: choiceId }),
  }),
  channelsQrUrl: (sessionId: string, revision = 0) => `api/channels/auth/sessions/${encodeURIComponent(sessionId)}/qr?revision=${Math.max(0, revision)}`,
  channelsAccounts: () => request<{ items: ChannelsAccount[]; total: number }>('api/channels/accounts'),
  channelsAccountDelete: (accountId: string) => request<{ ok: boolean; account_id: string }>(`api/channels/accounts/${encodeURIComponent(accountId)}`, { method: 'DELETE' }),
  channelsPromotionAuthStatus: (accountId: string) => request<ChannelsPromotionAuth>(`api/channels/accounts/${encodeURIComponent(accountId)}/promotion-auth`),
  channelsPromotionAuthStart: (accountId: string) => request<ChannelsPromotionAuthSession>(`api/channels/accounts/${encodeURIComponent(accountId)}/promotion-auth/start`, { method: 'POST' }),
  channelsPromotionAuthSession: (sessionId: string) => request<ChannelsPromotionAuthSession>(`api/channels/promotion-auth/sessions/${encodeURIComponent(sessionId)}`),
  channelsPromotionCaptureUrl: (sessionId: string, revision = 0) => `api/channels/promotion-auth/sessions/${encodeURIComponent(sessionId)}/capture?revision=${Math.max(0, revision)}`,
  channelsProducts: (accountId: string, q = '', refresh = false) => {
    const params = new URLSearchParams()
    if (q.trim()) params.set('q', q.trim())
    if (refresh) params.set('refresh', 'true')
    return request<{
      state: 'loading' | 'refreshing' | 'ready' | 'error'
      items: ChannelsProduct[]
      total: number
      cached_total: number
      cached: boolean
      stale: boolean
      message: string
      error: string
      stage: string
      attempt: number
      max_attempts: number
      retry_after_ms: number
      account_id: string
      account_name: string
      source: string
      read_at: string
      complete: boolean
      expected_total: number
      page_count: number
    }>(`api/channels/accounts/${encodeURIComponent(accountId)}/products?${params}`)
  },
  channelsPush: (assetIds: number[], accountIds: string[], title = '', description = '', tags: string[] = [], product?: ChannelsProduct | null, covers: { asset_id: number; object_key: string; filename: string }[] = [], titles: { asset_id: number; title: string }[] = [], annotations: ChannelsVideoAnnotationInput[] = [], descriptions: { asset_id: number; description: string }[] = []) => request<{ batch_id: string; task_ids: string[]; duplicate_task_ids: string[]; asset_count: number; account_count: number; status: string; message: string }>('api/channels/push', {
    method: 'POST', body: JSON.stringify({ asset_ids: assetIds, account_ids: accountIds, title, description, tags, product_id: product?.id || '', product_name: product?.name || '', covers, titles, annotations, descriptions }),
  }),
  channelsTasks: (q = '', status = 'all', page = 1, pageSize: 5 | 10 = 10, scope: 'mine' | 'all' = 'mine', startDate = '', endDate = '') => {
    const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), scope })
    if (q.trim()) params.set('q', q.trim())
    if (status !== 'all') params.set('status', status)
    if (startDate) params.set('push_start_date', startDate)
    if (endDate) params.set('push_end_date', endDate)
    return request<{ items: ChannelsTask[]; total: number; page: number; page_size: number; total_pages: number; viewer_scope: 'personal' | 'all'; can_view_all: boolean }>(`api/channels/tasks?${params}`)
  },
  channelsSyncMetrics: (taskIds: string[] = []) => request<{ status: string; task_ids: string[]; message: string }>('api/channels/metrics/sync', {
    method: 'POST', body: JSON.stringify({ task_ids: taskIds }),
  }),
  channelsEdit: (taskId: string, data: { title: string; description: string; product_id: string; product_name: string; cover_object_key?: string; cover_filename?: string; retry: boolean }) => request<{ status: string; task_id: string; message: string }>(`api/channels/tasks/${encodeURIComponent(taskId)}`, { method: 'PATCH', body: JSON.stringify(data) }),
  channelsPromotionQuote: (taskId: string, data: ChannelsPromotionQuoteInput) => request<ChannelsPromotionQuoteResult>(`api/channels/tasks/${encodeURIComponent(taskId)}/promotion/quote`, {
    method: 'POST', body: JSON.stringify(data),
  }),
  channelsPromotionCreate: (taskId: string, data: ChannelsPromotionQuoteInput & { order_name: string; confirmed: boolean; idempotency_key: string }) => request<ChannelsPromotionOrder>(`api/channels/tasks/${encodeURIComponent(taskId)}/promotion/orders`, {
    method: 'POST', body: JSON.stringify(data),
  }),
  channelsPromotionOrders: (taskId: string) => request<{ items: ChannelsPromotionOrder[]; total: number }>(`api/channels/tasks/${encodeURIComponent(taskId)}/promotion/orders`),
  channelsPromotionPaymentSessionCreate: (taskId: string, orderId: string, requestToken: string) => request<ChannelsPromotionPaymentSession>(`api/channels/tasks/${encodeURIComponent(taskId)}/promotion/orders/${encodeURIComponent(orderId)}/payment-sessions`, { method: 'POST', body: JSON.stringify({ request_token: requestToken }) }),
  channelsPromotionPaymentSessionGet: (taskId: string, orderId: string, sessionId: string) => request<ChannelsPromotionPaymentSession>(`api/channels/tasks/${encodeURIComponent(taskId)}/promotion/orders/${encodeURIComponent(orderId)}/payment-sessions/${encodeURIComponent(sessionId)}`),
  channelsPromotionPaymentSessionEvent: (taskId: string, orderId: string, sessionId: string, action: 'consumeSuccess' | 'consumeError' | 'consumeClose' | 'consumeStatusChange', message = '') => request<ChannelsPromotionPaymentSession>(`api/channels/tasks/${encodeURIComponent(taskId)}/promotion/orders/${encodeURIComponent(orderId)}/payment-sessions/${encodeURIComponent(sessionId)}/events`, { method: 'POST', body: JSON.stringify({ action, message }) }),
  channelsPromotionSync: (taskId: string, orderId: string) => request<ChannelsPromotionOrder>(`api/channels/tasks/${encodeURIComponent(taskId)}/promotion/orders/${encodeURIComponent(orderId)}/sync`, { method: 'POST' }),
  channelsRetry: (taskId: string) => request<{ status: string; task_id: string }>(`api/channels/tasks/${encodeURIComponent(taskId)}/retry`, { method: 'POST' }),
  channelsCancel: (taskId: string) => request<{ status: string; task_id: string; message: string }>(`api/channels/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST' }),
  channelsDeleteTask: (taskId: string) => request<{ ok: boolean; task_id: string; message: string }>(`api/channels/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' }),
  videoRequests: (scope: 'mine' | 'assigned' | 'all' = 'mine', status = 'all', q = '', page = 1, pageSize = 10) => {
    const params = new URLSearchParams({ scope, status, q, page: String(page), page_size: String(pageSize) })
    return request<VideoRequestPage>(`api/video-requests?${params}`)
  },
  videoRequest: (requestId: string) => request<VideoRequestItem>(`api/video-requests/${encodeURIComponent(requestId)}`),
  videoRequestReferencePreviews: (requestId: string) => request<{ status: 'empty' | 'ready' | 'processing' | 'queued' | string; request_id: string }>(`api/video-requests/${encodeURIComponent(requestId)}/reference-previews`, { method: 'POST' }),
  videoRequestCreate: (data: { product: string; description: string; reference_url?: string; reference_video_key?: string; reference_video_name?: string; reference_videos?: { object_key: string; filename: string }[]; reference_images?: { object_key: string; filename: string }[] }) =>
    request<VideoRequestItem>('api/video-requests', { method: 'POST', body: JSON.stringify(data) }),
  videoRequestAssignees: (q = '') => request<{ items: VideoRequestAssignee[]; total: number }>(`api/video-requests/assignees?q=${encodeURIComponent(q)}`),
  videoRequestAssign: (requestId: string, assignee: VideoRequestAssignee) => request<VideoRequestItem>(`api/video-requests/${encodeURIComponent(requestId)}/assign`, {
    method: 'POST', body: JSON.stringify({ assignee_number: assignee.number, assignee_name: assignee.name }),
  }),
  videoRequestStart: (requestId: string) => request<VideoRequestItem>(`api/video-requests/${encodeURIComponent(requestId)}/start`, { method: 'POST' }),
  videoRequestDeliver: (requestId: string, assetIds: number[], note = '') => request<VideoRequestItem>(`api/video-requests/${encodeURIComponent(requestId)}/deliver`, {
    method: 'POST', body: JSON.stringify({ asset_ids: assetIds, note }),
  }),
  videoRequestReturn: (requestId: string, reason: string) => request<VideoRequestItem>(`api/video-requests/${encodeURIComponent(requestId)}/return`, {
    method: 'POST', body: JSON.stringify({ reason }),
  }),
  videoRequestAccept: (requestId: string) => request<VideoRequestItem>(`api/video-requests/${encodeURIComponent(requestId)}/accept`, { method: 'POST' }),
  videoRequestRevision: (requestId: string, feedback: string) => request<VideoRequestItem>(`api/video-requests/${encodeURIComponent(requestId)}/revision`, {
    method: 'POST', body: JSON.stringify({ feedback }),
  }),
  notifications: (limit = 20) => request<{ items: UserNotification[]; unread: number }>(`api/notifications?limit=${limit}`),
  notificationRead: (id: number) => request<{ ok: boolean; id: number }>(`api/notifications/${id}/read`, { method: 'POST' }),
  notificationsReadAll: () => request<{ ok: boolean; count: number }>('api/notifications/read-all', { method: 'POST' }),
}
