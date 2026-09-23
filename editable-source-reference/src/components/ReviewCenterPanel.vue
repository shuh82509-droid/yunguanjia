<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { BadgeCheck, Check, CircleAlert, Clock3, ExternalLink, Play, Plus, RefreshCw, Save, Search, ShieldAlert, ShieldCheck, Trash2, UserPlus, Users, X } from 'lucide-vue-next'
import { api } from '../api'
import type { Asset, OaPermissions, ReviewAiRule, ReviewEligibleReviewer, ReviewNamingContext, ReviewNamingEvidence, ReviewSubmission, ReviewWorkflowConfig } from '../types'
import PaginationControls from './PaginationControls.vue'
import ReviewerAssignmentPicker from './ReviewerAssignmentPicker.vue'

const props = defineProps<{ mode: 'submit' | 'queue' | 'config'; permissions: OaPermissions }>()
const emit = defineEmits<{ changed: [] }>()

const config = ref<ReviewWorkflowConfig | null>(null)
const queueReadError = ref(false)
const queueHasLoaded = ref(false)
const submissions = ref<ReviewSubmission[]>([])
const total = ref(0)
const page = ref(1)
const totalPages = ref(1)
const q = ref('')
const reviewerQ = ref('')
const status = ref('all')
const loading = ref(true)
const busy = ref('')
const error = ref('')
const success = ref('')
const actionNotes = reactive<Record<string, string>>({})
const qualityScores = reactive<Record<string, Record<string, number>>>({})
const previewErrors = reactive<Record<string, boolean>>({})
let refreshTimer = 0
let queueRequestVersion = 0
let queueRequestInFlight = false
let disposed = false
const playingReviewIds = new Set<string>()

const assetQ = ref('')
const assetResults = ref<Asset[]>([])
const selectedAsset = ref<Asset | null>(null)
const selectedAssets = ref<Asset[]>([])
const previewAsset = ref<Asset | null>(null)
const submitNote = ref('')
const assignmentMode = ref<'organization' | 'designated'>('organization')
const selectedReviewer = ref<ReviewEligibleReviewer | null>(null)
const namingContext = ref<ReviewNamingContext | null>(null)
const namingEvidence = ref<ReviewNamingEvidence[]>([])
const noApplicableSources = ref(false)
const namingDrafts = reactive<Record<number, { evidence: ReviewNamingEvidence[]; noApplicable: boolean; loaded: boolean }>>({})
const namingLoading = ref(false)
const assetSearching = ref(false)
const assetSearched = ref(false)
const assetPage = ref(1)
const assetTotal = ref(0)
const assetTotalPages = ref(1)
const assetPageSize = 8

const roleForm = reactive({ role_code: 'member', user_name: '', user_number: '', department: '', center: '', group_name: '' })
const requiredRoles = ref<string[]>([])
const workflowEnabled = ref(false)
const namingReviewEnabled = ref(false)
const aiRedlineEnabled = ref(false)
const redlineRules = ref<ReviewAiRule[]>([])
const selectedSubmissionIds = ref<string[]>([])
const batchNote = ref('')

const roleLabels: Record<string, string> = {
  member: '组员', team_lead: '组长', supervisor: '主管', designated_reviewer: '指定审核人', brand_tone: '品牌调性（预留）', director: '总监（历史）', internal_control: '内控（历史）',
}
const statusLabel = (value: string) => value === 'approved' ? '已通过' : value === 'rejected' ? '已驳回' : '审核中'
const aiStatusLabel = (value: string) => ({ disabled: '已关闭', pending: '排队中', processing: '理解视频中', passed: '未见明显风险', warning: '有审核建议', rejected: '历史高风险', error: '识别失败', legacy_skipped: '历史流程' }[value] || value)
const aiCategoryLabel = (value: string) => ({ platform: '平台风险', internal: '内控风险', artist: '艺人及版权', relaxation: '放宽规则' }[value] || value)
const redlineCategories = [
  { code: 'platform', label: '平台风险', description: '平台规则、交易秩序与内容安全' },
  { code: 'internal', label: '内控风险', description: '功效承诺、品牌规范与画面质量' },
  { code: 'artist', label: '艺人及版权', description: '肖像、声音、换脸与内容版权' },
  { code: 'relaxation', label: '本土美妆放宽规则', description: '常态 8 类放宽边界与仍需重点核对的情形' },
] as const
const redlineRulesByCategory = (category: string) => redlineRules.value.filter(rule => rule.enabled && rule.category === category)
const patternSummary = (value: string) => value.replace(/\\s\*|\\s\+|\(\?:|[(){}?]/g, '').replace(/\|/g, '、').slice(0, 180)
const seconds = (value: number) => `${Math.floor(value / 60).toString().padStart(2, '0')}:${Math.floor(value % 60).toString().padStart(2, '0')}`
const time = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '—'
const activeRequiredLabels = computed(() => {
  const stages = [
    ...(namingReviewEnabled.value ? ['命名规范'] : []),
    ...(aiRedlineEnabled.value ? ['AI建议（不拦截）'] : []),
    ...(workflowEnabled.value ? requiredRoles.value.map(role => roleLabels[role]) : []),
  ]
  return stages.length ? stages.join(' → ') : '无（当前全部关闭）'
})
const anyReviewGateEnabled = computed(() => namingReviewEnabled.value || workflowEnabled.value)
const anyReviewFeatureEnabled = computed(() => anyReviewGateEnabled.value || aiRedlineEnabled.value)
const pageTitle = computed(() => props.mode === 'config' ? '审核角色配置' : props.mode === 'submit' ? '提交视频审核' : '视频审核中心')
const pageDescription = computed(() => props.mode === 'config'
  ? '配置命名规范、AI审核建议、组长和主管；AI只提供参考，最终由审核人决定。'
  : props.mode === 'submit'
    ? '默认展示本人上传的视频，可搜索、翻页并处理被驳回素材。'
    : 'AI给出风险与放宽建议，指定审核人查看完整素材后作出最终决定。')
const evidenceReady = (evidence: ReviewNamingEvidence[], noApplicable: boolean) => noApplicable || (
  evidence.length > 0 && evidence.every(item => Boolean(item.category && item.material_name.trim() && item.usage && item.position))
)
const namingReady = computed(() => {
  if (!namingReviewEnabled.value) return true
  return selectedAssets.value.every(asset => {
    if (asset.library_type !== 'remix') return true
    if (selectedAsset.value?.id === asset.id) return !namingLoading.value && evidenceReady(namingEvidence.value, noApplicableSources.value)
    const draft = namingDrafts[asset.id]
    return Boolean(draft?.loaded && evidenceReady(draft.evidence, draft.noApplicable))
  })
})
const incompleteNamingAssets = computed(() => {
  if (!namingReviewEnabled.value) return []
  return selectedAssets.value.filter(asset => {
    if (asset.library_type !== 'remix') return false
    if (selectedAsset.value?.id === asset.id) return namingLoading.value || !evidenceReady(namingEvidence.value, noApplicableSources.value)
    const draft = namingDrafts[asset.id]
    return !draft?.loaded || !evidenceReady(draft.evidence, draft.noApplicable)
  })
})
const selectedRemixAssets = computed(() => selectedAssets.value.filter(asset => asset.library_type === 'remix'))
const batchNoApplicableConfirmed = computed(() => selectedRemixAssets.value.length > 0 && selectedRemixAssets.value.every(asset => {
  if (selectedAsset.value?.id === asset.id) return noApplicableSources.value
  return Boolean(namingDrafts[asset.id]?.noApplicable)
}))
const batchSelectedItems = computed(() => submissions.value.filter(item => selectedSubmissionIds.value.includes(item.id) && item.can_review))
const reviewerNames = computed(() => Array.from(new Set(
  (config.value?.roles || []).filter(role => role.is_stage).flatMap(role => role.members.map(member => member.user_name)).filter(Boolean),
)))

const ensureQualityScores = (submissionId: string) => {
  if (!qualityScores[submissionId]) qualityScores[submissionId] = { hook: 20, selling_point: 20, rhythm: 20, production: 20 }
  return qualityScores[submissionId]
}
const qualityTotal = (submissionId: string) => Object.values(ensureQualityScores(submissionId)).reduce((sum, value) => sum + Number(value || 0), 0)

const normalizeSubmission = (item: ReviewSubmission): ReviewSubmission => {
  const raw = item as ReviewSubmission & Record<string, any>
  const naming = raw.naming_check || {}
  const ai = raw.ai_review || {}
  return {
    ...item,
    naming_evidence: Array.isArray(raw.naming_evidence) ? raw.naming_evidence : [],
    naming_check: {
      status: naming.status || 'failed',
      version: naming.version || '',
      document_url: naming.document_url || '',
      filename: naming.filename || item.asset_name,
      message: naming.message || '历史审核记录缺少命名明细，请重新提审或由审核人核对。',
      no_applicable_sources: Boolean(naming.no_applicable_sources),
      evidence_count: Number(naming.evidence_count || 0),
      required_names: Array.isArray(naming.required_names) ? naming.required_names : [],
      missing_names: Array.isArray(naming.missing_names) ? naming.missing_names : [],
      pending_categories: Array.isArray(naming.pending_categories) ? naming.pending_categories : [],
    },
    ai_review: {
      status: ai.status || 'legacy_skipped',
      summary: ai.summary || '该任务没有AI建议记录，由审核人直接作出决定。',
      provider: ai.provider || '',
      task_id: ai.task_id || '',
      rule_version: ai.rule_version || '',
      category_counts: {
        platform: Number(ai.category_counts?.platform || 0),
        internal: Number(ai.category_counts?.internal || 0),
        artist: Number(ai.category_counts?.artist || 0),
        relaxation: Number(ai.category_counts?.relaxation || 0),
      },
      findings: Array.isArray(ai.findings) ? ai.findings : [],
      segments: Array.isArray(ai.segments) ? ai.segments : [],
      error_message: ai.error_message || '',
      retry_count: Number(ai.retry_count || 0),
      started_at: ai.started_at || null,
      completed_at: ai.completed_at || null,
    },
    decisions: Array.isArray(raw.decisions) ? raw.decisions : [],
  }
}

const loadConfig = async () => {
  config.value = await api.reviewConfig()
  requiredRoles.value = [...config.value.required_roles]
  workflowEnabled.value = config.value.enabled
  namingReviewEnabled.value = config.value.naming_standard.enabled
  aiRedlineEnabled.value = config.value.ai_review_enabled
  redlineRules.value = config.value.ai_redlines.rules.map(rule => ({ ...rule }))
}

const loadQueue = async (background = false) => {
  if (background && (queueRequestInFlight || busy.value || previewAsset.value || playingReviewIds.size > 0 || document.hidden)) return
  const requestVersion = ++queueRequestVersion
  queueRequestInFlight = true
  const requestedMode = props.mode
  if (!background) { loading.value = true; error.value = '' }
  try {
    const data = await api.reviewSubmissions(q.value, status.value, page.value, 20, reviewerQ.value)
    if (disposed || requestVersion !== queueRequestVersion || requestedMode !== props.mode) return
    // Playback may have begun while an already-running refresh was awaiting the API.
    if (background && (playingReviewIds.size > 0 || previewAsset.value || busy.value)) return
    queueReadError.value = false
    queueHasLoaded.value = true
    submissions.value = data.items.map(normalizeSubmission)
    submissions.value.forEach(item => ensureQualityScores(item.id))
    total.value = data.total
    totalPages.value = data.total_pages
    config.value = data.workflow
    workflowEnabled.value = data.workflow.enabled
    namingReviewEnabled.value = data.workflow.naming_standard.enabled
    aiRedlineEnabled.value = data.workflow.ai_review_enabled
    selectedSubmissionIds.value = selectedSubmissionIds.value.filter(id => data.items.some(item => item.id === id && item.can_review))
  } catch (e) {
    if (!disposed && requestVersion === queueRequestVersion) {
      queueReadError.value = true
      error.value = e instanceof Error ? e.message : '审核任务读取失败'
    }
  }
  finally {
    if (requestVersion === queueRequestVersion) { queueRequestInFlight = false; loading.value = false }
  }
}

const changeQueuePage = async (targetPage: number) => {
  if (loading.value) return
  const nextPage = Math.min(totalPages.value, Math.max(1, Math.trunc(targetPage)))
  if (nextPage === page.value) return
  playingReviewIds.clear()
  page.value = nextPage
  await loadQueue()
}

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    if (props.mode === 'queue') await loadQueue()
    else {
      await loadConfig()
      if (props.mode === 'submit') await searchAssets(1)
    }
  } catch (e) { error.value = e instanceof Error ? e.message : '审核模块读取失败' }
  finally { loading.value = false }
}

const searchAssets = async (targetPage = 1) => {
  assetSearching.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ q: assetQ.value.trim(), media_type: 'video', asset_scope: 'marketing_video', mine_only: 'true', page: String(targetPage), page_size: String(assetPageSize), sort: 'newest' })
    const data = await api.assets(params)
    assetResults.value = data.items
    assetPage.value = data.page
    assetTotal.value = data.total
    assetTotalPages.value = Math.max(1, Math.ceil(data.total / data.page_size))
    assetSearched.value = true
  } catch (e) { error.value = e instanceof Error ? e.message : '素材搜索失败' }
  finally { assetSearching.value = false }
}

const blankNamingEvidence = (suggestion?: ReviewNamingContext['source_suggestions'][number]): ReviewNamingEvidence => ({
  category: '',
  material_name: suggestion?.material_name || '',
  usage: '',
  position: '',
  source_asset_id: suggestion?.source_asset_id || null,
})

const resetNaming = () => {
  namingContext.value = null
  namingEvidence.value = []
  noApplicableSources.value = false
}

const saveActiveNamingDraft = () => {
  if (!selectedAsset.value || selectedAsset.value.library_type !== 'remix') return
  namingDrafts[selectedAsset.value.id] = {
    evidence: namingEvidence.value.map(item => ({ ...item })),
    noApplicable: noApplicableSources.value,
    loaded: Boolean(namingContext.value) || noApplicableSources.value || Boolean(namingDrafts[selectedAsset.value.id]?.loaded),
  }
}

const activateAsset = (asset: Asset) => {
  if (selectedAsset.value?.id === asset.id) return
  saveActiveNamingDraft()
  selectedAsset.value = asset
  resetNaming()
  const draft = namingDrafts[asset.id]
  if (draft) {
    namingEvidence.value = draft.evidence.map(item => ({ ...item }))
    noApplicableSources.value = draft.noApplicable
  }
  if (asset.library_type === 'remix' && !draft?.loaded) void loadNamingContext(asset)
}

const loadNamingContext = async (asset: Asset) => {
  if (asset.library_type !== 'remix') return
  namingLoading.value = true
  try {
    const context = await api.reviewNamingContext(asset.id)
    if (selectedAsset.value?.id !== asset.id) return
    namingContext.value = context
    namingEvidence.value = context.source_suggestions.length
      ? context.source_suggestions.map(item => blankNamingEvidence(item))
      : [blankNamingEvidence()]
    saveActiveNamingDraft()
  } catch (e) { error.value = e instanceof Error ? e.message : '命名依据读取失败' }
  finally { namingLoading.value = false }
}

const toggleAsset = (asset: Asset) => {
  const alreadySelected = selectedAssets.value.some(item => item.id === asset.id)
  if (alreadySelected) {
    saveActiveNamingDraft()
    selectedAssets.value = selectedAssets.value.filter(item => item.id !== asset.id)
    delete namingDrafts[asset.id]
    if (selectedAsset.value?.id === asset.id) {
      selectedAsset.value = null
      resetNaming()
      const next = selectedAssets.value[selectedAssets.value.length - 1]
      if (next) activateAsset(next)
    }
    return
  }
  selectedAssets.value = [...selectedAssets.value, asset]
  activateAsset(asset)
}

const selectAllAssetsOnPage = () => {
  const byId = new Map(selectedAssets.value.map(asset => [asset.id, asset]))
  assetResults.value.forEach(asset => byId.set(asset.id, asset))
  selectedAssets.value = Array.from(byId.values()).slice(0, 100)
  const next = selectedAssets.value[selectedAssets.value.length - 1]
  if (next) activateAsset(next)
}

const clearSelectedAssets = () => {
  selectedAssets.value = []
  selectedAsset.value = null
  resetNaming()
}

const confirmBatchNoApplicable = () => {
  selectedRemixAssets.value.forEach(asset => {
    namingDrafts[asset.id] = { evidence: [], noApplicable: true, loaded: true }
  })
  if (selectedAsset.value?.library_type === 'remix') {
    namingEvidence.value = []
    noApplicableSources.value = true
  }
  error.value = ''
  success.value = `已为本批 ${selectedRemixAssets.value.length} 条混剪确认“未使用前四类素材”；现在可以直接批量提审。`
}

const addNamingEvidence = () => namingEvidence.value.push(blankNamingEvidence())
const removeNamingEvidence = (index: number) => {
  namingEvidence.value.splice(index, 1)
  if (!namingEvidence.value.length) namingEvidence.value.push(blankNamingEvidence())
}

const openAssetPreview = (asset: Asset) => {
  if (asset.preview_url) previewAsset.value = asset
}

const closeAssetPreview = () => { previewAsset.value = null }
const handlePreviewEscape = (event: KeyboardEvent) => {
  if (event.key === 'Escape' && previewAsset.value) closeAssetPreview()
}

const submitReview = async () => {
  if (!selectedAssets.value.length) return
  if (assignmentMode.value === 'designated' && !selectedReviewer.value) { error.value = '请先选择指定审核人'; return }
  saveActiveNamingDraft()
  if (!namingReady.value) {
    const pending = incompleteNamingAssets.value
    const first = pending[0]
    if (first) activateAsset(first)
    const names = pending.slice(0, 3).map(asset => asset.filename).join('、')
    const suffix = pending.length > 3 ? `等 ${pending.length} 条` : ''
    error.value = `还有素材未完成命名依据确认：${names}${suffix}。请逐条填写依据，或确认“未使用前四类素材”。`
    return
  }
  busy.value = 'submit-batch'
  error.value = ''; success.value = ''
  try {
    const result = await api.reviewBatchSubmit(selectedAssets.value.map(asset => {
      const draft = namingDrafts[asset.id]
      return {
        asset_id: asset.id,
        note: submitNote.value,
        naming_evidence: asset.library_type === 'remix' && draft && !draft.noApplicable ? draft.evidence : [],
        no_applicable_sources: asset.library_type === 'remix' && Boolean(draft?.noApplicable),
        assignment_mode: assignmentMode.value,
        designated_reviewer_number: selectedReviewer.value?.user_number || '',
        designated_reviewer_name: selectedReviewer.value?.user_name || '',
      }
    }))
    const failedIds = new Set(result.failed.map(item => item.asset_id))
    const remaining = selectedAssets.value.filter(asset => failedIds.has(asset.id))
    const reviewerNames = Array.from(new Set(result.succeeded.flatMap(item => item.current_reviewers.map(reviewer => reviewer.user_name)))).filter(Boolean)
    const routeText = reviewerNames.length ? `；审核人：${reviewerNames.join('、')}` : ''
    const failedText = result.failed.length ? `；${result.failed.length} 条未提交：${result.failed[0].detail}` : ''
    success.value = `已批量提审 ${result.succeeded.length} 条${routeText}${failedText}`
    emit('changed')
    selectedAssets.value = remaining
    selectedAsset.value = null
    resetNaming()
    if (!remaining.length) submitNote.value = ''
    if (!remaining.length) { assignmentMode.value = 'organization'; selectedReviewer.value = null }
    const next = remaining[remaining.length - 1]
    if (next) activateAsset(next)
    if (props.mode === 'queue') await loadQueue()
    else { await loadConfig(); await searchAssets(assetPage.value) }
  } catch (e) { error.value = e instanceof Error ? e.message : '提交审核失败' }
  finally { busy.value = '' }
}

const toggleSubmission = (item: ReviewSubmission) => {
  if (!item.can_review) return
  selectedSubmissionIds.value = selectedSubmissionIds.value.includes(item.id)
    ? selectedSubmissionIds.value.filter(id => id !== item.id)
    : [...selectedSubmissionIds.value, item.id]
}

const toggleAllReviewable = () => {
  const ids = submissions.value.filter(item => item.can_review).map(item => item.id)
  const allSelected = ids.length > 0 && ids.every(id => selectedSubmissionIds.value.includes(id))
  selectedSubmissionIds.value = allSelected ? [] : ids
}

const batchAct = async (decision: 'approve' | 'reject') => {
  if (!batchSelectedItems.value.length) return
  if (decision === 'reject' && !batchNote.value.trim()) { error.value = '批量驳回时请填写统一修改意见'; return }
  busy.value = `batch-${decision}`; error.value = ''; success.value = ''
  try {
    const result = await api.reviewBatchAct(decision, batchSelectedItems.value.map(item => ({
      submission_id: item.id,
      role_code: item.current_role,
      note: batchNote.value.trim(),
      quality_scores: ensureQualityScores(item.id),
    })))
    const failedText = result.failed.length ? `；${result.failed.length} 条未处理：${result.failed[0].detail}` : ''
    success.value = `已批量${decision === 'approve' ? '通过' : '驳回'} ${result.succeeded.length} 条${failedText}`
    selectedSubmissionIds.value = []
    batchNote.value = ''
    emit('changed')
    await loadQueue()
  } catch (e) { error.value = e instanceof Error ? e.message : '批量审核失败' }
  finally { busy.value = '' }
}

const act = async (item: ReviewSubmission, decision: 'approve' | 'reject') => {
  if (!item.current_role) return
  const note = (actionNotes[item.id] || '').trim()
  if (decision === 'reject' && !note) { error.value = '驳回时请填写修改意见'; return }
  busy.value = `${decision}-${item.id}`
  error.value = ''; success.value = ''
  try {
    const scores = ensureQualityScores(item.id)
    if (decision === 'approve') await api.reviewApprove(item.id, item.current_role, note, scores)
    else await api.reviewReject(item.id, item.current_role, note, scores)
    success.value = decision === 'approve' ? `已完成${item.current_role_label}审核。` : '已驳回并记录修改意见。'
    actionNotes[item.id] = ''
    emit('changed')
    await loadQueue()
  } catch (e) { error.value = e instanceof Error ? e.message : '审核操作失败' }
  finally { busy.value = '' }
}

const retryAi = async (item: ReviewSubmission) => {
  busy.value = `ai-${item.id}`; error.value = ''; success.value = ''
  try {
    await api.reviewAiRetry(item.id)
    success.value = 'AI建议已重新进入识别队列；审核人可继续人工审核。'
    emit('changed')
    await loadQueue()
  } catch (e) { error.value = e instanceof Error ? e.message : 'AI审核重试失败' }
  finally { busy.value = '' }
}

const saveWorkflow = async () => {
  busy.value = 'workflow'; error.value = ''; success.value = ''
  try {
    config.value = await api.reviewConfigUpdate(
      workflowEnabled.value,
      namingReviewEnabled.value,
      aiRedlineEnabled.value,
      requiredRoles.value,
    )
    requiredRoles.value = [...config.value.required_roles]
    workflowEnabled.value = config.value.enabled
    namingReviewEnabled.value = config.value.naming_standard.enabled
    aiRedlineEnabled.value = config.value.ai_review_enabled
    const enabledStages = [
      namingReviewEnabled.value ? '命名规范' : '',
      aiRedlineEnabled.value ? 'AI建议（不拦截）' : '',
      workflowEnabled.value ? '人工审核' : '',
    ].filter(Boolean)
    success.value = enabledStages.length
      ? `审核配置已保存；当前启用：${enabledStages.join('、')}。只有命名规范和人工审核会拦截推送。`
      : '审核配置已保存；命名规范、AI建议和人工审核均已关闭。'
  } catch (e) { error.value = e instanceof Error ? e.message : '流程保存失败' }
  finally { busy.value = '' }
}

const addRedlineRule = () => {
  redlineRules.value.push({
    code: '', category: 'platform', severity: 'warning', title: '', pattern: '', enabled: true,
    sort_order: redlineRules.value.length, updated_by_name: '', updated_at: '',
  })
}

const removeRedlineRule = (index: number) => {
  const rule = redlineRules.value[index]
  if (!window.confirm(`确认删除红线规则“${rule.title || '未命名规则'}”？保存后对后续新提审生效。`)) return
  redlineRules.value.splice(index, 1)
}

const saveRedlineRules = async () => {
  busy.value = 'redlines'; error.value = ''; success.value = ''
  try {
    config.value = await api.reviewRedlineRulesUpdate(redlineRules.value)
    redlineRules.value = config.value.ai_redlines.rules.map(rule => ({ ...rule }))
    success.value = `AI审核建议规则已保存为 ${config.value.ai_redlines.version}，后续新提审立即使用。`
  } catch (e) { error.value = e instanceof Error ? e.message : 'AI审核建议规则保存失败' }
  finally { busy.value = '' }
}

const addRole = async () => {
  if (!roleForm.user_name.trim()) return
  busy.value = 'role'; error.value = ''; success.value = ''
  try {
    await api.reviewRoleCreate({ ...roleForm })
    success.value = `${roleForm.user_name} 已配置为${roleLabels[roleForm.role_code]}。`
    roleForm.user_name = ''; roleForm.user_number = ''; roleForm.department = ''; roleForm.center = ''; roleForm.group_name = ''
    await loadConfig()
  } catch (e) { error.value = e instanceof Error ? e.message : '角色配置失败' }
  finally { busy.value = '' }
}

const removeRole = async (id: number) => {
  busy.value = `role-${id}`; error.value = ''; success.value = ''
  try { await api.reviewRoleDelete(id); await loadConfig() }
  catch (e) { error.value = e instanceof Error ? e.message : '移除角色失败' }
  finally { busy.value = '' }
}

watch(() => props.mode, () => { playingReviewIds.clear(); void load() })
watch(status, () => { playingReviewIds.clear(); if (props.mode === 'queue') { page.value = 1; void loadQueue() } })
onMounted(() => {
  window.addEventListener('keydown', handlePreviewEscape)
  void load()
  refreshTimer = window.setInterval(() => {
    if (props.mode === 'queue' && submissions.value.some(item => ['pending', 'processing'].includes(item.ai_review?.status))) void loadQueue(true)
  }, 8000)
})
onBeforeUnmount(() => {
  disposed = true
  queueRequestVersion++
  window.removeEventListener('keydown', handlePreviewEscape)
  if (refreshTimer) window.clearInterval(refreshTimer)
})
</script>

<template>
  <section class="review-center">
    <header class="review-hero">
      <div><span>WIS REVIEW WORKFLOW</span><h2>{{ pageTitle }}</h2><p>{{ pageDescription }}</p></div>
      <div class="review-state" :class="anyReviewFeatureEnabled ? 'enabled' : 'disabled'"><ShieldCheck /><span>审核功能状态</span><strong><i></i>{{ !config ? '状态尚未读取' : anyReviewGateEnabled ? '人工门禁已启用' : aiRedlineEnabled ? '仅AI建议已启用' : '全部关闭' }}</strong></div>
    </header>

    <div v-if="success" class="review-message success"><Check />{{ success }}<button @click="success = ''"><X /></button></div>
    <div v-if="error" class="review-message error"><CircleAlert />{{ error }}<button @click="error = ''"><X /></button></div>

    <template v-if="mode !== 'config'">
      <section v-if="mode === 'submit'" class="review-submit-card">
        <div class="section-title"><span><Play /><b>提交视频审核</b></span><small>默认展示本人上传素材；可搜索、翻页，驳回修改后再次提交。</small></div>
        <div class="asset-search"><label><Search /><input v-model="assetQ" placeholder="输入视频文件名或标签" @keyup.enter="searchAssets(1)" /></label><button :disabled="assetSearching" @click="searchAssets(1)"><RefreshCw :class="{ spin: assetSearching }" />{{ assetSearching ? '搜索中' : '搜索素材' }}</button></div>
        <div v-if="assetSearched" class="asset-result-summary"><strong>我的视频素材</strong><span>共 {{ assetTotal }} 条；可连续选择多条后统一指定审核人</span><div><button type="button" :disabled="!assetResults.length" @click="selectAllAssetsOnPage">全选当前页 {{ assetResults.length }} 条</button><button type="button" :disabled="!selectedAssets.length" @click="clearSelectedAssets">清空已选</button></div></div>
        <div v-if="assetResults.length" class="asset-results">
          <article
            v-for="asset in assetResults"
            :key="asset.id"
            class="asset-result-card"
            :class="{ selected: selectedAssets.some(item => item.id === asset.id) }"
            role="checkbox"
            :aria-checked="selectedAssets.some(item => item.id === asset.id)"
            tabindex="0"
            @click="toggleAsset(asset)"
            @keydown.enter.prevent="toggleAsset(asset)"
            @keydown.space.prevent="toggleAsset(asset)"
          >
            <span class="asset-card-cover">
              <img v-if="asset.cover_url" :src="asset.cover_url" :alt="`${asset.filename} 的封面`" />
              <span v-else class="asset-cover-empty"><Play /><small>封面生成中</small></span>
              <button type="button" class="asset-play-button" :disabled="!asset.preview_url" :aria-label="`播放 ${asset.filename}`" @click.stop="openAssetPreview(asset)"><Play /></button>
              <span v-if="asset.review_status === 'rejected'" class="asset-review-badge rejected"><i></i>已驳回</span>
              <span v-else-if="asset.review_status === 'pending'" class="asset-review-badge pending">审核中</span>
              <span v-else-if="asset.review_status === 'approved'" class="asset-review-badge approved">已通过</span>
              <span v-if="selectedAssets.some(item => item.id === asset.id)" class="asset-selected-mark"><Check /></span>
            </span>
            <span class="asset-card-copy"><strong>{{ asset.filename }}</strong><small>{{ asset.category }} · {{ asset.library_type === 'remix' ? '混剪成片' : '源素材' }}</small><em v-if="asset.review_status === 'rejected'">驳回意见：{{ asset.review_note || '请修改后重新提交' }}</em><b class="asset-card-choice">{{ selectedAssets.some(item => item.id === asset.id) ? '已加入批量 · 再次点击取消' : '点击加入批量提审' }}</b></span>
          </article>
        </div>
        <div v-else-if="assetSearched && !assetSearching" class="asset-no-result"><Search /><strong>没有找到匹配的素材</strong><span>换一个文件名或标签再试试。</span></div>
        <footer v-if="assetSearched && assetTotal" class="asset-result-pages"><span>第 {{ assetPage }} 页 · 每页 {{ assetPageSize }} 条</span><PaginationControls :page="assetPage" :total-pages="assetTotalPages" @change="searchAssets($event)" /></footer>
        <section v-if="selectedAsset" class="submit-review-shell">
          <div class="submit-selected-assets"><strong>已选 {{ selectedAssets.length }} 条</strong><button v-for="asset in selectedAssets" :key="asset.id" type="button" :class="{ active: selectedAsset?.id === asset.id }" @click="activateAsset(asset)"><span>{{ asset.filename }}</span><small>{{ asset.library_type === 'remix' ? '混剪' : '源素材' }}</small></button></div>
          <header><span><strong>当前配置：{{ selectedAsset.filename }}</strong><small>当前启用：{{ activeRequiredLabels }}</small></span><a v-if="namingReviewEnabled" :href="config?.naming_standard.document_url" target="_blank" rel="noreferrer"><ExternalLink />查看命名原文</a></header>
          <ReviewerAssignmentPicker :mode="assignmentMode" :reviewers="config?.eligible_reviewers || []" :selected="selectedReviewer" @update:mode="assignmentMode = $event" @select="selectedReviewer = $event" />
          <div v-if="selectedAsset.library_type === 'remix' && namingReviewEnabled" class="naming-review-form">
            <div class="naming-form-title"><span><ShieldCheck /><strong>混剪命名依据</strong><small>工作台来源会自动带出名称；请补齐类别、使用方式和出现位置。</small></span><b>第五章明星素材待确认，当前不拦截</b></div>
            <div v-if="namingLoading" class="naming-loading"><RefreshCw class="spin" />正在读取来源素材…</div>
            <template v-else>
              <label class="naming-none"><input v-model="noApplicableSources" type="checkbox" /><span><strong>未使用前四类素材</strong><small>确认本条混剪未使用上脸、机制、产品展示或 AI 一创素材</small></span></label>
              <div v-if="!noApplicableSources" class="naming-evidence-list">
                <article v-for="(item, index) in namingEvidence" :key="`${item.source_asset_id || 'manual'}-${index}`">
                  <span class="naming-row-index">{{ index + 1 }}</span>
                  <label><small>素材名称</small><input v-model="item.material_name" maxlength="512" placeholder="对应素材名称" /></label>
                  <label><small>素材类别</small><select v-model="item.category"><option value="" disabled>请选择</option><option value="face">上脸素材</option><option value="mechanism">机制素材</option><option value="product_display">产品展示素材</option><option value="ai_first_creation">AI一创素材</option></select></label>
                  <label><small>使用方式</small><select v-model="item.usage"><option value="" disabled>请选择</option><option value="complete">完整使用</option><option value="clip">切片使用</option></select></label>
                  <label><small>出现位置</small><select v-model="item.position"><option value="" disabled>请选择</option><option value="opening">片头</option><option value="middle">中段</option><option value="ending">片尾</option></select></label>
                  <button type="button" aria-label="删除命名依据" @click="removeNamingEvidence(index)"><Trash2 /></button>
                </article>
                <button type="button" class="add-naming-evidence" @click="addNamingEvidence"><Plus />增加一条来源素材</button>
              </div>
            </template>
          </div>
          <div v-else-if="selectedAsset.library_type === 'remix'" class="review-stage-off-note"><ShieldCheck /><span><strong>命名规范审核已关闭</strong><small>本次无需填写命名依据，也不会因文件名拦截提交或推送。</small></span></div>
          <div v-if="namingReviewEnabled && selectedRemixAssets.length > 1" class="batch-naming-confirm"><span><strong>批量命名确认</strong><small>仅当本批混剪均未使用上脸、机制、产品展示或 AI 一创素材时使用。</small></span><button type="button" :class="{ confirmed: batchNoApplicableConfirmed }" @click="confirmBatchNoApplicable"><Check />{{ batchNoApplicableConfirmed ? `已确认 ${selectedRemixAssets.length} 条` : `一键确认本批 ${selectedRemixAssets.length} 条` }}</button></div>
          <div class="submit-confirm"><div><strong>{{ assignmentMode === 'designated' && selectedReviewer ? `指定 ${selectedReviewer.user_name} 一人直接审核` : anyReviewGateEnabled ? `提交后按已启用节点处理：${activeRequiredLabels}` : aiRedlineEnabled ? 'AI会给出建议，当前未设置人工推送门禁' : '当前审核节点均已关闭，提交仅保留审核记录' }}</strong><span>已选择 {{ selectedAssets.length }} 条；AI结果不代替审核人决定，批量失败也不会拖垮整批。</span><em v-if="incompleteNamingAssets.length">还有 {{ incompleteNamingAssets.length }} 条待确认命名依据；可逐条填写，或使用上方批量确认。</em></div><input v-model="submitNote" placeholder="统一审核说明（选填）" /><button :disabled="busy === 'submit-batch' || (assignmentMode === 'designated' && !selectedReviewer)" @click="submitReview"><ShieldCheck />{{ busy === 'submit-batch' ? '提交中…' : `批量提审 ${selectedAssets.length} 条` }}</button></div>
        </section>
      </section>

      <section v-else class="review-list-card">
        <div class="review-toolbar"><div><strong>审核任务</strong><small>{{ queueHasLoaded ? `${queueReadError ? '上次成功读取：' : ''}共 ${total} 条` : '数量尚未读取' }}</small></div><label><Search /><input v-model="q" placeholder="搜索素材或提交人" @keyup.enter="loadQueue()" /></label><label><Users /><input v-model="reviewerQ" list="reviewer-name-options" placeholder="搜索指定审核人" @keyup.enter="loadQueue()" /></label><datalist id="reviewer-name-options"><option v-for="name in reviewerNames" :key="name" :value="name" /></datalist><select v-model="status"><option value="all">全部状态</option><option value="pending">审核中</option><option value="approved">已通过</option><option value="rejected">已驳回</option></select><button @click="loadQueue()"><RefreshCw />查询</button></div>
        <div class="review-batch-bar"><button type="button" class="batch-select" :disabled="!submissions.some(item => item.can_review)" @click="toggleAllReviewable"><Check />{{ batchSelectedItems.length ? `已选 ${batchSelectedItems.length} 条` : submissions.some(item => item.can_review) ? '选择本页可审任务' : '本页暂无可审任务' }}</button><input v-model="batchNote" :disabled="!submissions.some(item => item.can_review)" placeholder="批量审核说明；批量驳回时必填" /><button type="button" class="batch-reject" :disabled="!batchSelectedItems.length || busy.startsWith('batch-')" @click="batchAct('reject')"><X />批量驳回</button><button type="button" class="batch-approve" :disabled="!batchSelectedItems.length || busy.startsWith('batch-')" @click="batchAct('approve')"><BadgeCheck />批量通过</button></div>
        <div v-if="loading" class="review-empty">正在读取审核任务…</div>
        <div v-else-if="queueReadError && !submissions.length" class="review-empty" role="alert"><strong>审核任务读取未完成</strong><span>无法确认任务数量，请点击查询重试；已有审核记录不会因此删除。</span></div>
        <div v-else-if="!submissions.length" class="review-empty"><ShieldCheck /><strong>暂无审核任务</strong><span>从上方搜索并选择一条视频提交审核。</span></div>
        <div v-else class="review-task-grid">
          <article v-for="item in submissions" :key="item.id" class="review-item" :class="{ 'batch-selected': selectedSubmissionIds.includes(item.id) }">
            <div class="review-item-head">
              <button v-if="item.can_review" type="button" class="review-batch-check" :aria-pressed="selectedSubmissionIds.includes(item.id)" @click="toggleSubmission(item)"><Check v-if="selectedSubmissionIds.includes(item.id)" /></button><span class="review-item-copy"><strong>{{ item.asset_name }}</strong><small>{{ item.submitted_by_name }} · 第 {{ item.version }} 版 · {{ time(item.submitted_at) }}</small></span>
              <b :class="item.status">{{ statusLabel(item.status) }}</b>
            </div>
            <section class="naming-check-card" :class="item.naming_check.status">
              <header><span><ShieldCheck /><strong>混剪命名规范</strong></span><b>{{ item.naming_check.status === 'disabled' ? '已关闭' : item.naming_check.status === 'passed' ? '已通过' : item.naming_check.status === 'not_applicable' ? '不适用' : item.naming_check.status === 'stale' ? '已失效' : '未通过' }}</b></header>
              <p>{{ item.naming_check.message }}</p>
              <div v-if="item.naming_check.required_names.length" class="naming-required-names"><span>必须包含</span><b v-for="name in item.naming_check.required_names" :key="name">{{ name }}</b></div>
              <details v-if="item.naming_evidence.length"><summary>查看 {{ item.naming_evidence.length }} 条命名依据</summary><ul><li v-for="(entry, index) in item.naming_evidence" :key="`${entry.material_name}-${index}`"><strong>{{ entry.material_name }}</strong><span>{{ config?.naming_standard.categories.find(category => category.code === entry.category)?.label || entry.category }} · {{ entry.usage === 'complete' ? '完整使用' : '切片使用' }} · {{ entry.position === 'opening' ? '片头' : entry.position === 'middle' ? '中段' : '片尾' }}</span></li></ul></details>
            </section>
            <div class="review-inline-preview">
              <div class="review-player-stage">
                <video v-if="item.preview_url && !previewErrors[item.id]" :src="item.preview_url" controls playsinline preload="metadata" :aria-label="`${item.asset_name} 审核视频`" @play="playingReviewIds.add(item.id)" @pause="playingReviewIds.delete(item.id)" @ended="playingReviewIds.delete(item.id)" @loadeddata="previewErrors[item.id] = false" @error="playingReviewIds.delete(item.id); previewErrors[item.id] = true">当前浏览器无法播放该视频。</video>
                <div v-else class="review-preview-error"><CircleAlert /><strong>视频暂时无法在页面内播放</strong><span>{{ item.preview_url ? '可重试加载，或在新窗口打开原视频继续审核。' : '该素材暂未提供可播放地址。' }}</span><button v-if="item.preview_url" type="button" @click="previewErrors[item.id] = false">重试加载</button></div>
              </div>
              <div class="review-preview-meta"><span><strong>审核原视频</strong><small>第 {{ item.version }} 版</small></span><a v-if="item.preview_url" :href="item.preview_url" target="_blank" rel="noreferrer"><ExternalLink />打开原视频</a></div>
            </div>
            <section class="ai-review-card" :class="item.ai_review.status">
              <header><span><ShieldCheck /><strong>AI审核建议</strong></span><b>{{ aiStatusLabel(item.ai_review.status) }}</b></header>
              <p>{{ item.ai_review.summary || '正在等待视频理解结果。' }}</p>
              <div v-if="item.ai_review.status !== 'disabled'" class="ai-category-counts"><span>平台 {{ item.ai_review.category_counts.platform || 0 }}</span><span>内控 {{ item.ai_review.category_counts.internal || 0 }}</span><span>艺人 {{ item.ai_review.category_counts.artist || 0 }}</span><span>放宽 {{ item.ai_review.category_counts.relaxation || 0 }}</span></div>
              <details v-if="item.ai_review.findings.length"><summary>查看 {{ item.ai_review.findings.length }} 条证据与建议</summary><ul><li v-for="finding in item.ai_review.findings" :key="`${finding.rule_code}-${finding.segment_index}`" :class="finding.severity"><span>{{ aiCategoryLabel(finding.category) }} · {{ seconds(finding.start_seconds) }} · {{ finding.policy_effect === 'relaxed' ? '可参考放宽' : '重点关注' }}</span><strong>{{ finding.title }}</strong><em>{{ finding.evidence_source }}：{{ finding.evidence }}</em><p v-if="finding.recommendation">建议：{{ finding.recommendation }}</p><a v-if="finding.source_url" :href="finding.source_url" target="_blank" rel="noreferrer">查看规则依据 <ExternalLink /></a></li></ul></details>
              <div v-if="item.ai_review.status === 'error'" class="ai-error"><span>{{ item.ai_review.error_message || '视频理解暂时失败' }}</span><button v-if="item.can_retry_ai" :disabled="busy === `ai-${item.id}`" @click="retryAi(item)"><RefreshCw :class="{ spin: busy === `ai-${item.id}` }" />重新识别</button></div>
            </section>
            <div v-if="item.can_review" class="review-actions">
              <div><strong>轮到你进行{{ item.current_role_label }}审核</strong><small>重点判断前三秒、卖点、节奏和制作完成度；总分低于60分请驳回。</small></div>
              <div class="quality-score-grid"><label><span>前三秒</span><input v-model.number="ensureQualityScores(item.id).hook" type="number" min="0" max="25" /></label><label><span>卖点</span><input v-model.number="ensureQualityScores(item.id).selling_point" type="number" min="0" max="25" /></label><label><span>节奏</span><input v-model.number="ensureQualityScores(item.id).rhythm" type="number" min="0" max="25" /></label><label><span>完成度</span><input v-model.number="ensureQualityScores(item.id).production" type="number" min="0" max="25" /></label><b>总分 {{ qualityTotal(item.id) }}</b></div>
              <input v-model="actionNotes[item.id]" class="review-note-input" placeholder="效果判断或修改意见；驳回时必填" />
              <button class="reject" :disabled="busy.includes(item.id)" @click="act(item, 'reject')"><X />驳回</button><button class="approve" :disabled="busy.includes(item.id)" @click="act(item, 'approve')"><BadgeCheck />通过</button>
            </div>
            <p v-if="item.note" class="submit-note">提交说明：{{ item.note }}</p>
            <div v-if="item.route_center || item.current_reviewers.length || item.assignment_mode === 'designated'" class="review-routing"><span><Users /><strong v-if="item.assignment_mode === 'designated'">指定一人直接审核</strong><strong v-else>{{ [item.route_center, item.route_group].filter(Boolean).join(' / ') || '组织待匹配' }}</strong></span><span>{{ item.assignment_mode === 'designated' ? '指定审核人' : '当前审核人' }}：<b v-for="reviewer in item.current_reviewers" :key="`${reviewer.user_number}-${reviewer.user_name}`">{{ reviewer.user_name }}</b><b v-if="item.assignment_mode === 'designated' && !item.current_reviewers.length && item.designated_reviewer_name">{{ item.designated_reviewer_name }}</b><em v-if="!item.current_reviewers.length && !item.designated_reviewer_name">待配置</em></span></div>
            <div class="review-timeline" aria-label="审核流程">
              <div :class="['review-step', item.naming_check.status]"><span><Check v-if="['passed','not_applicable'].includes(item.naming_check.status)" /><ShieldCheck v-else-if="item.naming_check.status === 'disabled'" /><X v-else /></span><strong>命名规范</strong><small>{{ item.naming_check.status === 'disabled' ? '已关闭' : item.naming_check.status === 'passed' ? '已通过' : item.naming_check.status === 'not_applicable' ? '不适用' : '需重提' }}</small></div>
              <div :class="['review-step', `ai-${item.ai_review.status}`]"><span><Check v-if="['passed','warning'].includes(item.ai_review.status)" /><ShieldCheck v-else-if="item.ai_review.status === 'disabled'" /><CircleAlert v-else-if="['rejected','error'].includes(item.ai_review.status)" /><Clock3 v-else /></span><strong>AI建议</strong><small>{{ aiStatusLabel(item.ai_review.status) }}</small></div>
              <template v-for="decision in item.decisions" :key="decision.role_code"><div :class="['review-step', decision.status]"><span><Check v-if="decision.status === 'approved'" /><X v-else-if="decision.status === 'rejected'" /><Clock3 v-else /></span><strong>{{ decision.role_label }}</strong><small>{{ decision.quality_total != null ? `${decision.quality_total}分` : decision.reviewer_name || decision.candidate_reviewers.map(reviewer => reviewer.user_name).join(' / ') || (decision.status === 'pending' ? '待配置' : '—') }}</small><em v-if="decision.note">{{ decision.note }}</em></div></template>
              <div class="review-step reserved"><span><Clock3 /></span><strong>品牌调性</strong><small>入口预留</small><a :href="item.brand_tone.document_url" target="_blank" rel="noreferrer">查看规范</a></div>
            </div>
          </article>
        </div>
        <footer v-if="totalPages > 1" class="review-pages" aria-label="审核任务分页">
          <button type="button" :disabled="loading || page <= 1" @click="changeQueuePage(page - 1)">上一页</button>
          <span aria-live="polite">第 {{ page }} / {{ totalPages }} 页</span>
          <button type="button" :disabled="loading || page >= totalPages" @click="changeQueuePage(page + 1)">下一页</button>
        </footer>
      </section>
    </template>

    <template v-else>
      <section class="workflow-card">
        <div class="section-title"><span><ShieldCheck /><b>上线审核规则</b></span><small>命名规范和人工审核可作为门禁；AI只给结果和建议，不代替审核人决定。</small></div>
        <div class="review-gate-grid">
          <label class="review-gate-switch" :class="{ active: namingReviewEnabled }"><span class="review-gate-index">1</span><div><strong>命名规范审核</strong><small>{{ namingReviewEnabled ? '已开启 · 不合规范命名将禁止推送' : '已关闭 · 不校验素材命名' }}</small></div><input v-model="namingReviewEnabled" type="checkbox" aria-label="开启命名规范审核" /><i aria-hidden="true"></i></label>
          <label class="review-gate-switch" :class="{ active: aiRedlineEnabled }"><span class="review-gate-index">2</span><div><strong>AI 审核建议</strong><small>{{ aiRedlineEnabled ? '已开启 · 只给风险、放宽依据和建议，不自动决定' : '已关闭 · 不进入 AI 识别队列' }}</small></div><input v-model="aiRedlineEnabled" type="checkbox" aria-label="开启AI审核建议" /><i aria-hidden="true"></i></label>
          <label class="review-gate-switch" :class="{ active: workflowEnabled }"><span class="review-gate-index">3</span><div><strong>人工审核</strong><small>{{ workflowEnabled ? '已开启 · 依次经过组长与主管' : '已关闭 · 不等待组长和主管审核' }}</small></div><input v-model="workflowEnabled" type="checkbox" aria-label="开启人工审核" /><i aria-hidden="true"></i></label>
        </div>
        <div class="workflow-actions"><p class="workflow-summary"><strong>当前生效链路</strong><span>{{ activeRequiredLabels }}</span><small>AI与品牌调性不参与自动拦截。</small></p><button class="save-workflow" :disabled="busy === 'workflow'" @click="saveWorkflow"><Check />{{ busy === 'workflow' ? '保存中…' : '保存审核开关' }}</button></div>
        <div class="fixed-stage-flow" aria-label="审核节点状态"><a :class="{ inactive: !namingReviewEnabled }" :href="config?.naming_standard.document_url" target="_blank" rel="noreferrer"><b>1</b>命名规范<small>{{ namingReviewEnabled ? '已开启' : '已关闭' }}</small></a><i>→</i><span :class="{ inactive: !aiRedlineEnabled }"><b>2</b>AI建议<small>{{ aiRedlineEnabled ? '仅供参考' : '已关闭' }}</small></span><i>→</i><span :class="{ inactive: !workflowEnabled }"><b>3</b>组长<small>{{ workflowEnabled ? '已开启' : '已关闭' }}</small></span><i>→</i><span :class="{ inactive: !workflowEnabled }"><b>4</b>主管<small>{{ workflowEnabled ? '已开启' : '已关闭' }}</small></span><i>→</i><a class="inactive" :href="config?.brand_tone.document_url" target="_blank" rel="noreferrer"><b>5</b>品牌调性<small>预留入口</small></a></div>
        <section class="naming-standard-card">
          <header><span><ShieldCheck /><strong>自产素材混剪命名规范</strong><b class="stage-state-pill" :class="namingReviewEnabled ? 'enabled' : 'disabled'">{{ namingReviewEnabled ? '审核已开启' : '审核已关闭' }}</b></span><a :href="config?.naming_standard.document_url" target="_blank" rel="noreferrer"><ExternalLink />查看飞书原文</a></header>
          <div><article v-for="category in config?.naming_standard.categories" :key="category.code"><strong>{{ category.label }}</strong><p>{{ category.rule }}</p></article><article class="pending"><strong>明星素材</strong><p>第五章正文正在确认，当前明确不参与拦截。</p></article></div>
        </section>
        <section class="redline-standards">
          <header><span><ShieldAlert /><strong>AI审核建议标准</strong><b class="stage-state-pill" :class="aiRedlineEnabled ? 'enabled' : 'disabled'">{{ aiRedlineEnabled ? '建议已开启' : '建议已关闭' }}</b></span><small>{{ config?.ai_redlines.version }} · 识别口播、画面和画面文字</small></header>
          <p class="redline-policy-source">已纳入 <a :href="config?.ai_redlines.policy_source.url" target="_blank" rel="noreferrer">{{ config?.ai_redlines.policy_source.title }} <ExternalLink /></a>，共参考 {{ config?.ai_redlines.policy_source.case_count }} 条案例。</p>
          <div class="redline-standard-grid">
            <article v-for="category in redlineCategories" :key="category.code">
              <header><strong>{{ category.label }}</strong><small>{{ category.description }}</small></header>
              <ul><li v-for="rule in redlineRulesByCategory(category.code)" :key="rule.code"><b :class="rule.severity">{{ rule.severity === 'hard' ? '重点关注' : rule.category === 'relaxation' ? '放宽参考' : '一般提醒' }}</b><span><strong>{{ rule.title }}</strong><small>{{ patternSummary(rule.pattern) }}</small></span></li></ul>
            </article>
          </div>
          <footer><span><b>重点关注</b>：提示高风险边界，交给审核人判断</span><span><b>放宽参考</b>：展示可放宽依据与核对条件</span><span><b>识别失败</b>：不阻止审核人继续操作</span></footer>
        </section>
      </section>

      <section v-if="config?.can_configure" class="redline-editor-card">
        <div class="section-title"><span><ShieldAlert /><b>AI审核建议规则管理</b></span><small>修改仅影响后续新提审；AI结果始终不自动通过或驳回。</small></div>
        <div class="redline-editor-note"><CircleAlert /><span>“重点关注”与“普通提醒”都只提供证据和建议。命中词可用 <b>|</b> 分隔，复杂规则支持正则表达式。</span></div>
        <div class="redline-rule-list">
          <article v-for="(rule, index) in redlineRules" :key="rule.code || `new-${index}`" :class="{ disabled: !rule.enabled }">
            <label class="redline-enabled"><input v-model="rule.enabled" type="checkbox" /><span>{{ rule.enabled ? '已启用' : '已停用' }}</span></label>
            <select v-model="rule.category" aria-label="规则分类"><option value="platform">平台风险</option><option value="internal">内控风险</option><option value="artist">艺人及版权</option><option value="relaxation">放宽规则</option></select>
            <select v-model="rule.severity" aria-label="提示等级"><option value="hard">重点关注</option><option value="warning">普通提醒 / 放宽参考</option></select>
            <input v-model="rule.title" class="redline-title-input" maxlength="160" placeholder="规则名称，例如：站外导流" />
            <textarea v-model="rule.pattern" rows="2" maxlength="2000" placeholder="命中词或规则表达式，例如：加微信|微信号|站外下单"></textarea>
            <button class="redline-delete" type="button" aria-label="删除规则" @click="removeRedlineRule(index)"><Trash2 /></button>
          </article>
        </div>
        <footer class="redline-editor-actions"><button class="add-redline" type="button" @click="addRedlineRule"><Plus />新增规则</button><span>当前 {{ redlineRules.filter(rule => rule.enabled).length }} 条启用规则</span><button class="save-redlines" type="button" :disabled="busy === 'redlines'" @click="saveRedlineRules"><Save />{{ busy === 'redlines' ? '保存中…' : '保存并启用新版本' }}</button></footer>
      </section>

      <section class="review-organization-card">
        <div class="section-title"><span><Users /><b>营销中心分组审核架构</b></span><small>按已确认组织口径：具体小组或中心级组长初审，中心负责人作为主管复审。</small></div>
        <div class="organization-source"><span>{{ config?.organization_source.whiteboard_label }} · 文档版本 {{ config?.organization_source.document_revision }}</span><a v-if="config?.organization_source.document_url" :href="config.organization_source.document_url" target="_blank" rel="noreferrer">查看飞书画板 <ExternalLink /></a></div>
        <div class="review-organization-grid"><article v-for="center in config?.organization_centers" :key="center.center"><header><strong>{{ center.center }}</strong><b>{{ center.member_count }} 名组员</b></header><p class="center-supervisor"><span>主管（中心负责人）</span><b>{{ center.supervisors.join('、') || '待配置' }}</b></p><p v-if="!center.groups.length" class="center-supervisor center-team-leads"><span>组长</span><b>{{ center.team_leads.join('、') || '待配置' }}</b></p><div class="center-groups"><p v-for="group in center.groups" :key="`${center.center}-${group.group_name}`"><span>{{ group.group_name }} · {{ group.member_count }} 名组员</span><b>组长：{{ group.team_leads.join('、') || '画板未标注' }}</b></p></div></article></div>
      </section>

      <section class="role-add-card">
        <div class="section-title"><span><UserPlus /><b>添加角色成员</b></span><small>姓名应与 OA 真实姓名一致；工号可选，填写后匹配更准确。</small></div>
        <div class="role-form"><select v-model="roleForm.role_code"><option v-for="role in config?.roles.filter(item => !item.legacy)" :key="role.code" :value="role.code">{{ role.label }}</option></select><input v-model="roleForm.user_name" placeholder="OA 真实姓名" /><input v-model="roleForm.user_number" placeholder="OA 工号（选填）" /><select v-model="roleForm.center"><option value="">未指定中心</option><option v-for="center in config?.organization_centers" :key="center.center" :value="center.center">{{ center.center }}</option><option value="ALL">全部中心</option></select><input v-model="roleForm.group_name" placeholder="具体小组（如 1组）" /><button :disabled="busy === 'role' || !roleForm.user_name.trim()" @click="addRole"><UserPlus />添加成员</button></div>
      </section>

      <section class="role-grid">
        <article v-for="role in config?.roles.filter(item => !item.legacy)" :key="role.code" :class="{ missing: role.required && !role.members.length, reserved: role.code === 'brand_tone' }"><header><span><Users /><strong>{{ role.label }}</strong></span><b v-if="role.required">必审</b><b v-else-if="role.code === 'brand_tone'" class="optional">暂不启用</b><b v-else class="member">协作角色</b></header><p>{{ role.code === 'team_lead' ? '组长节点按中心及具体小组定向分配' : role.code === 'supervisor' ? '主管节点按中心负责人定向分配' : role.code === 'brand_tone' ? '保留规范入口与人员配置，本阶段不影响上线。' : '组员可提交素材并查看审核进度' }}</p><div class="role-members"><div v-for="member in role.members" :key="member.id"><span class="member-avatar">{{ member.user_name.slice(0, 1) }}</span><span><strong>{{ member.user_name }}</strong><small>{{ [member.center, member.group_name].filter(Boolean).join(' / ') || member.department || '组织待补充' }}<template v-if="member.user_number"> · {{ member.user_number }}</template></small></span><button v-if="member.source !== 'organization_sync'" :disabled="busy === `role-${member.id}`" @click="removeRole(member.id)"><Trash2 /></button></div><div v-if="!role.members.length" class="no-member"><CircleAlert /><span>{{ role.code === 'brand_tone' ? '当前无需配置' : `尚未配置${role.label}` }}</span></div></div></article>
      </section>
      <details v-if="config?.roles.some(item => item.legacy && item.members.length)" class="legacy-role-records"><summary>查看历史总监/内控角色记录</summary><p>仅用于兼容旧审核单，不再创建新的必审节点。</p></details>
    </template>

    <Teleport to="body">
      <div v-if="previewAsset" class="review-asset-preview-backdrop" role="presentation" @click.self="closeAssetPreview">
        <section class="review-asset-preview-dialog" role="dialog" aria-modal="true" :aria-label="`播放 ${previewAsset.filename}`">
          <header><span><strong>{{ previewAsset.filename }}</strong><small>按9:16审核画布显示，原视频不会被裁切或拉伸</small></span><button type="button" aria-label="关闭视频" @click="closeAssetPreview"><X /></button></header>
          <div class="review-asset-preview-stage"><video :key="previewAsset.id" :src="previewAsset.preview_url" :poster="previewAsset.cover_url || undefined" controls autoplay playsinline preload="metadata">当前浏览器无法播放该视频。</video></div>
        </section>
      </div>
    </Teleport>
  </section>
</template>

<style scoped>
.review-preview-load{width:100%;min-height:160px;border:1px solid #d7e9f5;border-radius:12px;background:#edf7ff;color:#087cba;cursor:pointer;font-weight:600}.review-center{display:grid;gap:18px;color:#123b5a}.review-hero{display:flex;align-items:center;justify-content:space-between;padding:28px 32px;border:1px solid #d7e9f5;border-radius:24px;background:linear-gradient(135deg,#f9fdff,#eef8ff 62%,#fff8f1);box-shadow:0 16px 42px rgba(30,91,128,.08)}.review-hero>div>span,.section-title>span{font-size:12px;font-weight:800;letter-spacing:.12em;color:#1487c9}.review-hero h2{margin:7px 0 8px;font-size:27px}.review-hero p{margin:0;color:#6b879b}.review-state{display:flex;gap:12px;align-items:center;min-width:150px;padding:15px 18px;border-radius:18px}.review-state svg{width:27px;height:27px}.review-state span{display:grid}.review-state small{color:#688398}.review-state.enabled{color:#087c5f;background:#def7ec}.review-state.disabled{color:#9a6844;background:#fff0e4}.review-message{display:flex;align-items:center;gap:9px;padding:12px 16px;border-radius:13px;font-weight:700}.review-message svg{width:17px}.review-message button{margin-left:auto;border:0;background:none;color:inherit}.review-message.success{color:#087c5f;background:#e6f8f1}.review-message.error{color:#b53342;background:#fff0f1}.review-submit-card,.review-list-card,.workflow-card,.role-add-card{padding:22px;border:1px solid #dcebf4;border-radius:20px;background:#fff}.section-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:17px}.section-title>span{display:flex;align-items:center;gap:8px;font-size:15px;letter-spacing:0;color:#123b5a}.section-title svg{width:19px;color:#1687c5}.section-title small{color:#7890a1}.asset-search,.role-form,.review-toolbar{display:flex;gap:10px}.asset-search label,.review-toolbar label{display:flex;align-items:center;gap:8px;flex:1;padding:0 13px;border:1px solid #cfe2ee;border-radius:12px}.asset-search input,.review-toolbar input{width:100%;height:43px;border:0;outline:0}.asset-search button,.review-toolbar button,.submit-confirm button,.save-workflow,.role-form button{display:flex;align-items:center;justify-content:center;gap:7px;border:0;border-radius:12px;padding:0 18px;background:#0d89d3;color:#fff;font-weight:800}.asset-search svg,.review-toolbar svg,.submit-confirm svg,.save-workflow svg,.role-form button svg{width:16px}.asset-results{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px;margin-top:13px}.asset-results button{display:flex;align-items:center;gap:9px;min-width:0;padding:9px;border:1px solid #dbe9f2;border-radius:13px;background:#fafdff;text-align:left;color:#173f5c}.asset-results button.selected{border-color:#168fd5;background:#edf8ff}.asset-results button>span:nth-child(2){display:grid;min-width:0;flex:1}.asset-results strong,.asset-results small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.asset-results small{margin-top:3px;color:#7890a1}.asset-thumb{display:grid;place-items:center;width:42px;height:42px;overflow:hidden;border-radius:9px;background:#dff1fb}.asset-thumb img{width:100%;height:100%;object-fit:cover}.asset-thumb svg{width:16px}.submit-confirm{display:flex;align-items:center;gap:12px;margin-top:14px;padding:13px;border-radius:14px;background:#f2f9fd}.submit-confirm>div{display:grid;flex:1}.submit-confirm span{margin-top:3px;font-size:12px;color:#718b9d}.submit-confirm input{width:260px;height:39px;padding:0 12px;border:1px solid #cfe2ee;border-radius:10px}.submit-confirm button{height:39px}.review-toolbar{align-items:center;margin-bottom:14px}.review-toolbar>div{display:grid;margin-right:auto}.review-toolbar>div small{color:#7890a1}.review-toolbar label{flex:0 1 330px}.review-toolbar select,.role-form select,.role-form input{height:43px;padding:0 12px;border:1px solid #cfe2ee;border-radius:12px;background:#fff}.review-toolbar button{height:43px;background:#eef7fc;color:#18688f}.review-empty{display:grid;place-items:center;gap:7px;min-height:180px;color:#7890a1}.review-empty svg{width:30px}.review-item{padding:18px 4px;border-top:1px solid #e5eff5}.review-item-head,.review-item-head>div{display:flex;align-items:center;justify-content:space-between}.review-item-head>div{justify-content:flex-start;gap:10px}.review-item-head>div>span:last-child{display:grid}.review-item-head small{margin-top:4px;color:#7890a1}.review-video-icon{display:grid;place-items:center;width:38px;height:38px;border-radius:11px;color:#147ab1;background:#e7f5fc}.review-video-icon svg{width:16px}.review-item-head>b{padding:6px 10px;border-radius:999px;font-size:12px}.review-item-head>b.pending{color:#966b17;background:#fff3d3}.review-item-head>b.approved{color:#087c5f;background:#def7ec}.review-item-head>b.rejected{color:#b53342;background:#ffe8eb}.submit-note{padding:9px 12px;border-radius:9px;background:#f7fafc;color:#607d90}.review-timeline{display:flex;align-items:flex-start;margin:16px 0}.review-step{display:grid;justify-items:center;gap:4px;width:120px;text-align:center}.review-step>span{display:grid;place-items:center;width:27px;height:27px;border-radius:50%;background:#edf3f6;color:#7890a1}.review-step>span svg{width:14px}.review-step.approved>span{color:#fff;background:#16a477}.review-step.rejected>span{color:#fff;background:#d84c5b}.review-step strong{font-size:13px}.review-step small{color:#7890a1}.review-step em{max-width:115px;font-size:11px;color:#b04a54}.step-arrow{width:17px;margin:6px -4px;color:#b4c7d2}.review-actions{display:flex;align-items:center;gap:9px;padding:13px;border-radius:13px;background:#f2f9fd}.review-actions>div{display:grid;flex:1;gap:7px}.review-actions input{height:38px;padding:0 11px;border:1px solid #cfe2ee;border-radius:9px}.review-actions button{display:flex;align-items:center;gap:6px;height:38px;padding:0 14px;border-radius:9px;font-weight:800}.review-actions svg{width:15px}.review-actions .reject{color:#b43a49;border:1px solid #f0c7cc;background:#fff}.review-actions .approve{color:#fff;border:1px solid #10946c;background:#10946c}.review-pages{display:flex;justify-content:center;align-items:center;gap:12px;padding-top:15px}.review-pages button{padding:7px 13px;border:1px solid #d4e4ed;border-radius:9px;background:#fff}.workflow-layout{display:flex;align-items:center;gap:20px}.workflow-switch{display:flex;align-items:center;gap:11px;min-width:220px}.workflow-switch>input{display:none}.workflow-switch>span{position:relative;width:48px;height:27px;border-radius:99px;background:#cbd8df}.workflow-switch>span:after{content:'';position:absolute;top:4px;left:4px;width:19px;height:19px;border-radius:50%;background:#fff;transition:.2s}.workflow-switch input:checked+span{background:#13a476}.workflow-switch input:checked+span:after{left:25px}.workflow-switch>div{display:grid}.workflow-switch small{color:#7890a1}.stage-checks{display:flex;gap:8px;flex:1}.stage-checks label input{display:none}.stage-checks label span{display:block;padding:9px 13px;border:1px solid #d5e6ef;border-radius:10px;color:#718a9b}.stage-checks input:checked+span{color:#0c75ae;border-color:#52acd9;background:#edf8ff}.save-workflow{height:43px}.workflow-summary{margin:16px 0 0;padding:11px 13px;border-radius:10px;color:#56768b;background:#f4f9fc}.role-form{display:grid;grid-template-columns:140px 1fr 1fr 1fr 145px}.role-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.role-grid article{padding:19px;border:1px solid #dceaf2;border-radius:17px;background:#fff}.role-grid article.missing{border-color:#f0c99f;background:#fffaf4}.role-grid article header{display:flex;align-items:center;justify-content:space-between}.role-grid article header>span{display:flex;align-items:center;gap:8px}.role-grid article header svg{width:19px;color:#1487c9}.role-grid article header>b{padding:4px 8px;border-radius:99px;color:#925d19;background:#fff0ce;font-size:11px}.role-grid article header>b.optional{color:#527284;background:#edf3f6}.role-grid article header>b.member{color:#136e97;background:#e8f5fb}.role-grid article>p{color:#7890a1;font-size:13px}.role-members{display:grid;gap:7px}.role-members>div{display:flex;align-items:center;gap:9px;padding:9px;border-radius:11px;background:#f7fafc}.role-members>div>span:nth-child(2){display:grid;flex:1}.role-members small{color:#7890a1}.member-avatar{display:grid;place-items:center;width:34px;height:34px;border-radius:50%;color:#1476a8;background:#dff1fa;font-weight:800}.role-members button{border:0;background:none;color:#b34b57}.role-members button svg{width:16px}.role-members .no-member{justify-content:center;color:#9c7351;background:#fff4e8}.no-member svg{width:16px}.spin{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:1000px){.asset-results,.role-grid{grid-template-columns:1fr}.workflow-layout,.submit-confirm{align-items:stretch;flex-direction:column}.role-form{grid-template-columns:1fr}.review-timeline{overflow-x:auto}.review-hero{align-items:flex-start;gap:16px}.section-title{align-items:flex-start;gap:8px;flex-direction:column}}

/* Keep the review workflow visually isolated from legacy dark-theme overrides. */
.review-state{
  min-width:0;
  gap:9px;
  padding:9px 11px;
  border:1px solid #d7e5ed;
  border-radius:12px;
  color:#536f82;
  background:#fff;
  box-shadow:0 7px 18px rgba(36,91,124,.06);
}
.review-state svg{width:18px;height:18px;color:#6f8a9c}
.review-state>span{display:block;color:#536f82;font-size:13px;font-weight:700;white-space:nowrap}
.review-state>strong{display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;font-size:12px;white-space:nowrap}
.review-state>strong i{width:6px;height:6px;border-radius:50%;background:currentColor}
.review-state.disabled{color:#718797;border-color:#d8e3ea;background:#fff}
.review-state.disabled>strong{color:#647b8b;background:#eef3f6}
.review-state.enabled{color:#147858;border-color:#bfdfd2;background:#f9fdfb}
.review-state.enabled svg{color:#198461}
.review-state.enabled>strong{color:#117453;background:#e5f5ef}

.review-item-head{align-items:flex-start;gap:16px}
.review-list-card,.review-item,.review-item-head,.review-inline-preview,.review-player-stage{min-width:0}
.review-item-identity{min-width:0;flex:1;align-items:center !important}
.review-item-copy{display:grid;min-width:0;align-content:center}
.review-item-copy>strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.review-item-copy>button{
  justify-self:start;
  margin:5px 0 0;
  padding:0;
  border:0;
  color:#0d7fbd;
  background:transparent;
  font-size:12px;
  font-weight:800;
  cursor:pointer;
}
.review-item-copy>button:hover{text-decoration:underline;text-underline-offset:3px}
.review-item-copy>button:focus-visible{outline:2px solid #168fd5;outline-offset:3px;border-radius:3px}
.review-preview-trigger{
  position:relative;
  flex:0 0 124px;
  width:124px;
  height:70px;
  padding:0;
  overflow:hidden;
  border:1px solid #c9dee9;
  border-radius:12px;
  background:#dceef8;
  cursor:pointer;
}
.review-preview-trigger:hover{border-color:#72b7da}
.review-preview-trigger:focus-visible{outline:3px solid rgba(13,137,211,.24);outline-offset:3px}
.review-preview-trigger:deep(.video-cover-shell){width:100%;height:100%}
.review-preview-trigger:after{content:'';position:absolute;inset:0;background:linear-gradient(180deg,rgba(8,34,49,0),rgba(8,34,49,.2));pointer-events:none}
.review-preview-control{
  position:absolute;
  z-index:1;
  top:50%;
  left:50%;
  display:grid;
  place-items:center;
  width:34px;
  height:34px;
  border:1px solid rgba(255,255,255,.72);
  border-radius:50%;
  color:#fff;
  background:rgba(10,64,94,.78);
  transform:translate(-50%,-50%);
  transition:background .18s ease,transform .18s ease;
  pointer-events:none;
}
.review-preview-trigger:hover .review-preview-control{background:#0d89d3;transform:translate(-50%,-50%) scale(1.06)}
.review-preview-control.active{background:rgba(12,54,79,.88)}
.review-preview-control svg{width:15px;height:15px}
.review-inline-preview{
  display:grid;
  grid-template-columns:1fr;
  width:min(268px,100%);
  margin:16px 0 20px;
  overflow:hidden;
  border-radius:16px;
  background:#fff;
  box-shadow:0 14px 32px rgba(36,91,124,.14);
}
.review-player-stage{display:grid;place-items:center;aspect-ratio:9/16;overflow:hidden;background:#0b1820}
.review-player-stage video{width:100%;height:100%;object-fit:contain;background:#0b1820}
.review-preview-error{display:grid;place-items:center;gap:6px;padding:20px;color:#dbeaf1;text-align:center}
.review-preview-error svg{width:24px;color:#f1c778}
.review-preview-error span{font-size:12px;color:#a9c0cc}
.review-preview-meta{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:10px;min-width:0;padding:12px}
.review-preview-meta>span{display:grid;min-width:0;gap:5px}
.review-preview-meta small{overflow:hidden;color:#718b9d;text-overflow:ellipsis;white-space:nowrap}
.review-preview-meta a{display:inline-flex;align-items:center;justify-content:center;gap:5px;box-sizing:border-box;padding:8px 9px;border:1px solid #c8dce7;border-radius:9px;color:#176f9b;background:#f8fcfe;font-size:12px;font-weight:800;text-decoration:none;white-space:nowrap}
.review-preview-meta a:hover{border-color:#83bddb;background:#f9fdff}
.review-preview-meta a:focus-visible{outline:3px solid rgba(13,137,211,.2);outline-offset:2px}
.review-preview-meta a svg{width:14px;height:14px}

.asset-search label,
.review-toolbar label{
  min-width:0;
  height:46px;
  color:#6e8a9d !important;
  border:1px solid #cbdde8 !important;
  background:#fff !important;
  transition:border-color .18s ease,box-shadow .18s ease;
}
.asset-search label:focus-within,
.review-toolbar label:focus-within{
  border-color:#55a9d7 !important;
  box-shadow:0 0 0 3px rgba(13,137,211,.1);
}
.review-center .asset-search input,
.review-center .review-toolbar input,
.review-center .submit-confirm input,
.review-center .review-actions input,
.review-center .role-form input{
  min-width:0;
  color:#274a61 !important;
  background:#fff !important;
  -webkit-text-fill-color:#274a61 !important;
  caret-color:#0d89d3;
  box-shadow:none !important;
}
.review-center input::placeholder{
  color:#8ca2b1 !important;
  -webkit-text-fill-color:#8ca2b1 !important;
  opacity:1;
}
.asset-search input,
.review-toolbar input{height:44px !important;padding:0 !important;border:0 !important;outline:0 !important}
.asset-search>button{height:46px;min-width:128px;border:1px solid #0d89d3}
.asset-search>button:hover{background:#087bc0;box-shadow:0 7px 16px rgba(13,137,211,.16)}
.review-toolbar label{flex:0 1 360px}
.review-toolbar select,
.review-toolbar>button{
  height:46px !important;
  border:1px solid #cbdde8 !important;
  border-radius:12px;
  background:#fff !important;
}
.review-toolbar select{min-width:120px;color:#34566d !important;padding:0 34px 0 13px}
.review-toolbar>button{color:#176d97 !important;background:#f3f9fc !important}
.review-toolbar>button:hover{border-color:#9bc9e1 !important;background:#eaf6fc !important}

@media(max-width:760px){
  .review-hero{flex-direction:column}
  .review-state{align-self:flex-start}
  .review-list-card{overflow:hidden}
  .review-timeline{width:100%;max-width:100%}
  .review-preview-meta{align-items:center}
  .asset-search{display:grid;grid-template-columns:minmax(0,1fr) auto}
  .review-toolbar{display:grid;grid-template-columns:minmax(0,1fr) auto auto}
  .review-toolbar>div{grid-column:1/-1}
  .review-toolbar label{grid-column:1/-1;max-width:none}
}
@media(max-width:520px){
  .review-item-head{gap:10px}
  .review-item-identity{align-items:flex-start !important}
  .review-preview-trigger{flex-basis:96px;width:96px;height:58px;border-radius:10px}
  .review-preview-control{width:30px;height:30px}
  .review-item-copy>strong{white-space:normal;overflow-wrap:anywhere}
  .review-inline-preview{width:100%;margin-inline:0}
  .review-preview-meta{grid-template-columns:1fr;align-items:start}
  .review-preview-meta a{width:100%}
  .asset-search{grid-template-columns:1fr}
  .asset-search>button{width:100%}
  .review-toolbar{grid-template-columns:1fr 1fr}
  .review-toolbar label{grid-column:1/-1}
  .review-toolbar>button{width:100%}
}

/* Search results are visual choices: show the real cover, then the filename. */
.asset-result-summary{
  display:flex;
  align-items:baseline;
  justify-content:space-between;
  gap:16px;
  margin-top:18px;
}
.asset-result-summary span{color:#718b9d;font-size:12px}
.asset-results{
  grid-template-columns:repeat(6,minmax(0,1fr));
  gap:14px;
  margin-top:10px;
}
.asset-results .asset-result-card{
  display:grid;
  align-content:start;
  gap:0;
  padding:0;
  overflow:hidden;
  border:1px solid #d5e5ed;
  border-radius:14px;
  background:#fff;
  color:#173f5c;
  text-align:left;
  cursor:pointer;
  transition:border-color .18s ease,box-shadow .18s ease,transform .18s ease;
}
.asset-results .asset-result-card:hover{border-color:#7dbbd9;box-shadow:0 10px 24px rgba(36,91,124,.1);transform:translateY(-2px)}
.asset-results .asset-result-card:focus-visible{outline:3px solid rgba(13,137,211,.2);outline-offset:3px}
.asset-results .asset-result-card.selected{border-color:#168fd5;background:#fff;box-shadow:0 0 0 3px rgba(22,143,213,.12)}
.asset-card-cover{position:relative;display:block;aspect-ratio:9/16;overflow:hidden;background:#dfeef5}
.asset-card-cover>img,.asset-card-cover:deep(.video-cover-shell){width:100%;height:100%;object-fit:cover}
.asset-card-cover:deep(.video-cover){width:100%;height:100%;object-fit:cover}
.asset-cover-empty{display:grid;place-items:center;align-content:center;gap:5px;width:100%;height:100%;color:#66879b;background:#eaf4f8}
.asset-cover-empty svg{width:22px}
.asset-cover-empty small{font-size:11px}
.asset-selected-mark{position:absolute;right:10px;top:10px;display:grid;place-items:center;width:28px;height:28px;border:2px solid #fff;border-radius:50%;color:#fff;background:#0d89d3;box-shadow:0 5px 12px rgba(20,85,122,.22)}
.asset-selected-mark svg{width:15px}
.asset-play-button{
  position:absolute;
  z-index:2;
  left:50%;
  top:50%;
  display:grid !important;
  place-items:center;
  width:42px;
  height:42px;
  min-width:0 !important;
  padding:0 !important;
  border:1px solid rgba(255,255,255,.8) !important;
  border-radius:50% !important;
  color:#fff !important;
  background:rgba(11,48,69,.78) !important;
  box-shadow:0 8px 18px rgba(8,35,51,.22);
  transform:translate(-50%,-50%);
  transition:background .18s ease,transform .18s ease !important;
}
.asset-play-button:hover{background:#0d89d3 !important;transform:translate(-50%,-50%) scale(1.07)}
.asset-play-button:focus-visible{outline:3px solid rgba(255,255,255,.8);outline-offset:3px}
.asset-play-button:disabled{cursor:not-allowed;opacity:.45}
.asset-play-button svg{width:18px;height:18px;margin-left:2px}
.asset-review-badge{
  position:absolute;
  left:9px;
  top:9px;
  z-index:1;
  display:inline-flex;
  align-items:center;
  gap:5px;
  min-height:27px;
  padding:0 9px;
  border:1px solid rgba(255,255,255,.88);
  border-radius:999px;
  color:#315a70;
  background:rgba(250,253,255,.94);
  box-shadow:0 5px 14px rgba(30,70,94,.14);
  font-size:11px;
  font-weight:800;
  backdrop-filter:blur(8px);
}
.asset-review-badge.rejected{color:#a92f3e;background:rgba(255,246,247,.96)}
.asset-review-badge.rejected i{width:7px;height:7px;border-radius:50%;background:#e04455;box-shadow:0 0 0 3px rgba(224,68,85,.13)}
.asset-review-badge.pending{color:#8a641b;background:rgba(255,250,235,.96)}
.asset-review-badge.approved{color:#147355;background:rgba(241,252,247,.96)}
.asset-card-copy{display:grid;gap:5px;min-width:0;padding:11px 12px 12px}
.asset-card-copy strong,.asset-card-copy small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.asset-card-copy small{color:#718b9d;font-size:12px}
.asset-card-choice{margin-top:3px;color:#1477a8;font-size:11px;font-weight:800}
.asset-result-card.selected .asset-card-choice{color:#087c5f}
.asset-card-copy em{
  display:-webkit-box;
  overflow:hidden;
  margin-top:3px;
  color:#b03c4a;
  font-size:11px;
  font-style:normal;
  line-height:1.45;
  -webkit-box-orient:vertical;
  -webkit-line-clamp:2;
}
.asset-no-result{display:grid;place-items:center;gap:6px;min-height:150px;margin-top:12px;color:#7890a1;border:1px dashed #cddfe8;border-radius:14px;background:#fbfdfe}
.asset-no-result svg{width:25px}
.asset-result-pages{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-top:14px;padding-top:14px;border-top:1px solid #e2edf3}
.asset-result-pages>span{color:#718b9d;font-size:12px}

.review-asset-preview-backdrop{
  position:fixed;
  z-index:80;
  inset:0;
  display:grid;
  place-items:center;
  padding:24px;
  background:rgba(8,31,45,.72);
  backdrop-filter:blur(8px);
}
.review-asset-preview-dialog{
  display:grid;
  width:min(390px,calc(100vw - 32px));
  max-height:calc(100dvh - 32px);
  overflow:hidden;
  border:1px solid rgba(255,255,255,.72);
  border-radius:20px;
  background:#f8fcfe;
  box-shadow:0 28px 80px rgba(4,28,42,.36);
}
.review-asset-preview-dialog>header{display:flex;align-items:center;gap:14px;padding:13px 14px;background:#fff}
.review-asset-preview-dialog>header>span{display:grid;min-width:0;flex:1;gap:4px}
.review-asset-preview-dialog>header strong,.review-asset-preview-dialog>header small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.review-asset-preview-dialog>header small{color:#718b9d;font-size:11px}
.review-asset-preview-dialog>header button{display:grid;place-items:center;width:34px;height:34px;padding:0;border:1px solid #d2e2ea;border-radius:10px;color:#45687d;background:#f8fbfd}
.review-asset-preview-dialog>header button:hover{border-color:#8fc3dc;background:#eef8fc}
.review-asset-preview-dialog>header button svg{width:17px}
.review-asset-preview-stage{display:grid;place-items:center;aspect-ratio:9/16;max-height:calc(100dvh - 108px);background:#07131b}
.review-asset-preview-stage video{width:100%;height:100%;object-fit:contain;background:#07131b}

/* Each review task opens directly as one 9:16 review card; no intermediate landscape cover. */
.review-item{padding:20px 0}
.review-item-head{align-items:center}
.review-item-head>.review-item-copy{display:grid;min-width:0;gap:4px}
.review-item-head>.review-item-copy>strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.review-actions{margin-top:14px}
.review-task-layout{display:grid;grid-template-columns:268px minmax(0,1fr);align-items:start;gap:24px;margin-top:16px}
.review-task-layout .review-inline-preview{width:268px;margin:0}
.review-task-context{min-width:0;padding-top:2px}
.review-task-context .submit-note{margin:0 0 18px}
.review-task-context .review-timeline{margin:0;max-width:100%;overflow-x:auto;padding:4px 0 10px}

@media(max-width:1400px){
  .asset-results{grid-template-columns:repeat(5,minmax(0,1fr))}
}
@media(max-width:1100px){
  .asset-results{grid-template-columns:repeat(4,minmax(0,1fr))}
  .review-task-layout{grid-template-columns:240px minmax(0,1fr)}
  .review-task-layout .review-inline-preview{width:240px}
}
@media(max-width:760px){
  .asset-results{grid-template-columns:repeat(3,minmax(0,1fr))}
  .asset-result-pages{align-items:flex-start;flex-direction:column}
  .review-task-layout{grid-template-columns:1fr}
  .review-task-layout .review-inline-preview{width:min(268px,100%)}
  .review-actions{align-items:stretch;flex-wrap:wrap}
  .review-actions>div{flex-basis:100%}
  .review-actions button{flex:1;justify-content:center}
}
@media(max-width:520px){
  .asset-results{grid-template-columns:repeat(2,minmax(0,1fr))}
  .asset-result-summary{align-items:flex-start;flex-direction:column;gap:4px}
  .review-item-head>.review-item-copy>strong{white-space:normal;overflow-wrap:anywhere}
}

/* Compact review wall: six complete 9:16 tasks per desktop row. */
.review-list-card{padding:18px}
.review-task-grid{
  display:grid;
  grid-template-columns:repeat(6,minmax(0,1fr));
  gap:14px;
  align-items:start;
}
.review-task-grid .review-item{
  display:flex;
  flex-direction:column;
  min-width:0;
  padding:0;
  overflow:hidden;
  border:1px solid #d7e5ed;
  border-radius:14px;
  background:#fff;
  transition:border-color .18s ease,box-shadow .18s ease,transform .18s ease;
}
.review-task-grid .review-item:hover{
  border-color:#93c5dc;
  box-shadow:0 12px 26px rgba(36,91,124,.1);
  transform:translateY(-2px);
}
.review-task-grid .review-item-head{
  display:flex;
  align-items:flex-start;
  gap:8px;
  min-height:76px;
  padding:12px;
  border-bottom:1px solid #e3edf2;
}
.review-task-grid .review-item-copy{display:grid;min-width:0;flex:1;gap:5px}
.review-task-grid .review-item-copy>strong{
  display:-webkit-box;
  overflow:hidden;
  min-height:36px;
  font-size:13px;
  line-height:1.38;
  white-space:normal;
  overflow-wrap:anywhere;
  -webkit-box-orient:vertical;
  -webkit-line-clamp:2;
}
.review-task-grid .review-item-copy>small{
  overflow:hidden;
  margin:0;
  color:#7890a1;
  font-size:11px;
  text-overflow:ellipsis;
  white-space:nowrap;
}
.review-task-grid .review-item-head>b{
  flex:0 0 auto;
  padding:5px 8px;
  font-size:11px;
  white-space:nowrap;
}
.review-task-grid .review-inline-preview{
  width:100%;
  margin:0;
  border-radius:0;
  box-shadow:none;
}
.review-task-grid .review-preview-meta{
  grid-template-columns:minmax(0,1fr) auto;
  gap:6px;
  padding:9px 10px;
  border-bottom:1px solid #e3edf2;
}
.review-task-grid .review-preview-meta strong{font-size:12px}
.review-task-grid .review-preview-meta small{font-size:10px}
.review-task-grid .review-preview-meta a{padding:6px 7px;font-size:10px}
.review-task-grid .review-preview-meta a svg{width:12px;height:12px}
.review-task-grid .review-actions{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:7px;
  margin:0;
  padding:10px;
  border-bottom:1px solid #e3edf2;
  border-radius:0;
  background:#eef8fd;
}
.review-task-grid .review-actions>div{grid-column:1/-1;display:grid;gap:6px}
.review-task-grid .review-actions>div strong{font-size:11px}
.review-task-grid .review-actions input{width:100%;box-sizing:border-box;height:34px;font-size:11px}
.review-task-grid .review-actions button{
  justify-content:center;
  width:100%;
  height:34px;
  padding:0 7px;
  font-size:11px;
}
.review-task-grid .submit-note{
  margin:0;
  padding:9px 10px;
  border-bottom:1px solid #e3edf2;
  border-radius:0;
  color:#5f7d90;
  background:#f8fbfd;
  font-size:11px;
  line-height:1.45;
}
.review-task-grid .review-timeline{
  display:grid;
  grid-template-columns:repeat(4,minmax(0,1fr));
  gap:4px;
  width:auto;
  margin:0;
  padding:12px 8px;
  overflow:visible;
}
.review-task-grid .review-step{width:auto;min-width:0;gap:3px}
.review-task-grid .review-step>span{width:22px;height:22px}
.review-task-grid .review-step>span svg{width:12px}
.review-task-grid .review-step strong{font-size:10px}
.review-task-grid .review-step small{
  overflow:hidden;
  width:100%;
  color:#7890a1;
  font-size:9px;
  text-overflow:ellipsis;
  white-space:nowrap;
}
.review-task-grid .review-step em{max-width:100%;font-size:9px;overflow-wrap:anywhere}
.review-task-grid .step-arrow{display:none}

.ai-review-card{display:grid;gap:8px;padding:10px;border-top:1px solid #dce9ef;border-bottom:1px solid #dce9ef;background:#f7fbfd}
.ai-review-card header{display:flex;align-items:center;justify-content:space-between;gap:8px}
.ai-review-card header>span{display:flex;align-items:center;gap:6px;min-width:0}
.ai-review-card header svg{width:15px;color:#1679ad}
.ai-review-card header strong{font-size:11px}
.ai-review-card header b{padding:4px 7px;border-radius:999px;color:#587486;background:#e8f0f4;font-size:9px;white-space:nowrap}
.ai-review-card.passed header b{color:#117453;background:#dff5ec}
.ai-review-card.warning header b{color:#8c6418;background:#fff1c9}
.ai-review-card.rejected header b,.ai-review-card.error header b{color:#ad3343;background:#ffe6e9}
.ai-review-card.disabled{background:#f5f8fa}.ai-review-card.disabled header b{color:#647d8b;background:#e6edf1}
.ai-review-card p{margin:0;color:#607d8e;font-size:10px;line-height:1.45}
.ai-category-counts{display:grid;grid-template-columns:repeat(4,1fr);gap:4px}
.ai-category-counts span{padding:5px 3px;border-radius:7px;color:#557386;background:#eaf3f7;font-size:9px;text-align:center}
.ai-review-card details{font-size:10px}.ai-review-card summary{color:#1375a5;font-weight:800;cursor:pointer}
.ai-review-card ul{display:grid;gap:6px;margin:7px 0 0;padding:0;list-style:none}
.ai-review-card li{display:grid;gap:2px;padding:7px;border:1px solid #f0dfb6;border-radius:7px;background:#fff}
.ai-review-card li.hard{border-color:#f0c8ce;background:#fff7f8}
.ai-review-card li span{color:#728b9a;font-size:9px}.ai-review-card li strong{font-size:10px}.ai-review-card li em{color:#5b7485;font-size:9px;font-style:normal;overflow-wrap:anywhere}.ai-review-card li p{color:#436b80;font-size:9px}.ai-review-card li a{display:flex;align-items:center;gap:3px;color:#1479aa;font-size:9px;font-weight:800;text-decoration:none}.ai-review-card li a svg{width:11px}
.ai-error{display:grid;gap:6px}.ai-error>span{color:#a43b49;font-size:9px;overflow-wrap:anywhere}
.ai-error button{display:flex;align-items:center;justify-content:center;gap:5px;height:29px;border:1px solid #e6b6bc;border-radius:8px;color:#a43b49;background:#fff;font-size:10px;font-weight:800}.ai-error button svg{width:13px}
.quality-score-grid{grid-column:1/-1;display:grid;grid-template-columns:repeat(4,1fr);gap:5px}
.quality-score-grid label{display:grid;gap:3px;color:#607d8e;font-size:9px}.quality-score-grid input{box-sizing:border-box;width:100%;height:30px!important;padding:0 5px!important;text-align:center}
.quality-score-grid>b{grid-column:1/-1;padding:5px;border-radius:7px;color:#146f98;background:#dff1f9;font-size:10px;text-align:center}
.review-note-input{grid-column:1/-1;width:100%;box-sizing:border-box}
.review-actions>div>small{color:#698696;font-size:9px;line-height:1.4}
.review-step.ai-passed>span,.review-step.ai-warning>span{color:#fff;background:#16a477}.review-step.ai-warning>span{background:#d69a22}
.review-step.ai-rejected>span,.review-step.ai-error>span{color:#fff;background:#d84c5b}
.review-step.disabled>span,.review-step.ai-disabled>span{color:#69818f;background:#e6edf1}
.review-step.reserved>span{color:#6c8494;background:#edf3f6}.review-step a{color:#1479aa;font-size:9px;font-weight:800;text-decoration:none}
.review-task-grid .review-timeline{grid-template-columns:repeat(auto-fit,minmax(48px,1fr))}
.review-gate-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.review-gate-switch{position:relative;display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:11px;min-width:0;padding:15px;border:1px solid #d7e6ee;border-radius:14px;color:#365d74;background:#f8fbfc;cursor:pointer;transition:border-color .18s ease,background .18s ease,box-shadow .18s ease}
.review-gate-switch:hover{border-color:#9bc8df;background:#f3faff}.review-gate-switch.active{border-color:#79c8ac;color:#174f3e;background:#effaf5;box-shadow:0 8px 20px rgba(22,118,83,.08)}
.review-gate-switch>div{display:grid;gap:4px;min-width:0}.review-gate-switch strong{font-size:14px}.review-gate-switch small{color:#708b9b;font-size:11px;line-height:1.45;overflow-wrap:anywhere}.review-gate-switch.active small{color:#527a6c}
.review-gate-index{display:grid;place-items:center;width:27px;height:27px;border-radius:50%;color:#fff;background:#7895a5;font-size:11px;font-weight:900}.review-gate-switch.active .review-gate-index{background:#15976f}
.review-gate-switch>input{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}.review-gate-switch>i{position:relative;width:44px;height:24px;border-radius:999px;background:#bdcbd3;box-shadow:inset 0 0 0 1px rgba(52,86,104,.08);transition:background .18s ease}.review-gate-switch>i:after{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 2px 5px rgba(26,61,80,.22);transition:transform .18s ease}.review-gate-switch>input:checked+i{background:#15976f}.review-gate-switch>input:checked+i:after{transform:translateX(20px)}.review-gate-switch>input:focus-visible+i{outline:3px solid rgba(20,136,200,.24);outline-offset:3px}
.workflow-actions{display:flex;align-items:stretch;gap:12px;margin-top:14px}.workflow-actions .workflow-summary{display:grid;flex:1;gap:3px;margin:0;padding:11px 13px}.workflow-actions .workflow-summary span{font-weight:800;color:#315d75}.workflow-actions .workflow-summary small{color:#7890a1}.workflow-actions .save-workflow{flex:none;min-width:150px}
.fixed-stage-flow{display:flex;align-items:center;justify-content:center;gap:7px;flex:1}
.fixed-stage-flow span,.fixed-stage-flow a{display:grid;gap:2px;min-width:92px;padding:9px 11px;border:1px solid #d4e5ee;border-radius:11px;color:#244e67;background:#f9fcfd;text-decoration:none}
.fixed-stage-flow b{display:grid;place-items:center;width:20px;height:20px;border-radius:50%;color:#fff;background:#1488c8;font-size:10px}.fixed-stage-flow small{color:#77909f;font-size:10px}.fixed-stage-flow i{color:#91a9b7;font-style:normal}
.fixed-stage-flow a{border-style:dashed;color:#657f8f;background:#fbfcfd}
.fixed-stage-flow{margin-top:13px;padding-top:13px;border-top:1px solid #e3edf2}.fixed-stage-flow .inactive{color:#77909f;background:#f5f7f8;opacity:.72}.fixed-stage-flow .inactive b{background:#8ca0ab}
.stage-state-pill{padding:4px 7px;border-radius:999px;font-size:10px}.stage-state-pill.enabled{color:#0b7453;background:#def5eb}.stage-state-pill.disabled{color:#637d8c;background:#e9eff2}
.review-stage-off-note{display:flex;align-items:flex-start;gap:9px;padding:12px;border:1px solid #d9e5eb;border-radius:12px;color:#587383;background:#f5f8fa}.review-stage-off-note svg{flex:none;width:17px}.review-stage-off-note span{display:grid;gap:3px}.review-stage-off-note small{color:#7890a1}
.redline-standards{margin-top:16px;padding:16px;border:1px solid #d7e7ef;border-radius:15px;background:#fbfdfe}
.redline-standards>header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.redline-standards>header>span{display:flex;align-items:center;gap:8px}.redline-standards>header svg{width:18px;color:#cf4c5a}.redline-standards>header small{color:#7890a1}
.redline-policy-source{margin:0 0 12px;padding:9px 11px;border-radius:9px;color:#587688;background:#eef7fb;font-size:11px}.redline-policy-source a{display:inline-flex;align-items:center;gap:3px;color:#1479aa;font-weight:800;text-decoration:none}.redline-policy-source svg{width:12px}
.redline-standard-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.redline-standard-grid>article{min-width:0;padding:13px;border:1px solid #e1ebf1;border-radius:12px;background:#fff}
.redline-standard-grid>article>header{display:grid;gap:3px}.redline-standard-grid>article>header small{color:#8297a5;font-size:11px}
.redline-standard-grid ul{display:grid;gap:8px;margin:11px 0 0;padding:0;list-style:none}.redline-standard-grid li{display:flex;align-items:flex-start;gap:8px;min-width:0}.redline-standard-grid li>b{flex:none;margin-top:1px;padding:3px 6px;border-radius:99px;font-size:10px}.redline-standard-grid li>b.hard{color:#b83a47;background:#ffe8eb}.redline-standard-grid li>b.warning{color:#95651a;background:#fff1cf}
.redline-standard-grid li>span{display:grid;min-width:0}.redline-standard-grid li>span>strong{font-size:12px}.redline-standard-grid li>span>small{overflow:hidden;color:#8095a3;font-size:10px;text-overflow:ellipsis;white-space:nowrap}
.redline-standards>footer{display:flex;flex-wrap:wrap;gap:8px 20px;margin-top:13px;padding-top:11px;border-top:1px solid #e2edf2;color:#6e8797;font-size:11px}.redline-standards>footer b{color:#274e66}
.redline-editor-card{padding:22px;border:1px solid #dcebf4;border-radius:20px;background:#fff}
.redline-editor-note{display:flex;align-items:flex-start;gap:8px;margin-bottom:12px;padding:11px 13px;border-radius:11px;color:#7f6127;background:#fff8e8}.redline-editor-note svg{flex:none;width:17px}.redline-editor-note b{color:#965e11}
.redline-rule-list{display:grid;gap:8px}.redline-rule-list>article{display:grid;grid-template-columns:82px 120px 120px minmax(180px,1fr) minmax(280px,1.7fr) 38px;align-items:center;gap:8px;padding:10px;border:1px solid #dce9f1;border-radius:12px;background:#fbfdfe}.redline-rule-list>article.disabled{opacity:.62;background:#f4f7f8}
.redline-rule-list select,.redline-rule-list input,.redline-rule-list textarea{box-sizing:border-box;width:100%;min-width:0;color:#173f5c;border:1px solid #cfe0e9;border-radius:9px;background:#fff;outline:none}.redline-rule-list select,.redline-rule-list input{height:39px;padding:0 10px}.redline-rule-list textarea{resize:vertical;padding:9px 10px;line-height:1.35}
.redline-enabled{display:grid;justify-items:center;gap:3px;color:#607b8d;font-size:11px}.redline-enabled input{width:17px;height:17px;accent-color:#118bcf}.redline-delete{display:grid;place-items:center;width:36px;height:36px;color:#b6404d;border:1px solid #f0cbd0;border-radius:9px;background:#fff}.redline-delete svg{width:15px}
.redline-editor-actions{display:flex;align-items:center;gap:12px;margin-top:13px}.redline-editor-actions>span{margin-right:auto;color:#79909f;font-size:12px}.redline-editor-actions button{display:flex;align-items:center;justify-content:center;gap:7px;height:40px;padding:0 15px;border-radius:10px;font-weight:800}.redline-editor-actions button svg{width:16px}.add-redline{color:#176f99;border:1px solid #cce0eb;background:#f4fafd}.save-redlines{color:#fff;border:1px solid #0e88cf;background:#0e88cf}.save-redlines:disabled{opacity:.6}
.role-grid article.reserved{border-style:dashed;background:#fbfcfd}.legacy-role-records{padding:14px 18px;border:1px dashed #cadbe4;border-radius:14px;color:#617b8c;background:#f9fbfc}.legacy-role-records summary{font-weight:800;cursor:pointer}.legacy-role-records p{margin:8px 0 0;font-size:12px}

.asset-result-summary>div{display:flex;gap:7px;margin-left:auto}.asset-result-summary>div button{padding:7px 10px;border:1px solid #c9dfe9;border-radius:8px;color:#176f98;background:#fff;font-size:11px;font-weight:800}.asset-result-summary>div button:disabled{opacity:.45}.submit-review-shell{display:grid;gap:12px;margin-top:14px;padding:15px;border:1px solid #cfe3ee;border-radius:16px;background:#f7fbfd}
.submit-review-shell>header{display:flex;align-items:center;justify-content:space-between;gap:12px}.submit-review-shell>header>span{display:grid;gap:3px}.submit-review-shell>header small{color:#718a9a}.submit-review-shell>header a{display:flex;align-items:center;gap:5px;color:#147aa9;font-size:12px;font-weight:800;text-decoration:none}.submit-review-shell>header svg{width:15px}
.naming-review-form{display:grid;gap:11px;padding:14px;border:1px solid #cfe6da;border-radius:13px;background:#f5fcf8}.naming-form-title{display:flex;align-items:center;justify-content:space-between;gap:12px}.naming-form-title>span{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:2px 7px}.naming-form-title svg{grid-row:1/3;width:18px;color:#11865f}.naming-form-title small{color:#658477;font-size:11px}.naming-form-title>b{padding:5px 8px;border-radius:99px;color:#8b641b;background:#fff1cb;font-size:10px}.naming-loading{display:flex;align-items:center;gap:7px;color:#5f7d70}.naming-loading svg{width:16px}
.naming-none{display:flex;align-items:center;gap:9px;padding:10px;border:1px dashed #b9d7c8;border-radius:10px;background:#fff}.naming-none input{width:17px;height:17px;accent-color:#11865f}.naming-none span{display:grid}.naming-none small{color:#759085;font-size:10px}
.naming-evidence-list{display:grid;gap:8px}.naming-evidence-list>article{display:grid;grid-template-columns:26px minmax(190px,1.5fr) repeat(3,minmax(115px,1fr)) 34px;align-items:end;gap:7px;padding:9px;border:1px solid #d8e9e0;border-radius:11px;background:#fff}.naming-evidence-list label{display:grid;gap:4px}.naming-evidence-list label small{color:#6f877d;font-size:10px}.naming-evidence-list input,.naming-evidence-list select{box-sizing:border-box;width:100%;height:36px;padding:0 9px;border:1px solid #cbded5;border-radius:8px;background:#fff;outline:none}.naming-row-index{display:grid;place-items:center;width:24px;height:24px;margin-bottom:6px;border-radius:50%;color:#fff;background:#1489c8;font-size:10px;font-weight:800}.naming-evidence-list>article>button{display:grid;place-items:center;width:32px;height:32px;margin-bottom:2px;border:1px solid #efcbd0;border-radius:8px;color:#b7424e;background:#fff}.naming-evidence-list>article>button svg{width:14px}.add-naming-evidence{display:flex;align-items:center;justify-content:center;gap:6px;height:36px;border:1px dashed #9bc7b1;border-radius:9px;color:#167454;background:#f9fffb;font-weight:800}.add-naming-evidence svg{width:15px}.submit-review-shell .submit-confirm{position:sticky;bottom:12px;z-index:6;margin:0;border:1px solid #c3dce8;box-shadow:0 12px 30px rgba(23,81,113,.16)}
.batch-naming-confirm{display:flex;align-items:center;gap:12px;padding:11px 13px;border:1px solid #d8e7df;border-radius:11px;background:#f6fbf8}.batch-naming-confirm>span{display:grid;flex:1;gap:3px}.batch-naming-confirm small{color:#6f877c}.batch-naming-confirm button{display:flex;align-items:center;gap:6px;height:36px;padding:0 12px;border:1px solid #a8cfbc;border-radius:9px;color:#167454;background:#fff;font-weight:800}.batch-naming-confirm button.confirmed{color:#fff;border-color:#13885f;background:#13885f}.batch-naming-confirm svg{width:15px}
.review-toolbar{flex-wrap:wrap}.review-toolbar label{flex:1 1 250px}.review-toolbar select{min-width:118px}.review-batch-bar{display:grid;grid-template-columns:auto minmax(220px,1fr) auto auto;align-items:center;gap:8px;margin-bottom:14px;padding:10px;border:1px solid #cfe1eb;border-radius:12px;background:#f3f9fc}.review-batch-bar input{height:38px;padding:0 11px;border:1px solid #cbdfe9;border-radius:9px;background:#fff}.review-batch-bar button{display:flex;align-items:center;justify-content:center;gap:5px;height:38px;padding:0 12px;border-radius:9px;font-weight:800}.review-batch-bar button svg{width:15px}.batch-select{color:#176f98;border:1px solid #bad8e7;background:#fff}.batch-reject{color:#b43d4b;border:1px solid #ebc3c9;background:#fff}.batch-approve{color:#fff;border:1px solid #118e68;background:#118e68}.review-item.batch-selected{border-color:#39a7db;box-shadow:0 0 0 2px rgba(25,143,202,.12)}.review-batch-check{display:grid;place-items:center;flex:none;width:25px;height:25px;border:1px solid #bcd7e5;border-radius:7px;color:#fff;background:#fff}.review-batch-check[aria-pressed="true"]{border-color:#148bc9;background:#148bc9}.review-batch-check svg{width:14px}
.naming-check-card{display:grid;gap:6px;padding:10px;border-top:1px solid #dce9ef;background:#f8fbfc}.naming-check-card header{display:flex;align-items:center;justify-content:space-between;gap:7px}.naming-check-card header>span{display:flex;align-items:center;gap:5px}.naming-check-card header svg{width:14px}.naming-check-card header strong{font-size:10px}.naming-check-card header b{padding:3px 6px;border-radius:99px;font-size:9px}.naming-check-card.passed,.naming-check-card.not_applicable{background:#f0faf6}.naming-check-card.passed header b,.naming-check-card.not_applicable header b{color:#087252;background:#d9f2e8}.naming-check-card.failed,.naming-check-card.stale{background:#fff6f7}.naming-check-card.failed header b,.naming-check-card.stale header b{color:#ac3947;background:#ffe0e4}.naming-check-card p{margin:0;color:#627d8d;font-size:9px;line-height:1.45}.naming-required-names{display:flex;flex-wrap:wrap;align-items:center;gap:4px;color:#688191;font-size:9px}.naming-required-names b{padding:3px 5px;border-radius:5px;color:#176c92;background:#e2f1f8}.naming-check-card details{font-size:9px}.naming-check-card summary{color:#1477a4;font-weight:800;cursor:pointer}.naming-check-card ul{display:grid;gap:4px;margin:6px 0 0;padding:0;list-style:none}.naming-check-card li{display:grid;gap:1px;padding:5px;border-radius:6px;background:#fff}.naming-check-card li span{color:#728896}
.naming-check-card.disabled{background:#f5f8fa}.naming-check-card.disabled header b{color:#637d8c;background:#e6edf1}
.review-step.passed>span,.review-step.not_applicable>span{color:#fff;background:#16a477}.review-step.failed>span,.review-step.stale>span{color:#fff;background:#d84c5b}
.naming-standard-card{display:grid;gap:12px;margin-top:16px;padding:16px;border:1px solid #cfe5da;border-radius:15px;background:#f7fcf9}.naming-standard-card>header{display:flex;align-items:center;justify-content:space-between}.naming-standard-card>header>span,.naming-standard-card>header>a{display:flex;align-items:center;gap:7px}.naming-standard-card>header svg{width:17px}.naming-standard-card>header>a{color:#1479a7;font-size:11px;font-weight:800;text-decoration:none}.naming-standard-card>div{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px}.naming-standard-card article{padding:10px;border:1px solid #dceae3;border-radius:10px;background:#fff}.naming-standard-card article strong{font-size:11px}.naming-standard-card article p{margin:5px 0 0;color:#6c8579;font-size:9px;line-height:1.45}.naming-standard-card article.pending{border-style:dashed;background:#fffaf0}.naming-standard-card article.pending strong{color:#90651b}

.role-form{grid-template-columns:130px 1fr 1fr 150px 1fr 145px}
.submit-selected-assets{display:flex;align-items:center;gap:7px;overflow-x:auto;padding-bottom:2px}.submit-selected-assets>strong{flex:none;color:#24536d;font-size:12px}.submit-selected-assets button{display:grid;flex:0 0 170px;min-width:0;padding:8px 10px;border:1px solid #d3e3eb;border-radius:10px;color:#567384;background:#fff;text-align:left}.submit-selected-assets button.active{color:#0d6f9f;border-color:#55add7;background:#eef8fd;box-shadow:0 0 0 2px rgba(20,139,201,.1)}.submit-selected-assets button span{overflow:hidden;font-size:11px;font-weight:800;text-overflow:ellipsis;white-space:nowrap}.submit-selected-assets button small{color:#8498a5;font-size:9px}
.review-routing{display:grid;gap:5px;padding:9px 10px;border-top:1px solid #dce9ef;color:#587486;background:#f1f8fb}.review-routing>span{display:flex;align-items:center;flex-wrap:wrap;gap:5px;font-size:10px}.review-routing svg{width:14px;color:#1384bd}.review-routing b{padding:3px 6px;border-radius:99px;color:#0c7152;background:#ddf3ea}.review-routing em{font-style:normal;color:#9b6b21}
.review-organization-card{padding:22px;border:1px solid #dcebf4;border-radius:20px;background:#fff}.organization-source{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:-7px 0 13px;padding:8px 10px;border-radius:9px;color:#668192;background:#f3f8fb;font-size:10px}.organization-source a{display:flex;align-items:center;gap:4px;color:#1479a7;font-weight:800;text-decoration:none}.organization-source svg{width:13px}.review-organization-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}.review-organization-grid article{padding:13px;border:1px solid #d9e8ef;border-radius:13px;background:#f8fbfc}.review-organization-grid header{display:flex;align-items:center;justify-content:space-between;gap:8px}.review-organization-grid header>b{padding:4px 7px;border-radius:99px;color:#176f98;background:#e3f2f9;font-size:10px}.review-organization-grid p{display:grid;gap:4px;margin:10px 0 0;font-size:11px}.review-organization-grid p span{color:#7a909e}.review-organization-grid p b{color:#294f65;overflow-wrap:anywhere}.review-organization-grid .center-supervisor{padding:8px;border:1px solid #cbe4d9;border-radius:9px;background:#f0faf6}.review-organization-grid .center-supervisor b{color:#087451}.center-groups{display:grid;gap:6px;margin-top:8px}.center-groups p{margin:0;padding:7px;border:1px solid #dce9ef;border-radius:8px;background:#fff}.center-groups p span{font-size:9px}.center-groups p b{font-size:10px}

/* The application theme has global white button text; keep queue pagination legible. */
.review-pages{display:flex;justify-content:center;align-items:center;gap:12px;padding-top:18px}
.review-pages button{min-width:88px;padding:8px 16px;border:1px solid #bcd5e4!important;border-radius:9px;color:#126b9a!important;background:#fff!important;font:inherit;font-weight:800;line-height:1.35;cursor:pointer}
.review-pages button:hover:not(:disabled){color:#fff!important;border-color:#0d89d3!important;background:#0d89d3!important}
.review-pages button:disabled{color:#93a6b4!important;border-color:#dce6eb!important;background:#f2f5f7!important;opacity:1;cursor:not-allowed}
.review-pages>span{min-width:112px;color:#244f69!important;font-size:14px;font-weight:800;text-align:center;white-space:nowrap}

@media(max-width:1360px){.review-task-grid{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:1200px){.redline-rule-list>article{grid-template-columns:82px 1fr 1fr 38px}.redline-rule-list .redline-title-input,.redline-rule-list textarea{grid-column:span 2}.naming-evidence-list>article{grid-template-columns:26px 1fr 1fr}.naming-evidence-list>article>button{grid-column:3}.naming-standard-card>div{grid-template-columns:repeat(3,1fr)}.review-organization-grid{grid-template-columns:repeat(3,1fr)}.role-form{grid-template-columns:repeat(3,1fr)}}
@media(max-width:1000px){.review-task-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.review-gate-grid{grid-template-columns:1fr}.fixed-stage-flow{flex-wrap:wrap}.fixed-stage-flow i{display:none}.redline-standard-grid{grid-template-columns:1fr}}
@media(max-width:760px){.review-task-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:520px){
  .review-list-card{padding:14px}
  .review-task-grid{grid-template-columns:1fr}
  .review-task-grid .review-item-head{min-height:0}
  .review-batch-bar{grid-template-columns:1fr}.naming-standard-card>div,.review-organization-grid,.role-form{grid-template-columns:1fr}.naming-evidence-list>article{grid-template-columns:1fr}.naming-row-index{display:none}.naming-evidence-list>article>button{grid-column:auto;justify-self:end}.naming-form-title,.submit-review-shell>header{align-items:flex-start;flex-direction:column}
  .redline-rule-list>article{grid-template-columns:1fr}.redline-rule-list .redline-title-input,.redline-rule-list textarea{grid-column:auto}.redline-delete{justify-self:end}.redline-editor-actions{align-items:stretch;flex-direction:column}.redline-editor-actions>span{margin-right:0}
  .workflow-actions{flex-direction:column}.workflow-actions .save-workflow{min-height:43px}.review-gate-switch{grid-template-columns:auto minmax(0,1fr)}.review-gate-switch>i{grid-column:1/-1;justify-self:end;margin-top:-33px}
}
</style>
