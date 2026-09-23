<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { BarChart3, CheckCircle2, Flame, LoaderCircle, Maximize2, QrCode, RefreshCw, Search, ShieldCheck, Trash2, X } from 'lucide-vue-next'
import { ApiError, api } from '../api'
import type { ChannelsAccount, ChannelsAuthSession, ChannelsStatus, ChannelsTask } from '../types'
import PaginationControls from './PaginationControls.vue'
import ChannelsPromotionModal from './ChannelsPromotionModal.vue'
import ChannelsEditModal from './ChannelsEditModal.vue'
import DateRangeFilter from './DateRangeFilter.vue'
import { authorizationPhase, isAuthorizationTerminal, normalizeAuthorizationSession } from '../utils/channelsAuthorization'

const statusInfo = ref<ChannelsStatus | null>(null)
const accounts = ref<ChannelsAccount[]>([])
const tasks = ref<ChannelsTask[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref<5 | 10>(10)
const pages = ref(1)
const allRecords = ref(false)
const canViewAll = ref(false)
const recordScope = ref<'mine' | 'all'>('mine')
const q = ref('')
const status = ref('all')
const pushStartDate = ref('')
const pushEndDate = ref('')
const loading = ref(true)
const error = ref('')
const reconnecting = ref(false)
const authSession = ref('')
const authMessage = ref('')
const authStatus = ref('starting')
const authPhase = computed(() => authorizationPhase(authStatus.value, captureMode.value, accountChoices.value.length))
const qrReady = ref(false)
const qrSrc = ref('')
const captureRevision = ref(0)
const qrExpanded = ref(false)
const interactionRequired = ref(false)
const captureMode = ref<'qr' | 'account_list' | 'account_choice'>('qr')
const interacting = ref(false)
const interactionAction = ref<'click' | 'scroll' | 'select'>('click')
const accountChoices = ref<ChannelsAuthSession['account_choices']>([])
const choiceMessage = ref('')
const busy = ref('')
const syncingMetrics = ref(false)
const syncMessage = ref('')
const promotionTask = ref<ChannelsTask | null>(null)
const editTask = ref<ChannelsTask | null>(null)
const promotionAccount = computed(() => {
  if (!promotionTask.value) return null
  const publishingAccount = accounts.value.find(account => account.id === promotionTask.value?.account_id)
  if (publishingAccount?.promotion.authorized) return publishingAccount
  return accounts.value.find(account => account.promotion.authorized)
    || publishingAccount
    || accounts.value[0]
    || null
})
const promotionAuthAccount = ref<ChannelsAccount | null>(null)
const cancelConfirmTask = ref<ChannelsTask | null>(null)
let authTimer: ReturnType<typeof setInterval> | undefined
let refreshTimer: ReturnType<typeof setInterval> | undefined
let searchTimer: ReturnType<typeof setTimeout> | undefined
let accountScrollTimer: ReturnType<typeof setTimeout> | undefined
let reconnectTimer: ReturnType<typeof setTimeout> | undefined
let pendingAccountScroll = 0
let authPollingSession = ''
let authReadFailures = 0
let reconnectAttempt = 0
let loadRevision = 0

const transientReadStatuses = new Set([0, 408, 425, 429, 500, 502, 503, 504])
const isTransientReadError = (value: unknown) => value instanceof ApiError && transientReadStatuses.has(value.status)
const readErrorMessage = (value: unknown) => value instanceof Error ? value.message : '请求失败'

const clearReconnectTimer = () => {
  if (reconnectTimer) clearTimeout(reconnectTimer)
  reconnectTimer = undefined
}

const scheduleReconnect = () => {
  if (reconnectTimer) return
  const delay = Math.min(12000, 1600 * (2 ** Math.min(reconnectAttempt, 3)))
  reconnectAttempt += 1
  reconnectTimer = setTimeout(() => {
    reconnectTimer = undefined
    void load(true)
  }, delay)
}

const changePage = (nextPage: number) => {
  if (nextPage === page.value) return
  page.value = nextPage
  void load(true)
}

const openPromotion = (task: ChannelsTask) => {
  promotionTask.value = task
  if (promotionAccount.value) return
  promotionTask.value = null
  error.value = '请先授权一个视频号，并完成企业加热账户授权'
}

const paintAuthCapture = (sessionId: string, revision: number, force = false) => {
  if (!sessionId || (!force && revision <= captureRevision.value)) return
  const suffix = force ? `&manual=${Date.now()}` : ''
  const nextSrc = `${api.channelsQrUrl(sessionId, revision)}${suffix}`
  const preload = new Image()
  preload.onload = () => {
    if (authSession.value !== sessionId) return
    qrSrc.value = nextSrc
    captureRevision.value = Math.max(captureRevision.value, revision)
    qrReady.value = true
  }
  preload.onerror = () => {
    if (!qrSrc.value) qrReady.value = false
  }
  preload.src = nextSrc
}

const load = async (quiet = false) => {
  const revision = ++loadRevision
  if (!quiet && !statusInfo.value && !accounts.value.length && !tasks.value.length) loading.value = true
  const [statusResult, accountResult, taskResult] = await Promise.allSettled([
    api.channelsStatus(),
    api.channelsAccounts(),
    api.channelsTasks(q.value, status.value, page.value, pageSize.value, recordScope.value, pushStartDate.value, pushEndDate.value),
  ])
  if (revision !== loadRevision) return

  if (statusResult.status === 'fulfilled') statusInfo.value = statusResult.value
  if (accountResult.status === 'fulfilled') accounts.value = accountResult.value.items
  if (taskResult.status === 'fulfilled') {
    const taskData = taskResult.value
    tasks.value = taskData.items
    total.value = taskData.total
    pages.value = Math.max(1, taskData.total_pages)
    allRecords.value = taskData.viewer_scope === 'all'
    canViewAll.value = taskData.can_view_all
    if (page.value > pages.value) {
      page.value = pages.value
      void load(true)
    }
  }

  const failures: Array<{ label: string; result: PromiseRejectedResult }> = []
  const collectFailure = (label: string, result: PromiseSettledResult<unknown>) => {
    if (result.status === 'rejected') failures.push({ label, result })
  }
  collectFailure('服务器状态', statusResult)
  collectFailure('视频号账号', accountResult)
  collectFailure('发布记录', taskResult)

  if (!failures.length) {
    reconnecting.value = false
    reconnectAttempt = 0
    clearReconnectTimer()
    error.value = ''
  } else if (failures.every(({ result }) => isTransientReadError(result.reason))) {
    reconnecting.value = true
    error.value = ''
    scheduleReconnect()
  } else {
    reconnecting.value = false
    clearReconnectTimer()
    error.value = failures.map(({ label, result }) => `${label}：${readErrorMessage(result.reason)}`).join('；')
  }
  loading.value = false
}
const pollAuthSession = async (sessionId: string) => {
  if (authPollingSession === sessionId || authSession.value !== sessionId) return
  authPollingSession = sessionId
  try {
    const current = normalizeAuthorizationSession(await api.channelsAuthSession(sessionId))
    if (authSession.value !== sessionId) return
    authReadFailures = 0
    applyAuthState(current)
    if (current.qr_ready) {
      paintAuthCapture(sessionId, Number(current.capture_revision || 0))
    } else if (!qrSrc.value) {
      qrReady.value = false
    }
    if (isAuthorizationTerminal(current.status)) {
      if (current.status === 'authorized') {
        authSession.value = ''
        qrReady.value = false
        qrSrc.value = ''
        captureRevision.value = 0
        qrExpanded.value = false
        interactionRequired.value = false
        captureMode.value = 'qr'
        await load(true)
      }
    }
  } catch (e) {
    if (authSession.value !== sessionId) return
    authReadFailures += 1
    if (!isTransientReadError(e) || authReadFailures >= 3) {
      applyAuthState({ id: sessionId, status: 'failed', message: '授权状态暂时无法读取，请重新扫码；不会自动发布任何内容。', qr_ready: false, capture_revision: 0, account_id: '', interaction_required: false, capture_mode: 'qr', account_choices: [], updated_at: '' })
    } else {
      authMessage.value = '登录状态连接中断，正在重新读取…'
    }
  } finally {
    if (authPollingSession === sessionId) authPollingSession = ''
  }
}
const applyAuthState = (raw: ChannelsAuthSession) => {
  const current = normalizeAuthorizationSession(raw)
  authStatus.value = current.status
  authMessage.value = current.message
  interactionRequired.value = current.interaction_required
  captureMode.value = current.capture_mode
  accountChoices.value = current.account_choices || []
  if (isAuthorizationTerminal(current.status)) {
    if (authTimer) clearInterval(authTimer)
    authTimer = undefined
    if (accountScrollTimer) clearTimeout(accountScrollTimer)
    accountScrollTimer = undefined
    pendingAccountScroll = 0
    choiceMessage.value = ''
    interacting.value = false
    qrReady.value = false
    qrSrc.value = ''
    qrExpanded.value = false
    busy.value = ''
  } else if (current.status === 'verifying') {
    choiceMessage.value = ''
  }
}
const startAuth = async () => {
  if (authTimer) clearInterval(authTimer)
  authSession.value = ''; authStatus.value = 'starting'; authReadFailures = 0
  busy.value = 'auth'; error.value = ''; choiceMessage.value = ''; qrReady.value = false; qrSrc.value = ''; captureRevision.value = 0; qrExpanded.value = false; interactionRequired.value = false; captureMode.value = 'qr'; accountChoices.value = []
  try {
    const data = await api.channelsAuthStart()
    authSession.value = data.id; applyAuthState(data)
    await pollAuthSession(data.id)
    if (authSession.value === data.id && !isAuthorizationTerminal(authStatus.value)) authTimer = setInterval(() => void pollAuthSession(data.id), 1200)
  } catch (e) { error.value = e instanceof Error ? e.message : '无法发起视频号授权'; busy.value = '' }
}
const refreshQr = () => {
  if (!authSession.value) return
  paintAuthCapture(authSession.value, captureRevision.value, true)
}

const screenshotPoint = (element: HTMLImageElement, clientX: number, clientY: number) => {
  const rect = element.getBoundingClientRect()
  const sourceWidth = element.naturalWidth || 1280
  const sourceHeight = element.naturalHeight || 900
  if (!rect.width || !rect.height || !sourceWidth || !sourceHeight) return null
  const scale = Math.min(rect.width / sourceWidth, rect.height / sourceHeight)
  const contentWidth = sourceWidth * scale
  const contentHeight = sourceHeight * scale
  const contentLeft = rect.left + (rect.width - contentWidth) / 2
  const contentTop = rect.top + (rect.height - contentHeight) / 2
  const localX = clientX - contentLeft
  const localY = clientY - contentTop
  if (localX < 0 || localY < 0 || localX > contentWidth || localY > contentHeight) return null
  return { x: localX / contentWidth, y: localY / contentHeight }
}

const sendChannelsInteraction = async (action: 'click' | 'scroll' | 'select', x: number, y: number, deltaY = 0, choiceId = '') => {
  if (!authSession.value || !interactionRequired.value || interacting.value) return
  interacting.value = true
  interactionAction.value = action
  choiceMessage.value = action.includes('scroll') ? '正在加载更多账号…' : '已精确选择账号，正在登录…'
  error.value = ''
  try {
    const sessionId = authSession.value
    const current = await api.channelsAuthInteract(sessionId, x, y, action, deltaY, choiceId)
    if (authSession.value !== sessionId || isAuthorizationTerminal(authStatus.value)) return
    applyAuthState(current)
    setTimeout(() => void pollAuthSession(sessionId), 320)
  } catch (e) {
    choiceMessage.value = ''
    error.value = e instanceof Error ? e.message : action.includes('scroll') ? '账号列表滚动失败，请重试' : '视频号选择未完成，请重试'
  } finally {
    interacting.value = false
  }
}
const selectChannelsAccount = (choiceId: string) => void sendChannelsInteraction('select', 0.5, 0.5, 0, choiceId)
const flushAccountScroll = async () => {
  accountScrollTimer = undefined
  if (!pendingAccountScroll || interacting.value) {
    if (pendingAccountScroll) accountScrollTimer = setTimeout(() => void flushAccountScroll(), 140)
    return
  }
  const deltaY = Math.max(-2400, Math.min(2400, pendingAccountScroll))
  pendingAccountScroll = 0
  await sendChannelsInteraction('scroll', 0.79, 0.58, deltaY)
  if (pendingAccountScroll) accountScrollTimer = setTimeout(() => void flushAccountScroll(), 140)
}
const queueAccountScroll = (deltaY: number) => {
  pendingAccountScroll = Math.max(-3000, Math.min(3000, pendingAccountScroll + deltaY))
  if (!accountScrollTimer) accountScrollTimer = setTimeout(() => void flushAccountScroll(), 140)
}
const chooseChannelsAccount = async (event: MouseEvent) => {
  const element = event.currentTarget as HTMLImageElement
  const point = screenshotPoint(element, event.clientX, event.clientY)
  if (!point) {
    choiceMessage.value = '请点击黑色的视频号页面，左右浅色留白区域不会响应。'
    return
  }
  await sendChannelsInteraction('click', point.x, point.y)
}
const scrollChannelsAccountList = (event: WheelEvent) => {
  const element = event.currentTarget as HTMLImageElement
  const point = screenshotPoint(element, event.clientX, event.clientY)
  if (!point) {
    choiceMessage.value = '请把鼠标移到黑色的视频号页面内再滚动。'
    return
  }
  const deltaY = event.deltaY >= 0 ? 720 : -720
  queueAccountScroll(deltaY)
}
const onKeydown = (event: KeyboardEvent) => {
  if (event.key !== 'Escape') return
  qrExpanded.value = false
  cancelConfirmTask.value = null
}
const retry = async (task: ChannelsTask) => { busy.value = task.id; try { await api.channelsRetry(task.id); await load(true) } catch (e) { error.value = e instanceof Error ? e.message : '重试失败' } finally { busy.value = '' } }
const cancelTask = async (task: ChannelsTask) => {
  busy.value = task.id; error.value = ''
  try {
    const result = await api.channelsCancel(task.id)
    syncMessage.value = result.message
    cancelConfirmTask.value = null
    await load(true)
  } catch (e) { error.value = e instanceof Error ? e.message : '取消发布失败' }
  finally { busy.value = '' }
}
const removeTask = async (task: ChannelsTask) => { busy.value = task.id; try { await api.channelsDeleteTask(task.id); await load(true) } catch (e) { error.value = e instanceof Error ? e.message : '删除失败' } finally { busy.value = '' } }
const removeAccount = async (account: ChannelsAccount) => { busy.value = account.id; try { await api.channelsAccountDelete(account.id); await load(true) } catch (e) { error.value = e instanceof Error ? e.message : '解除授权失败' } finally { busy.value = '' } }
const syncMetrics = async () => {
  syncingMetrics.value = true; error.value = ''; syncMessage.value = ''
  try {
    const ids = tasks.value.filter(task => task.can_manage && ['success', 'submitted'].includes(task.status)).map(task => task.id)
    const result = await api.channelsSyncMetrics(ids)
    syncMessage.value = result.message
    window.setTimeout(() => void load(true), 2500)
  } catch (e) { error.value = e instanceof Error ? e.message : '视频号数据回传未完成' }
  finally { syncingMetrics.value = false }
}
watch([q, status, pushStartDate, pushEndDate], () => { page.value = 1; clearTimeout(searchTimer); searchTimer = setTimeout(() => load(true), 240) })
watch(recordScope, () => { page.value = 1; void load(true) })
watch(pageSize, () => { page.value = 1; void load(true) })
onMounted(() => { void load(); refreshTimer = setInterval(() => void load(true), 12000); window.addEventListener('keydown', onKeydown) })
onBeforeUnmount(() => { if (authTimer) clearInterval(authTimer); if (refreshTimer) clearInterval(refreshTimer); if (searchTimer) clearTimeout(searchTimer); if (accountScrollTimer) clearTimeout(accountScrollTimer); clearReconnectTimer(); window.removeEventListener('keydown', onKeydown) })
const dateTime = (value?: string | null) => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '—'
const stageLabel = (stage: string) => ({
  download_original: '读取原视频', open_publish_page: '打开发布页', upload_original: '上传原视频',
  fill_content: '填写内容', bind_product: '关联商品', set_video_annotation: '确认视频标注', set_cover: '确认视频封面', wait_platform: '等待平台处理',
  submit_publish: '提交发布', confirm_publish: '等待公开确认', platform_review: '平台审核结果', authorization: '等待重新授权',
} as Record<string, string>)[stage] || ''
const taskStatusLabel = (task: ChannelsTask) => ({
  success: '已发布', submitted: '待平台确认', failed: '发布失败', publishing: '发布中',
  cancel_requested: '取消中', cancelled: '已取消', pending: '等待中',
} as Record<string, string>)[task.status] || task.status
const channelsCancelHint = (task: ChannelsTask) => task.status === 'pending'
  ? '任务尚未开始，将立即取消，不会上传或发布。'
  : '当前文件或页面操作结束后，会在点击视频号平台“发布”前停止；如果平台已经受理，系统会保留真实状态，不会显示虚假取消。'
</script>

<template>
  <section class="qc-center channels-center">
    <div class="qc-center-head"><div><span>WECHAT CHANNELS CREATOR</span><h2>视频号主页推送</h2><p>每位同事单独扫码授权自己的视频号；原视频直接发布，不压缩、不转码。</p></div><button class="primary-button" :disabled="busy === 'auth' || !statusInfo?.configured" @click="startAuth"><QrCode :size="16" />{{ busy === 'auth' ? '等待扫码…' : '扫码授权视频号' }}</button></div>
    <div class="qc-status-grid">
      <article :class="{ ready: statusInfo?.configured }"><ShieldCheck /><div><small>服务器能力</small><strong>{{ statusInfo?.message || '读取中' }}</strong><span class="qc-status-note">使用独立浏览器会话</span></div></article>
      <article :class="{ ready: accounts.length }"><QrCode /><div><small>我的视频号</small><strong>{{ accounts.length }} 个</strong><span class="qc-status-note">其他同事不可见</span></div></article>
      <article><CheckCircle2 /><div><small>{{ allRecords ? '全员发布记录' : '发布记录' }}</small><strong>{{ total }} 条</strong><span class="qc-status-note">{{ allRecords ? '管理员可查看全员结果并取消未完成任务' : '按个人账号隔离' }}</span></div></article>
      <article class="channels-metrics-card"><BarChart3 /><div><small>每日累计数据快照</small><strong>每天 {{ String(statusInfo?.metrics?.daily_hour ?? 7).padStart(2, '0') }}:00</strong><span class="qc-status-note">{{ statusInfo?.metrics?.last_completed_date ? `已回流至 ${statusInfo.metrics.last_completed_date}` : '首次回流后显示日期' }}</span></div></article>
    </div>
    <div v-if="authSession" class="channels-auth-card" :class="captureMode">
      <div v-if="authPhase !== 'failed'" class="channels-auth-steps" aria-label="视频号授权进度">
        <span class="done"><i>1</i>微信扫码</span><b></b>
        <span :class="{ active: captureMode !== 'qr', done: captureMode !== 'qr' }"><i>2</i>电脑选择视频号</span><b></b>
        <span><i>3</i>授权完成</span>
      </div>
      <div class="channels-auth-copy"><div><small>{{ authPhase === 'failed' ? '授权未完成' : authPhase === 'verifying' ? '第 3 步 · 正在校验登录状态' : captureMode !== 'qr' ? '第 2 步 · 请在电脑上完成' : '第 1 步 · 微信扫码确认' }}</small><strong>{{ authMessage }}</strong><span>{{ authPhase === 'failed' ? '当前页面未能确认授权成功，请查看账号列表或重新扫码；此操作不会发布视频。' : authPhase === 'verifying' ? '账号已选定，正在保存并校验登录状态，请勿重复点击。' : captureMode === 'account_list' ? '请选择本次要授权的视频号；账号卡片来自当前扫码会话，不会跨同事或跨会话复用。' : captureMode === 'account_choice' ? '未能读取账号卡片，请在下方安全截图中点击要登录的视频号。' : '请使用要发布内容的微信扫码，并在手机上确认。' }}</span></div><div v-if="qrReady && captureMode === 'qr'" class="channels-qr-actions"><button @click="qrExpanded = true"><Maximize2 :size="15" />全屏放大</button><button @click="refreshQr"><RefreshCw :size="15" />刷新二维码</button></div><small v-if="captureMode !== 'qr'" class="channels-choice-tip">这里只选择本次授权的视频号，不会替你发布任何内容。</small></div>
      <div v-if="authPhase === 'failed'" class="channels-auth-failure" role="alert"><button class="primary-button" :disabled="busy === 'auth'" @click="startAuth"><QrCode :size="16" />重新扫码授权</button></div>
      <div v-else-if="authPhase === 'verifying'" class="channels-auth-loading" role="status"><LoaderCircle class="spin" /><span>正在校验所选视频号，请稍候…</span></div>
      <button v-else-if="qrReady && captureMode === 'qr'" class="channels-qr-preview" aria-label="放大视频号登录二维码" @click="qrExpanded = true"><img :src="qrSrc" alt="视频号登录二维码" /><span><Maximize2 :size="14" />点击二维码全屏放大</span></button>
      <div v-else-if="captureMode === 'account_list'" class="channels-choice-preview" :class="{ busy: interacting }">
        <div class="channels-native-account-picker">
          <header><div><strong>选择要授权的视频号</strong><small>{{ accountChoices.length ? '账号已经读取，可直接滚动并选择' : '正在等待当前扫码会话的账号列表' }}</small></div><span>{{ accountChoices.length }} 个账号</span></header>
          <div v-if="accountChoices.length" class="channels-native-account-list">
            <button v-for="choice in accountChoices" :key="choice.choice_id" :disabled="interacting" @click="selectChannelsAccount(choice.choice_id)">
              <span>{{ choice.label.slice(0, 1) }}</span>
              <div><strong>{{ choice.label }}</strong><small>{{ choice.detail || '视频号账号' }}</small></div><b>{{ interacting && interactionAction === 'select' ? '登录中' : '选择' }}</b>
            </button>
          </div>
          <div v-else class="channels-account-list-loading"><LoaderCircle class="spin" /><span>正在读取账号列表…</span></div>
        </div>
        <span>{{ interacting ? '已锁定本次选择，正在登录该视频号…' : '账号选择仅在本次扫码会话内有效' }}</span>
        <small v-if="choiceMessage" class="channels-choice-message">{{ choiceMessage }}</small>
      </div>
      <div v-else-if="qrReady && captureMode === 'account_choice'" class="channels-choice-preview" :class="{ busy: interacting }">
        <div class="channels-choice-fallback-notice"><strong>安全截图兜底</strong><small>请点击截图中的账号；若列表较长，可在截图内滚动。</small></div>
        <div class="channels-choice-stage">
          <img :src="qrSrc" alt="选择要登录的视频号" @click="chooseChannelsAccount" @wheel.prevent.stop="scrollChannelsAccountList" />
        </div>
        <span>{{ interacting ? (interactionAction === 'scroll' ? '正在滚动并刷新账号页面…' : '已识别选择，正在登录该视频号…') : '截图只用于本次账号选择，不会保存到业务记录' }}</span>
        <small v-if="choiceMessage" class="channels-choice-message">{{ choiceMessage }}</small>
      </div>
      <div v-else class="channels-auth-loading"><LoaderCircle class="spin" /><span>正在安全打开视频号授权页面…</span></div>
    </div>
    <div v-if="accounts.length" class="channels-account-list">
      <article v-for="account in accounts" :key="account.id">
        <img v-if="account.avatar_url" :src="account.avatar_url" alt="" /><span v-else>{{ account.nickname.slice(0, 1) }}</span>
        <div><strong>{{ account.nickname }}</strong><small>发布授权于 {{ dateTime(account.authorized_at) }}</small><small class="channels-promotion-auth-chip" :class="{ ready: account.promotion.authorized }">{{ account.promotion.authorized ? `${account.promotion.account_type || '企业账户'} · ${account.promotion.nickname || '未命名'} · ${account.promotion.balance == null ? '余额待读取' : `${account.promotion.balance} 微信豆`}` : account.promotion.status === 'enterprise_required' ? '当前不是企业账户，需重新授权' : '企业加热未授权（使用时扫码）' }}</small></div>
        <div class="channels-account-actions"><button class="channels-heat-auth-button" :class="{ ready: account.promotion.authorized }" @click="promotionAuthAccount = account"><Flame :size="14" />{{ account.promotion.authorized ? '管理加热' : account.promotion.status === 'enterprise_required' ? '重新授权企业账户' : '授权企业账户加热' }}</button><button class="channels-unbind-button" :disabled="busy === account.id" @click="removeAccount(account)"><Trash2 :size="14" />解除发布授权</button></div>
      </article>
    </div>
    <div v-if="reconnecting" class="channels-reconnect-notice" role="status"><LoaderCircle class="spin" :size="18" /><div><strong>服务升级中，正在自动恢复</strong><span>已保留当前账号和发布记录，无需手动重试。</span></div></div>
    <div v-if="error" class="notice compact"><strong>视频号操作未完成</strong><span>{{ error }}</span><button @click="load()">重试</button></div>
    <div v-if="syncMessage" class="channels-sync-message"><CheckCircle2 :size="16" />{{ syncMessage }}</div>
    <div class="qc-records-card">
      <div class="qc-records-toolbar"><div><strong>{{ allRecords ? '全员发布记录' : '我的发布记录' }}</strong><small>{{ allRecords ? '管理员可查看全部结果，并取消尚未到达平台的任务' : '默认展示自己的记录，已发布素材可直接加热' }}</small></div><div class="qc-records-controls"><select v-if="canViewAll" v-model="recordScope" aria-label="发布记录范围"><option value="mine">我的记录</option><option value="all">全员记录</option></select><label class="qc-search"><Search :size="15" /><input v-model="q" :placeholder="allRecords ? '搜索发起人、素材、视频号或错误' : '搜索素材、视频号或错误'" /></label><select v-model="status"><option value="all">全部状态</option><option value="pending">等待中</option><option value="publishing">发布中</option><option value="cancel_requested">取消中</option><option value="cancelled">已取消</option><option value="submitted">已受理 / 待确认</option><option value="success">已发布</option><option value="failed">失败</option></select><select v-model="pageSize"><option :value="5">每页 5 条</option><option :value="10">每页 10 条</option></select><button class="qc-collapse" :disabled="syncingMetrics" @click="syncMetrics"><RefreshCw :size="14" :class="{ spin: syncingMetrics }" />{{ syncingMetrics ? '回传中' : '刷新并回传' }}</button><DateRangeFilter v-model:start-date="pushStartDate" v-model:end-date="pushEndDate" label="推送日期" /></div></div>
      <div class="channels-task-list">
        <nav v-if="total" class="qc-pagination qc-pagination-top" aria-label="视频号发布记录顶部分页"><span>共 {{ total }} 条</span><PaginationControls :page="page" :total-pages="pages" @change="changePage" /></nav>
        <div class="channels-task-row channels-task-head"><span>素材 / 发起人</span><span>视频号 / 标题</span><span>状态</span><span>每日累计回流</span><span>操作</span></div>
        <div v-if="loading" class="access-empty">正在读取发布记录…</div>
        <div v-for="task in tasks" v-else :key="task.id" class="channels-task-row">
          <span><strong>{{ task.asset_name }}</strong><small>{{ task.created_by_name }} · {{ dateTime(task.created_at) }}</small></span>
          <span><strong>{{ task.account_name }}</strong><small>{{ task.title }}</small><small class="channels-task-annotation">标注：{{ task.video_annotation_label || '无需标注' }}</small><small v-if="task.product_id" class="channels-task-product">商品：{{ task.product_name || task.product_id }}</small></span>
          <span><b class="qc-task-status" :class="task.status">{{ taskStatusLabel(task) }}</b><small v-if="task.cover_status && task.cover_status !== 'not_requested'" class="channels-cover-state">封面：{{ task.cover_status === 'verified' ? '已核对一致' : task.cover_status === 'mismatch' ? '未生效' : '待核验' }} · {{ task.cover_filename }}</small><small v-else-if="task.failure_stage === 'set_cover'" class="channels-cover-state">封面：待核验</small><small v-if="task.failure_stage && task.failure_stage !== 'set_cover'" class="channels-task-stage">阶段：{{ stageLabel(task.failure_stage) }}</small><small>{{ task.error_message || task.message }}</small><small v-if="task.latest_promotion" class="channels-promotion-state" :class="task.latest_promotion.status">最近加热：{{ task.latest_promotion.status === 'success' ? '已支付' : task.latest_promotion.status === 'pending_payment' ? '待支付' : task.latest_promotion.status === 'payment_processing' ? '支付确认中' : task.latest_promotion.status === 'manual_review' ? '需人工核对' : task.latest_promotion.status === 'failed' ? '失败' : '创建中' }}</small></span>
          <span class="channels-task-metrics"><strong>{{ task.view_count == null ? '待回流' : `${task.view_count.toLocaleString()} 播放` }}</strong><small>赞 {{ task.like_count ?? '—' }} · 评 {{ task.comment_count ?? '—' }} · 分享 {{ task.share_count ?? '—' }}</small><small v-if="task.order_count != null">订单 {{ task.order_count }}</small><small v-if="task.gmv_yuan != null">成交 ¥{{ task.gmv_yuan.toLocaleString() }}</small><small>{{ task.metrics_date || task.metrics_message || '每天回流一次' }}</small></span>
          <span class="channels-row-actions"><button v-if="task.can_edit" :disabled="busy === task.id" @click="editTask = task">修改封面/标题/商品</button><button v-if="task.can_promote" class="channels-promote-button" @click="openPromotion(task)"><Flame :size="14" />加热</button><button v-if="task.can_cancel && task.status !== 'cancel_requested'" class="channels-cancel-button" :disabled="busy === task.id" @click="cancelConfirmTask = task"><X :size="14" />取消推送</button><small v-else-if="task.status === 'cancel_requested'">等待安全停止</small><button v-if="task.can_manage && task.status === 'failed'" :disabled="busy === task.id" @click="retry(task)"><RefreshCw :size="14" />重试</button><button v-if="task.can_manage && ['success','failed','cancelled'].includes(task.status)" :disabled="busy === task.id" @click="removeTask(task)"><Trash2 :size="14" />删除</button><small v-if="task.can_manage && task.status === 'success' && !task.can_promote">等待核验加热标识</small><small v-if="!task.can_manage && !task.can_cancel">只读</small></span>
        </div>
        <div v-if="!loading && !tasks.length" class="access-empty">暂无视频号发布记录</div>
      </div>
      <footer v-if="total" class="qc-pagination"><span>共 {{ total }} 条</span><PaginationControls :page="page" :total-pages="pages" @change="changePage" /></footer>
    </div>
    <ChannelsPromotionModal v-if="promotionTask && promotionAccount" :task="promotionTask" :account="promotionAccount" @close="promotionTask = null" @changed="load(true)" />
    <ChannelsEditModal v-if="editTask" :task="editTask" @close="editTask = null" @saved="editTask = null; load(true)" />
    <ChannelsPromotionModal v-if="promotionAuthAccount" :account="promotionAuthAccount" auth-only @close="promotionAuthAccount = null" @changed="load(true)" />
    <Teleport to="body"><div v-if="cancelConfirmTask" class="channels-cancel-layer" @click.self="cancelConfirmTask = null"><section role="dialog" aria-modal="true" aria-label="确认取消视频号推送"><header><div><strong>确认取消视频号推送？</strong><span>{{ cancelConfirmTask.asset_name }}</span></div><button aria-label="关闭" @click="cancelConfirmTask = null"><X :size="20" /></button></header><p>{{ channelsCancelHint(cancelConfirmTask) }}</p><footer><button @click="cancelConfirmTask = null">暂不取消</button><button class="danger" :disabled="busy === cancelConfirmTask.id" @click="cancelTask(cancelConfirmTask)"><LoaderCircle v-if="busy === cancelConfirmTask.id" class="spin" :size="14" /><X v-else :size="14" />确认取消推送</button></footer></section></div></Teleport>
    <Teleport to="body"><div v-if="qrExpanded && qrReady && captureMode === 'qr'" class="channels-qr-lightbox" @click.self="qrExpanded = false"><section role="dialog" aria-modal="true" aria-label="放大视频号登录二维码"><header><div><strong>微信扫码授权视频号</strong><span>请使用要发布内容的微信扫描，并在手机上确认</span></div><button aria-label="关闭二维码" @click="qrExpanded = false"><X :size="22" /></button></header><img :src="qrSrc" alt="放大的视频号登录二维码" /><footer><button @click="refreshQr"><RefreshCw :size="16" />刷新二维码</button><span>二维码过期时请刷新，或重新发起授权</span></footer></section></div></Teleport>
  </section>
</template>

<style scoped>
.channels-reconnect-notice {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 16px 0;
  padding: 14px 16px;
  border: 1px solid #b8d9ee;
  border-radius: 14px;
  background: #eef8fe;
  color: #14557b;
}
.channels-reconnect-notice > div { display: grid; gap: 3px; }
.channels-reconnect-notice strong { font-size: 14px; }
.channels-reconnect-notice span { color: #53788e; font-size: 12px; }
</style>
