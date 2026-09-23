<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { BarChart3, CheckCircle2, ChevronDown, ChevronRight, Library, LoaderCircle, RefreshCw, Search, Send, ShieldCheck, Trash2, X } from 'lucide-vue-next'
import { api } from '../api'
import type { AdqStatus, AdqTask } from '../types'
import PaginationControls from './PaginationControls.vue'
import DateRangeFilter from './DateRangeFilter.vue'

const status = ref<AdqStatus | null>(null)
const tasks = ref<AdqTask[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref<5 | 10>(10)
const totalPages = ref(1)
const allRecords = ref(false)
const loading = ref(true)
const retryingTask = ref('')
const retryingBatch = ref('')
const authorizing = ref(false)
const deletingTask = ref('')
const deleteConfirmTask = ref('')
const expandedTaskIds = ref<string[]>([])
const recordsExpanded = ref(true)
const searchText = ref('')
const statusFilter = ref('all')
const pushStartDate = ref('')
const pushEndDate = ref('')
const error = ref('')
const notice = ref('')
let timer: ReturnType<typeof setInterval> | undefined
let searchTimer: ReturnType<typeof setTimeout> | undefined

const load = async (quiet = false) => {
  if (!quiet) loading.value = true
  error.value = ''
  try {
    const [statusData, taskData] = await Promise.all([
      api.adqStatus(),
      api.adqTasks(undefined, searchText.value, statusFilter.value, page.value, pageSize.value, false, pushStartDate.value, pushEndDate.value),
    ])
    status.value = statusData
    tasks.value = taskData.items
    total.value = taskData.total
    totalPages.value = taskData.total_pages
    allRecords.value = taskData.viewer_scope === 'all'
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法读取腾讯 ADQ 素材库'
  } finally {
    loading.value = false
  }
}

const retryTask = async (task: AdqTask) => {
  retryingTask.value = task.id
  error.value = ''
  try { await api.adqRetry(task.id); await load(true) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '无法重试素材库上传任务' }
  finally { retryingTask.value = '' }
}

const retryBatch = async (task: AdqTask) => {
  retryingBatch.value = task.batch_id
  error.value = ''
  notice.value = ''
  try { const result = await api.adqRetryBatch(task.batch_id); notice.value = result.message; await load(true) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '无法重试本批失败素材' }
  finally { retryingBatch.value = '' }
}

const deleteTask = async (task: AdqTask) => {
  deletingTask.value = task.id
  try { await api.adqDeleteTask(task.id); deleteConfirmTask.value = ''; await load(true) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '无法隐藏这条上传记录' }
  finally { deletingTask.value = '' }
}

const authorizeUser = async () => {
  if (authorizing.value || !status.value?.can_authorize_user) return
  authorizing.value = true
  error.value = ''
  try {
    const result = await api.adqUserAuthorizationStart()
    window.location.assign(result.url)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法发起 ADQ 操作人实名认证'
    authorizing.value = false
  }
}

const toggleTask = (id: string) => {
  expandedTaskIds.value = expandedTaskIds.value.includes(id)
    ? expandedTaskIds.value.filter(item => item !== id)
    : [...expandedTaskIds.value, id]
}
const statusName = (value: string) => ({ pending: '等待中', uploading: '原视频上传中', binding: '素材库核验中', success: '已核验入库', partial: '待核验', failed: '失败' }[value] || value)
const taskStatusName = (task: AdqTask) => task.status === 'success' && task.library_readback_status !== 'verified' ? '待核验' : statusName(task.status)
const money = (value: number | string | null | undefined) => value == null ? '—' : `¥${Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`
const number = (value: number | string | null | undefined) => value == null ? '—' : Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN') : '—'
const canRetry = (task: AdqTask) => task.can_manage && ['failed', 'partial'].includes(task.status)
const canDelete = (task: AdqTask) => task.can_manage && !['pending', 'uploading', 'binding'].includes(task.status)
const hasMetrics = (task: AdqTask) => task.metrics_status === 'success' && Boolean(Object.keys(task.metrics || {}).length)
const metricsState = (task: AdqTask) => ({
  success: task.metrics_end_date ? `累计至 ${task.metrics_end_date}` : '数据已回流',
  no_data: '近 30 日暂无素材数据',
  error: '数据回流失败，待重试',
  pending: '等待素材数据回流',
  not_applicable: '等待素材数据回流',
}[task.metrics_status] || '等待素材数据回流')
const rangeLabel = computed(() => total.value ? `第 ${(page.value - 1) * pageSize.value + 1}–${Math.min(total.value, page.value * pageSize.value)} 条，共 ${total.value} 条` : '0 条')

watch([searchText, statusFilter, pushStartDate, pushEndDate], () => {
  if (searchTimer) clearTimeout(searchTimer)
  if (page.value !== 1) page.value = 1
  searchTimer = setTimeout(() => void load(true), 280)
})
watch(page, () => void load(true))
watch(pageSize, () => { page.value = 1; void load(true) })
onMounted(() => {
  const params = new URLSearchParams(window.location.search)
  const authResult = params.get('adq_user')
  if (authResult) {
    notice.value = authResult === 'authorized' ? 'ADQ 操作人实名认证已完成，可以继续重试营销单元任务。' : 'ADQ 操作人实名认证未完成，请重新发起。'
    params.delete('adq_user')
    const query = params.toString()
    window.history.replaceState({}, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`)
  }
  void load()
  timer = setInterval(() => {
    if (tasks.value.some(item => ['pending', 'uploading', 'binding'].includes(item.status))) void load(true)
  }, 8000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer); if (searchTimer) clearTimeout(searchTimer) })
</script>

<template>
  <section class="qc-center adq-library-center">
    <div class="qc-center-head">
      <div><span>TENCENT ADQ DELIVERY</span><h2>腾讯 ADQ 推送与数据回流</h2><p>支持上传统一素材库，也可推送到已存在的营销单元；数据跟随每条素材记录回流。</p></div>
      <div class="qc-sync-control"><small>数据口径：账户 + 视频 ID</small><span>近 30 日累计；平台缺失字段保持“—”，不按 0 处理</span><button class="secondary-button" :disabled="loading" @click="load()"><RefreshCw :size="16" :class="{ spin: loading }" />{{ loading ? '读取中' : '刷新素材数据' }}</button></div>
    </div>
    <div v-if="notice" class="qc-sync-notice"><CheckCircle2 :size="15" />{{ notice }}</div>
    <div v-if="loading" class="qc-loading"><LoaderCircle class="spin" />正在读取腾讯 ADQ 素材库与逐条数据…</div>
    <template v-else>
      <div class="qc-status-grid">
        <article :class="{ ready: status?.authorized }"><ShieldCheck /><div><small>素材库授权</small><strong>{{ status?.message }}</strong><span class="qc-status-note">统一素材源账户 {{ status?.account_metric_accounts?.[0]?.account_id || status?.account_id }}</span></div></article>
        <article><Library /><div><small>{{ allRecords ? '全员 ADQ 记录' : '我的 ADQ 记录' }}</small><strong>{{ total }} 条</strong><span class="qc-status-note">素材库与营销单元任务统一展示</span></div></article>
        <article><BarChart3 /><div><small>回流位置</small><strong>跟随上传记录</strong><span class="qc-status-note">消耗、成交金额与 ROI</span></div></article>
        <article :class="{ ready: status?.user_authorization?.authorized }"><ShieldCheck /><div><small>营销单元写入</small><strong>{{ status?.user_authorization?.authorized ? '操作人认证可用' : '等待操作人认证' }}</strong><span class="qc-status-note">{{ status?.user_authorization?.message }}</span><button v-if="!status?.user_authorization?.authorized && status?.can_authorize_user" class="qc-status-action" :disabled="authorizing" @click="authorizeUser">{{ authorizing ? '正在跳转…' : '完成操作人认证' }}</button></div></article>
      </div>
      <div v-if="error" class="notice compact"><strong>腾讯 ADQ 操作失败</strong><span>{{ error }}</span><button @click="load()">重试</button></div>

      <section class="qc-records-card">
        <header class="qc-records-toolbar">
          <div><strong>ADQ 推送记录与数据</strong><small>按视频 ID 回流；删除只隐藏记录，不删除 ADQ 素材或投放单元</small></div>
          <div class="qc-records-controls">
            <label class="qc-search"><Search :size="15" /><input v-model="searchText" type="search" placeholder="搜索素材、视频 ID 或错误" /></label>
            <select v-model="statusFilter"><option value="all">全部状态</option><option value="success">已入素材库</option><option value="partial">部分完成</option><option value="failed">失败</option><option value="pending">等待中</option><option value="uploading">上传中</option><option value="binding">授权中</option></select>
            <select v-model.number="pageSize"><option :value="5">每页 5 条</option><option :value="10">每页 10 条</option></select>
            <DateRangeFilter v-model:start-date="pushStartDate" v-model:end-date="pushEndDate" label="推送日期" />
            <button class="qc-collapse" @click="recordsExpanded = !recordsExpanded"><ChevronDown v-if="recordsExpanded" :size="15" /><ChevronRight v-else :size="15" />{{ recordsExpanded ? '收起记录' : '展开记录' }}</button>
          </div>
        </header>
        <div v-if="recordsExpanded" class="qc-task-list">
          <nav v-if="total" class="qc-pagination qc-pagination-top"><span>{{ rangeLabel }}</span><PaginationControls :page="page" :total-pages="totalPages" @change="page = $event" /></nav>
          <div class="qc-task-row qc-task-header"><span>素材 / 发起人</span><span>目标位置</span><span>状态 / 视频 ID</span><span>消耗</span><span>成交金额</span><span>ROI</span><span>操作</span></div>
          <div v-for="task in tasks" :key="task.id" class="qc-task-item">
            <article class="qc-task-row" :class="{ 'has-error': task.error_message || task.metrics_status === 'error' }">
              <span><strong>{{ task.asset_name }}</strong><small>{{ task.created_by_name }} · {{ formatTime(task.created_at) }}</small></span>
              <span><strong>{{ task.account_name || task.account_id }}</strong><small>{{ task.adgroup_id === '__shared_library__' ? '统一素材库' : `${task.adgroup_name || task.adgroup_id} · 营销单元` }}</small></span>
              <span><b class="qc-task-status" :class="task.status">{{ taskStatusName(task) }}</b><small>{{ task.platform_asset_id ? `视频 ID ${task.platform_asset_id}` : (task.error_message || task.message) }}</small><small>{{ task.root_material_id ? `根数据素材 ID ${task.root_material_id}` : '根数据素材 ID 待回流' }}</small><small>{{ task.library_readback_message }}</small></span>
              <span><strong>{{ hasMetrics(task) ? money(task.metrics.cost_yuan) : '—' }}</strong><small>{{ metricsState(task) }}</small></span>
              <span><strong>{{ hasMetrics(task) ? money(task.metrics.order_amount_yuan) : '—' }}</strong><small>{{ hasMetrics(task) ? `${number(task.metrics.order_count)} 单` : '缺失不记 0' }}</small></span>
              <span><strong>{{ hasMetrics(task) ? number(task.metrics.order_roi) : '—' }}</strong><small>{{ hasMetrics(task) ? `${number(task.metrics.view_count)} 播放` : '待回流' }}</small></span>
              <span class="qc-row-actions"><button v-if="canRetry(task)" :disabled="Boolean(retryingBatch)" @click="retryBatch(task)"><RefreshCw :size="13" :class="{ spin: retryingBatch === task.batch_id }" />本批重试</button><span class="qc-row-actions-inline"><button v-if="canRetry(task)" :disabled="retryingTask === task.id" @click="retryTask(task)"><RefreshCw :size="13" :class="{ spin: retryingTask === task.id }" />单条重试</button><button @click="toggleTask(task.id)"><ChevronDown v-if="expandedTaskIds.includes(task.id)" :size="13" /><ChevronRight v-else :size="13" />详情</button><button v-if="canDelete(task)" @click="deleteConfirmTask = task.id"><Trash2 :size="13" /></button></span></span>
            </article>
            <section v-if="expandedTaskIds.includes(task.id) || deleteConfirmTask === task.id" class="qc-task-detail">
              <div class="qc-detail-grid"><div><small>素材库结果</small><strong>{{ task.message || '暂无结果说明' }}</strong><span>{{ task.library_readback_message }}<template v-if="task.library_readback_at"> · {{ formatTime(task.library_readback_at) }}</template></span></div><div><small>素材数据</small><strong>{{ task.metrics_message || metricsState(task) }}</strong><span>视频 ID：{{ task.platform_asset_id || '待回读' }} · 根数据素材 ID：{{ task.root_material_id || '待回流' }}<template v-if="task.metrics_start_date"> · {{ task.metrics_start_date }}—{{ task.metrics_end_date }}</template></span></div></div>
              <div v-if="deleteConfirmTask === task.id" class="qc-delete-confirm"><div><strong>隐藏这条显示记录？</strong><span>ADQ 素材和审计记录会保留。</span></div><button @click="deleteConfirmTask = ''"><X :size="13" />取消</button><button class="danger" :disabled="deletingTask === task.id" @click="deleteTask(task)"><Trash2 :size="13" />确认隐藏</button></div>
            </section>
          </div>
          <div v-if="!tasks.length" class="qc-empty"><Send :size="34" /><h3>还没有 ADQ 推送记录</h3><p>从素材详情或批量操作中选择“腾讯 ADQ”，再选择素材库或营销单元。</p></div>
          <footer v-if="total" class="qc-pagination"><span>{{ rangeLabel }}</span><PaginationControls :page="page" :total-pages="totalPages" @change="page = $event" /></footer>
        </div>
      </section>
    </template>
  </section>
</template>
