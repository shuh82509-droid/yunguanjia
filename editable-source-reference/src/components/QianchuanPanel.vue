<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Link2,
  LoaderCircle,
  RefreshCw,
  Search,
  Send,
  ShieldCheck,
  Trash2,
  X,
} from 'lucide-vue-next'
import { api } from '../api'
import { openAuthorizationWindow } from '../authorization-window'
import type { QianchuanAccount, QianchuanPlan, QianchuanPlanMaterialResult, QianchuanStatus, QianchuanTask } from '../types'
import PaginationControls from './PaginationControls.vue'
import DateRangeFilter from './DateRangeFilter.vue'

const status = ref<QianchuanStatus | null>(null)
const tasks = ref<QianchuanTask[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref<5 | 10 | 50 | 100>(10)
const totalPages = ref(1)
const allRecords = ref(false)
const loading = ref(true)
const syncing = ref(false)
const authorizing = ref(false)
const retryingTask = ref('')
const retryingBatch = ref('')
const deletingTask = ref('')
const deleteConfirmTask = ref('')
const cancellingTask = ref('')
const cancelConfirmTask = ref('')
const expandedTaskIds = ref<string[]>([])
const recordsExpanded = ref(true)
const searchText = ref('')
const statusFilter = ref('all')
const pushStartDate = ref('')
const pushEndDate = ref('')
const error = ref('')
const syncNotice = ref('')
const recordsCard = ref<HTMLElement | null>(null)
const inventoryOpen = ref(false)
const inventoryAccounts = ref<QianchuanAccount[]>([])
const inventoryPlans = ref<QianchuanPlan[]>([])
const inventoryAdvertiserId = ref('')
const inventoryPlanId = ref('')
const inventoryLoading = ref(false)
const inventoryPlansLoading = ref(false)
const inventoryError = ref('')
const inventoryResult = ref<QianchuanPlanMaterialResult | null>(null)
const isoDateOffset = (days: number) => {
  const value = new Date()
  value.setDate(value.getDate() + days)
  return value.toISOString().slice(0, 10)
}
const inventoryStartDate = ref(isoDateOffset(-7))
const inventoryEndDate = ref(isoDateOffset(-1))
let timer: ReturnType<typeof setInterval> | undefined
let searchTimer: ReturnType<typeof setTimeout> | undefined

const changePage = (nextPage: number) => { page.value = nextPage }

const load = async (quiet = false) => {
  if (!quiet) loading.value = true
  error.value = ''
  try {
    const [statusData, taskData] = await Promise.all([
      api.qianchuanStatus(),
      api.qianchuanTasks(undefined, searchText.value, statusFilter.value, page.value, pageSize.value, pushStartDate.value, pushEndDate.value),
    ])
    status.value = statusData
    tasks.value = taskData.items
    total.value = taskData.total
    totalPages.value = taskData.total_pages
    allRecords.value = taskData.viewer_scope === 'all'
    if (page.value > taskData.total_pages) page.value = taskData.total_pages
  } catch (e) { error.value = e instanceof Error ? e.message : '无法读取千川推送中心' }
  finally { loading.value = false }
}

const authorize = async () => {
  if (authorizing.value) return
  authorizing.value = true
  error.value = ''
  try {
    await openAuthorizationWindow(() => window.open('about:blank', '_blank'), api.qianchuanAuthorize)
    syncNotice.value = '请在独立窗口完成千川授权，然后返回这里刷新状态。'
  }
  catch (e) { error.value = e instanceof Error ? e.message : '无法发起授权' }
  finally { authorizing.value = false }
}

const loadInventoryAccounts = async () => {
  if (inventoryAccounts.value.length) return
  inventoryLoading.value = true
  inventoryError.value = ''
  try {
    const result = await api.qianchuanAccounts()
    inventoryAccounts.value = result.items
  } catch (e) {
    inventoryError.value = e instanceof Error ? e.message : '无法读取千川账户'
  } finally {
    inventoryLoading.value = false
  }
}

const toggleInventory = async () => {
  inventoryOpen.value = !inventoryOpen.value
  if (inventoryOpen.value) await loadInventoryAccounts()
}

const loadInventoryPlans = async () => {
  inventoryPlanId.value = ''
  inventoryPlans.value = []
  inventoryResult.value = null
  if (!inventoryAdvertiserId.value) return
  inventoryPlansLoading.value = true
  inventoryError.value = ''
  try {
    const result = await api.qianchuanPlans(inventoryAdvertiserId.value, '', 'all')
    inventoryPlans.value = result.items.filter(item => item.can_attach_video)
  } catch (e) {
    inventoryError.value = e instanceof Error ? e.message : '无法读取计划列表'
  } finally {
    inventoryPlansLoading.value = false
  }
}

const loadPlanInventory = async () => {
  if (!inventoryAdvertiserId.value || !inventoryPlanId.value) return
  inventoryLoading.value = true
  inventoryError.value = ''
  try {
    inventoryResult.value = await api.qianchuanPlanMaterials(
      inventoryAdvertiserId.value,
      inventoryPlanId.value,
      inventoryStartDate.value,
      inventoryEndDate.value,
    )
  } catch (e) {
    inventoryError.value = e instanceof Error ? e.message : '无法回读计划素材'
  } finally {
    inventoryLoading.value = false
  }
}

const syncMetrics = async () => {
  syncing.value = true
  error.value = ''
  syncNotice.value = ''
  try {
    const result = await api.qianchuanSyncMetrics()
    syncNotice.value = result.message
    await load(true)
    setTimeout(() => void load(true), 2200)
  } catch (e) { error.value = e instanceof Error ? e.message : '无法同步投放数据' }
  finally { syncing.value = false }
}

const retryTask = async (task: QianchuanTask) => {
  retryingTask.value = task.id
  error.value = ''
  try {
    await api.qianchuanRetry(task.id)
    deleteConfirmTask.value = ''
    await load(true)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法重试推送任务'
  } finally {
    retryingTask.value = ''
  }
}

const retryBatch = async (task: QianchuanTask) => {
  retryingBatch.value = task.batch_id
  error.value = ''
  syncNotice.value = ''
  try {
    const result = await api.qianchuanRetryBatch(task.batch_id)
    deleteConfirmTask.value = ''
    syncNotice.value = result.message
    await load(true)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法重试本批失败目标'
  } finally {
    retryingBatch.value = ''
  }
}

const deleteTask = async (task: QianchuanTask) => {
  deletingTask.value = task.id
  error.value = ''
  try {
    await api.qianchuanDeleteTask(task.id)
    deleteConfirmTask.value = ''
    expandedTaskIds.value = expandedTaskIds.value.filter(id => id !== task.id)
    await load(true)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法删除推送记录'
  } finally {
    deletingTask.value = ''
  }
}

const cancelTask = async (task: QianchuanTask) => {
  cancellingTask.value = task.id
  error.value = ''
  try {
    const result = await api.qianchuanCancel(task.id)
    syncNotice.value = result.message
    cancelConfirmTask.value = ''
    await load(true)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法取消千川推送任务'
  } finally {
    cancellingTask.value = ''
  }
}

const toggleTask = (id: string) => {
  expandedTaskIds.value = expandedTaskIds.value.includes(id)
    ? expandedTaskIds.value.filter(item => item !== id)
    : [...expandedTaskIds.value, id]
}

const requestDelete = (task: QianchuanTask) => {
  deleteConfirmTask.value = task.id
  if (!expandedTaskIds.value.includes(task.id)) {
    expandedTaskIds.value = [...expandedTaskIds.value, task.id]
  }
}

const requestCancel = (task: QianchuanTask) => {
  cancelConfirmTask.value = task.id
  deleteConfirmTask.value = ''
  if (!expandedTaskIds.value.includes(task.id)) {
    expandedTaskIds.value = [...expandedTaskIds.value, task.id]
  }
}

const statusName = (value: string) => ({
  pending: '等待中', uploading: '上传中', uploaded: '已入素材库', binding: '计划投放中',
  cancel_requested: '取消中', cancelled: '已取消', success: '计划已关联', partial: '部分完成', failed: '失败', maintenance_hold: '维护待核验',
}[value] || value)
const stageName = (value: string) => ({ upload: '原视频上传', plan_binding: '计划绑定', metrics_sync: '数据回流' }[value] || value || '未标记')
const metricsLinked = (task: QianchuanTask) => task.metrics_link_status === 'verified' || task.metrics_link_status === 'account_material'
const hasDisplayMetrics = (task: QianchuanTask) => metricsLinked(task) && Boolean(Object.keys(task.metrics || {}).length) && !['pending', 'error', 'no_data'].includes(task.metrics_data_status)
const money = (value: number | string | undefined) => value === undefined ? '—' : `¥${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
const number = (value: number | string | undefined) => value === undefined ? '—' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN') : '—'
const canRetry = (task: QianchuanTask) => task.can_manage && (task.status === 'failed' || (task.status === 'partial' && task.plan_type !== 'standard'))
const canDelete = (task: QianchuanTask) => task.can_manage && !['pending', 'uploading', 'binding', 'cancel_requested', 'maintenance_hold'].includes(task.status)
const cancelHint = (task: QianchuanTask) => task.status === 'pending'
  ? '任务尚未开始，将立即取消且不会向千川发送视频。'
  : task.status === 'binding'
    ? '当前平台绑定请求结束后停止；若千川已经受理，系统会保留真实结果，不会显示虚假取消。'
    : task.platform_asset_id || task.upload_task_id
      ? '将停止后续步骤；已经进入千川素材库的原视频会保留，不会删除平台素材。'
      : '当前文件或平台操作结束后停止，不会再进入后续计划绑定。'
const metricsState = (task: QianchuanTask) => ({
  fresh: `已更新至 ${task.metrics_fresh_through || '昨日'}`,
  no_data: `已刷新至 ${task.metrics_fresh_through || '昨日'}，暂无数据`,
  partial: `日数据补齐中 ${task.metrics_coverage?.completed || 0}/${task.metrics_coverage?.expected || 0}`,
  partial_error: `部分日期失败 ${task.metrics_coverage?.completed || 0}/${task.metrics_coverage?.expected || 0}`,
  pending: '待首次按日刷新',
  error: '每日数据回流失败',
}[task.metrics_data_status] || ({
  verified: '历史汇总数据', linked_pending: '已关联，等待数据', pending: '等待回流',
  missing: '计划内未发现视频', mismatch: '计划不匹配', unverified: '关联未核验',
  account_material: '账户素材级',
}[task.metrics_link_status] || '等待回流'))
const dailyMetricState = (value: string) => ({ success: '已回流', no_data: '暂无数据', error: '失败', missing: '未发现关联' }[value] || value)
const filteredLabel = computed(() => searchText.value.trim() || statusFilter.value !== 'all' ? `找到 ${total.value} 条` : `共 ${total.value} 条`)
const pageRangeLabel = computed(() => {
  if (!total.value) return '0 条'
  const start = (page.value - 1) * pageSize.value + 1
  const end = Math.min(total.value, page.value * pageSize.value)
  return `第 ${start}–${end} 条，共 ${total.value} 条`
})
const metricsSync = computed(() => status.value?.metrics_sync)
const manualSyncDisabled = computed(() => Boolean(
  syncing.value || !status.value?.authorized || metricsSync.value?.manual_running || metricsSync.value?.manual_available === false,
))

watch([searchText, statusFilter, pushStartDate, pushEndDate], () => {
  if (searchTimer) clearTimeout(searchTimer)
  if (page.value !== 1) {
    page.value = 1
    return
  }
  searchTimer = setTimeout(() => void load(true), 280)
})
watch(pageSize, () => {
  if (page.value !== 1) {
    page.value = 1
    return
  }
  void load(true)
})
watch(page, async () => {
  await load(true)
  await nextTick()
  recordsCard.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
})

onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (tasks.value.some(item => ['pending', 'uploading', 'binding', 'cancel_requested'].includes(item.status)) || metricsSync.value?.manual_running) void load(true)
  }, 8000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<template>
  <section class="qc-center">
    <div class="qc-center-head">
      <div><span>OCEAN ENGINE OPEN API</span><h2>千川推送与数据回流</h2><p>推送结果与数据回流分开核验；只有计划回读发现相同视频 ID 才显示为关联成功。</p></div>
      <div class="qc-sync-control">
        <small>每日回流：每天 {{ String(metricsSync?.daily_hour ?? 6).padStart(2, '0') }}:00 · 覆盖全库 · 按计划去重</small>
        <span v-if="metricsSync?.manual_running">正在同步 {{ metricsSync.manual_task_count }} 条数据</span>
        <span v-else-if="metricsSync?.manual_next_available_at">下次可手动同步：{{ formatTime(metricsSync.manual_next_available_at) }}</span>
        <span v-else-if="metricsSync?.daily_state === 'running'">全库任务进行中：{{ metricsSync.daily_processed_groups || 0 }}/{{ metricsSync.daily_group_count || '—' }} 组</span>
        <span v-else-if="metricsSync?.daily_last_completed_date">全库数据已刷新至：{{ metricsSync.daily_last_completed_date }}<template v-if="metricsSync.daily_error_groups"> · {{ metricsSync.daily_error_groups }} 组需重试</template></span>
        <span v-else>首次将补齐近 {{ metricsSync?.lookback_days || 7 }} 日数据</span>
        <button class="secondary-button" :disabled="manualSyncDisabled" @click="syncMetrics"><RefreshCw :size="16" :class="{ spin: syncing || metricsSync?.manual_running }" />{{ metricsSync?.manual_running ? '数据同步中' : '刷新我的今日数据' }}</button>
      </div>
    </div>
    <div v-if="syncNotice" class="qc-sync-notice"><CheckCircle2 :size="15" />{{ syncNotice }}</div>

    <div v-if="loading" class="qc-loading"><LoaderCircle class="spin" />正在读取千川状态…</div>
    <template v-else>
      <div class="qc-status-grid">
        <article :class="{ ready: status?.authorized }"><ShieldCheck /><div><small>授权状态</small><strong>{{ status?.message }}</strong><button v-if="!status?.authorized && status?.can_authorize" class="qc-status-action" :disabled="authorizing" @click="authorize"><Link2 :size="14" />{{ authorizing ? '正在打开…' : '去授权' }}</button><span v-else-if="!status?.authorized" class="qc-status-note">请联系千川管理员</span></div></article>
        <article><Send /><div><small>{{ allRecords ? '全员推送记录' : '我的推送记录' }}</small><strong>{{ total }} 条</strong><span class="qc-status-note">{{ allRecords ? '管理员可查看全员回流并取消未完成任务' : '仅你本人可见和管理' }}</span></div></article>
        <article><BarChart3 /><div><small>数据口径</small><strong>计划素材级</strong><span class="qc-status-note">计划 ID + 视频 ID 精确关联</span></div></article>
      </div>

      <section class="qc-plan-inventory">
        <header>
          <div><strong>计划素材占用与数据回读</strong><small>直接读取所选计划当前返回的视频、状态和统计，方便计划满额后人工清理并安全补位。</small></div>
          <button class="secondary-button" @click="toggleInventory"><BarChart3 :size="15" />{{ inventoryOpen ? '收起计划素材' : '查看计划素材' }}<ChevronDown v-if="inventoryOpen" :size="14" /><ChevronRight v-else :size="14" /></button>
        </header>
        <template v-if="inventoryOpen">
          <div class="qc-plan-inventory-controls">
            <select v-model="inventoryAdvertiserId" aria-label="选择千川账户" @change="loadInventoryPlans">
              <option value="">选择千川账户</option>
              <option v-for="account in inventoryAccounts" :key="account.id" :value="account.id">{{ account.name }} · {{ account.id }}</option>
            </select>
            <select v-model="inventoryPlanId" :disabled="!inventoryAdvertiserId || inventoryPlansLoading" aria-label="选择投放计划">
              <option value="">{{ inventoryPlansLoading ? '正在读取计划…' : '选择投放计划' }}</option>
              <option v-for="plan in inventoryPlans" :key="plan.id" :value="plan.id">{{ plan.name }} · {{ plan.plan_type_label }}</option>
            </select>
            <label>开始<input v-model="inventoryStartDate" type="date" /></label>
            <label>结束<input v-model="inventoryEndDate" type="date" /></label>
            <button class="primary-button" :disabled="inventoryLoading || !inventoryPlanId" @click="loadPlanInventory"><RefreshCw :size="15" :class="{ spin: inventoryLoading }" />{{ inventoryLoading ? '正在回读…' : '回读计划素材' }}</button>
          </div>
          <div v-if="inventoryError" class="qc-plan-inventory-error"><AlertTriangle :size="16" />{{ inventoryError }}</div>
          <template v-if="inventoryResult">
            <div class="qc-plan-inventory-summary">
              <span><strong>{{ inventoryResult.total }}</strong> 条已回读</span>
              <span><strong>{{ inventoryResult.active_count }}</strong> 条有效</span>
              <span><strong>{{ inventoryResult.inactive_count }}</strong> 条停用/已删除</span>
              <small>统计区间 {{ inventoryResult.start_date }} 至 {{ inventoryResult.end_date }} · {{ formatTime(inventoryResult.read_at) }}</small>
            </div>
            <div class="qc-plan-inventory-table">
              <div class="qc-plan-inventory-row header"><span>计划素材</span><span>状态</span><span>消耗</span><span>成交金额</span><span>ROI</span><span>WIS 关联</span></div>
              <div v-for="item in inventoryResult.items" :key="`${item.video_id}-${item.material_id}`" class="qc-plan-inventory-row">
                <span><strong :title="item.title">{{ item.title || `视频 ${item.video_id}` }}</strong><small>视频 ID：{{ item.video_id }}</small></span>
                <span><b :class="item.active ? 'active' : 'inactive'">{{ item.active ? '计划内有效' : '停用/已删除' }}</b><small>{{ item.material_status || item.audit_status || '状态未返回' }}</small></span>
                <span>{{ item.has_data ? money(item.metrics.stat_cost) : '—' }}</span>
                <span>{{ item.has_data ? money(item.metrics.pay_order_amount) : '—' }}</span>
                <span>{{ item.has_data ? number(item.metrics.prepay_and_pay_order_roi) : '—' }}</span>
                <span><strong>{{ item.linked_asset?.asset_name || '未匹配素材库记录' }}</strong><small v-if="item.linked_asset">素材 ID：{{ item.linked_asset.asset_id }}</small></span>
              </div>
              <div v-if="!inventoryResult.items.length" class="qc-plan-inventory-empty">该计划在所选区间内未返回视频素材。</div>
            </div>
            <div class="qc-plan-inventory-tip"><ShieldCheck :size="16" /><span>这里只展示计划容量状态，不提供清理或删除素材操作；已满计划会醒目标红，请改选其他可用计划。</span></div>
          </template>
        </template>
      </section>

      <div v-if="error" class="notice compact"><strong>千川操作失败</strong><span>{{ error }}</span><button @click="load()">重试</button></div>

      <section v-else ref="recordsCard" class="qc-records-card">
        <header class="qc-records-toolbar">
          <div>
            <strong>推送记录</strong>
            <small>{{ filteredLabel }}，{{ allRecords ? '全员回流只读总览；你的记录仍可重试或删除' : '删除只移除你的列表显示，不会删除千川素材或计划' }}</small>
          </div>
          <div class="qc-records-controls">
            <label class="qc-search"><Search :size="15" /><input v-model="searchText" type="search" :placeholder="allRecords ? '搜索发起人、素材、账户、计划、视频 ID 或错误' : '搜索素材、账户、计划、视频 ID 或错误'" /></label>
            <select v-model="statusFilter" aria-label="按推送状态筛选">
              <option value="all">全部状态</option><option value="success">计划已关联</option><option value="partial">部分完成</option>
              <option value="failed">失败</option><option value="pending">等待中</option><option value="uploading">上传中</option><option value="binding">计划投放中</option><option value="cancel_requested">取消中</option><option value="cancelled">已取消</option><option value="uploaded">已入素材库</option><option value="maintenance_hold">维护待核验</option>
            </select>
            <select v-model.number="pageSize" aria-label="每页显示条数">
              <option :value="5">每页 5 条</option><option :value="10">每页 10 条</option><option :value="50">每页 50 条</option><option :value="100">每页 100 条</option>
            </select>
            <DateRangeFilter v-model:start-date="pushStartDate" v-model:end-date="pushEndDate" label="推送日期" />
            <button class="qc-collapse" :aria-expanded="recordsExpanded" @click="recordsExpanded = !recordsExpanded">
              <ChevronDown v-if="recordsExpanded" :size="15" /><ChevronRight v-else :size="15" />{{ recordsExpanded ? '收起记录' : '展开记录' }}
            </button>
          </div>
        </header>

        <div v-if="recordsExpanded" class="qc-task-list">
          <nav v-if="total" class="qc-pagination qc-pagination-top" aria-label="推送记录顶部分页">
            <span>{{ pageRangeLabel }}</span>
            <PaginationControls :page="page" :total-pages="totalPages" @change="changePage" />
          </nav>
          <div class="qc-task-row qc-task-header"><span>素材 / 发起人</span><span>账户 / 计划</span><span>状态</span><span>消耗</span><span>成交金额</span><span>ROI</span><span>操作</span></div>
          <div v-for="task in tasks" :key="task.id" class="qc-task-item">
            <article class="qc-task-row" :class="{ 'has-error': task.error_message }">
              <span><strong :title="task.asset_name">{{ task.asset_name }}</strong><small>{{ task.created_by_name }} · {{ formatTime(task.created_at) }}</small></span>
              <span><strong :title="task.advertiser_name || task.advertiser_id">{{ task.advertiser_name || task.advertiser_id }}</strong><small :title="task.plan_name">{{ task.plan_name || '账户素材库' }}</small></span>
              <span><b class="qc-task-status" :class="task.status">{{ statusName(task.status) }}</b><small :title="task.error_message || task.message">{{ task.error_message || task.message }}</small><small v-if="task.attempt_count">已尝试 {{ task.attempt_count }} 次</small></span>
              <span><strong>{{ hasDisplayMetrics(task) ? money(task.metrics.stat_cost) : '—' }}</strong><small>{{ metricsState(task) }}</small></span>
              <span><strong>{{ hasDisplayMetrics(task) ? money(task.metrics.pay_order_amount) : '—' }}</strong><small>{{ hasDisplayMetrics(task) ? number(task.metrics.pay_order_count) : '—' }} 单</small></span>
              <span><strong>{{ hasDisplayMetrics(task) ? number(task.metrics.prepay_and_pay_order_roi ?? task.metrics.create_order_roi) : '—' }}</strong><small>{{ hasDisplayMetrics(task) ? number(task.metrics.show_cnt) : '—' }} 展现</small></span>
              <span class="qc-row-actions" :class="{ 'has-retry': canRetry(task) }">
                <button v-if="canRetry(task)" class="qc-retry qc-retry-batch" :disabled="Boolean(retryingBatch) || retryingTask === task.id" @click="retryBatch(task)"><RefreshCw :size="13" :class="{ spin: retryingBatch === task.batch_id }" />{{ retryingBatch === task.batch_id ? '本批重试中' : '重试本批失败项' }}</button>
                <span class="qc-row-actions-inline">
                  <button v-if="canRetry(task)" class="qc-retry-one" :disabled="Boolean(retryingBatch) || retryingTask === task.id" title="只重试这一条推送" @click="retryTask(task)"><RefreshCw :size="13" :class="{ spin: retryingTask === task.id }" />{{ retryingTask === task.id ? '重试中' : '重试' }}</button>
                  <button v-if="task.can_cancel && task.status !== 'cancel_requested'" class="qc-cancel-one" :disabled="cancellingTask === task.id" title="停止这条推送" @click="requestCancel(task)"><X :size="13" />取消推送</button>
                  <small v-else-if="task.status === 'cancel_requested'">等待安全停止</small>
                  <button @click="toggleTask(task.id)"><ChevronDown v-if="expandedTaskIds.includes(task.id)" :size="13" /><ChevronRight v-else :size="13" />详情</button>
                  <button v-if="canDelete(task)" title="删除我的记录" @click="requestDelete(task)"><Trash2 :size="13" /></button>
                </span>
              </span>
            </article>

            <section v-if="expandedTaskIds.includes(task.id)" class="qc-task-detail">
              <div class="qc-detail-grid">
                <div><small>推送结果</small><strong>{{ task.message || '暂无结果说明' }}</strong><span>状态：{{ statusName(task.status) }} · 尝试 {{ task.attempt_count }} 次</span></div>
                <div><small>数据回流</small><strong>{{ metricsState(task) }}</strong><span>{{ task.metrics_message || '尚未同步数据' }}</span><span>近7日覆盖：{{ task.metrics_coverage?.completed || 0 }}/{{ task.metrics_coverage?.expected || 0 }} 天</span></div>
                <div><small>精确关联</small><strong>计划 ID：{{ task.delivery_entity_id || task.plan_id || '—' }}</strong><span>视频 ID：{{ task.platform_asset_id || '—' }}</span><span>核验时间：{{ formatTime(task.binding_verified_at) }}</span></div>
              </div>

              <div v-if="task.daily_metrics?.length" class="qc-daily-metrics">
                <header><strong>每日投放数据</strong><span>真实 0、未返回和失败分别展示</span></header>
                <div class="qc-daily-row qc-daily-header"><span>日期</span><span>状态</span><span>消耗</span><span>成交金额</span><span>订单</span><span>ROI</span><span>展现</span></div>
                <div v-for="day in task.daily_metrics" :key="`${task.id}-${day.stat_date}`" class="qc-daily-row">
                  <span>{{ day.stat_date }}</span><span :class="`is-${day.status}`">{{ dailyMetricState(day.status) }}</span>
                  <span>{{ day.has_data ? money(day.metrics.stat_cost) : '—' }}</span><span>{{ day.has_data ? money(day.metrics.pay_order_amount) : '—' }}</span>
                  <span>{{ day.has_data ? number(day.metrics.pay_order_count) : '—' }}</span><span>{{ day.has_data ? number(day.metrics.prepay_and_pay_order_roi) : '—' }}</span>
                  <span>{{ day.has_data ? number(day.metrics.show_cnt) : '—' }}</span>
                </div>
              </div>

              <div v-if="task.error_message" class="qc-error-detail">
                <AlertTriangle :size="18" />
                <div><small>失败阶段：{{ stageName(task.failure_stage) }}</small><strong>{{ task.error_message }}</strong><p>{{ task.error_advice || '请核对完整错误后重试。' }}</p><span v-if="task.error_code">错误码：{{ task.error_code }}</span><span v-if="task.request_id">请求 ID：{{ task.request_id }}</span></div>
              </div>
              <div v-else-if="task.status === 'success'" class="qc-success-detail"><CheckCircle2 :size="17" /><span>计划绑定有回读证据；数据为 0 会显示 ¥0，未返回数据会明确显示“等待数据”。</span></div>

              <div v-if="cancelConfirmTask === task.id" class="qc-delete-confirm qc-cancel-confirm">
                <div><strong>确定取消这条千川推送？</strong><span>{{ cancelHint(task) }}</span></div>
                <button @click="cancelConfirmTask = ''"><X :size="13" />暂不取消</button>
                <button class="danger" :disabled="cancellingTask === task.id" @click="cancelTask(task)"><LoaderCircle v-if="cancellingTask === task.id" class="spin" :size="13" /><X v-else :size="13" />确认取消推送</button>
              </div>

              <div v-if="deleteConfirmTask === task.id" class="qc-delete-confirm">
                <div><strong>确定删除这条显示记录？</strong><span>只从你的列表中隐藏；千川计划、素材和后台审计信息都会保留。</span></div>
                <button @click="deleteConfirmTask = ''"><X :size="13" />取消</button>
                <button class="danger" :disabled="deletingTask === task.id" @click="deleteTask(task)"><LoaderCircle v-if="deletingTask === task.id" class="spin" :size="13" /><Trash2 v-else :size="13" />确认删除</button>
              </div>

              <footer v-if="canDelete(task) && deleteConfirmTask !== task.id" class="qc-detail-actions">
                <button class="qc-delete-link" @click="deleteConfirmTask = task.id"><Trash2 :size="13" />删除我的记录</button>
              </footer>
            </section>
          </div>
          <div v-if="!tasks.length" class="qc-empty"><Send :size="34" /><h3>{{ searchText || statusFilter !== 'all' ? '没有符合条件的记录' : '还没有推送记录' }}</h3><p>{{ searchText || statusFilter !== 'all' ? '换个关键词或状态筛选试试。' : '打开素材大厅中的视频详情，点击“推送千川”即可选择账户和计划。' }}</p></div>
          <footer v-if="total" class="qc-pagination" aria-label="推送记录底部分页">
            <span>{{ pageRangeLabel }}</span>
            <PaginationControls :page="page" :total-pages="totalPages" @change="changePage" />
          </footer>
        </div>
      </section>
    </template>
  </section>
</template>
