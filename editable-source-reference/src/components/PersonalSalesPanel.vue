<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { AlertTriangle, ArrowDown, ArrowUp, ArrowUpDown, BarChart3, CalendarDays, CheckCircle2, Database, RotateCcw, RefreshCw, Search, Users, WalletCards } from 'lucide-vue-next'
import { api } from '../api'
import type { PersonalSalesRanking, PersonalSalesSnapshot } from '../types'
import PaginationControls from './PaginationControls.vue'

type ChannelFilter = 'all' | 'qianchuan' | 'video'
type DetailSortKey = 'rank' | 'personName' | 'department' | 'qianchuanGmvYuan' | 'videoGmvYuan' | 'totalGmvYuan' | 'materialCount'
type NumericFilterKey = 'none' | 'rank' | 'qianchuanGmvYuan' | 'videoGmvYuan' | 'totalGmvYuan' | 'materialCount'

const data = ref<PersonalSalesSnapshot | null>(null)
const loading = ref(true)
const error = ref('')
const channel = ref<ChannelFilter>('all')
const search = ref('')
const department = ref('')
const numericFilterKey = ref<NumericFilterKey>('none')
const numericMin = ref('')
const numericMax = ref('')
const detailSortKey = ref<DetailSortKey>('totalGmvYuan')
const detailSortDirection = ref<'asc' | 'desc'>('desc')
const detailPage = ref(1)
const detailPageSize = ref<10 | 20 | 50>(10)
const startDate = ref('')
const endDate = ref('')

const numberFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 })
const integerFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })

const metricValue = (row: PersonalSalesRanking) => {
  if (channel.value === 'qianchuan') return row.qianchuanGmvYuan
  if (channel.value === 'video') return row.videoGmvYuan
  return row.totalGmvYuan
}

const metricMaterialCount = (row: PersonalSalesRanking) => {
  if (channel.value === 'qianchuan') return row.qianchuanMaterialCount
  if (channel.value === 'video') return row.videoMaterialCount
  return row.materialCount
}

const channelRows = computed(() => [...(data.value?.rankings || [])]
  .filter(row => metricValue(row) > 0)
  .sort((a, b) => metricValue(b) - metricValue(a)))
const departmentOptions = computed(() => Array.from(new Set(channelRows.value.map(row => row.department).filter(Boolean))).sort((a, b) => a.localeCompare(b, 'zh-CN')))
const rowKey = (row: PersonalSalesRanking) => row.employeeId || row.personName
const channelRankMap = computed(() => new Map(channelRows.value.map((row, index) => [rowKey(row), index + 1])))
const rowRank = (row: PersonalSalesRanking) => channelRankMap.value.get(rowKey(row)) || row.rank || 0
const numericValue = (row: PersonalSalesRanking, key: NumericFilterKey | DetailSortKey): number => {
  if (key === 'rank') return rowRank(row)
  if (key === 'qianchuanGmvYuan') return row.qianchuanGmvYuan
  if (key === 'videoGmvYuan') return row.videoGmvYuan
  if (key === 'totalGmvYuan') return row.totalGmvYuan
  if (key === 'materialCount') return row.materialCount
  return 0
}
const rankedRows = computed(() => channelRows.value
  .filter(row => {
    const keyword = search.value.trim().toLowerCase()
    return !keyword || `${row.personName} ${row.department} ${row.employeeId}`.toLowerCase().includes(keyword)
  })
  .filter(row => !department.value || row.department === department.value)
  .filter(row => {
    if (numericFilterKey.value === 'none') return true
    const value = numericValue(row, numericFilterKey.value)
    const min = numericMin.value.trim() === '' ? null : Number(numericMin.value)
    const max = numericMax.value.trim() === '' ? null : Number(numericMax.value)
    if (min !== null && Number.isFinite(min) && value < min) return false
    if (max !== null && Number.isFinite(max) && value > max) return false
    return true
  })
  .sort((left, right) => {
    const direction = detailSortDirection.value === 'asc' ? 1 : -1
    if (detailSortKey.value === 'personName' || detailSortKey.value === 'department') {
      return String(left[detailSortKey.value]).localeCompare(String(right[detailSortKey.value]), 'zh-CN') * direction
    }
    return (numericValue(left, detailSortKey.value) - numericValue(right, detailSortKey.value)) * direction
  }))

const topRows = computed(() => [...rankedRows.value].sort((a, b) => metricValue(b) - metricValue(a)).slice(0, 10))
const maxValue = computed(() => Math.max(1, ...topRows.value.map(metricValue)))
const selectedTotalGmv = computed(() => rankedRows.value.reduce((total, row) => total + metricValue(row), 0))
const selectedMaterialCount = computed(() => rankedRows.value.reduce((total, row) => total + metricMaterialCount(row), 0))
const selectedChannelLabel = computed(() => channel.value === 'qianchuan' ? '千川' : channel.value === 'video' ? '视频号' : '综合')
const selectedPeriodLabel = computed(() => data.value?.meta.periodStart === data.value?.meta.periodEnd ? '当日' : '所选周期')
const detailPageCount = computed(() => Math.max(1, Math.ceil(rankedRows.value.length / detailPageSize.value)))
const detailRows = computed(() => {
  const start = (detailPage.value - 1) * detailPageSize.value
  return rankedRows.value.slice(start, start + detailPageSize.value)
})
const detailRangeLabel = computed(() => rankedRows.value.length
  ? `第 ${(detailPage.value - 1) * detailPageSize.value + 1}–${Math.min(rankedRows.value.length, detailPage.value * detailPageSize.value)} 条，共 ${rankedRows.value.length} 条`
  : '共 0 条')

const formatGmv = (value: number) => value >= 10000
  ? `${(value / 10000).toFixed(value >= 1000000 ? 1 : 2)}万`
  : `¥${numberFormatter.format(value)}`

const formatDate = (value?: string | null) => {
  if (!value) return '待读取'
  const match = value.match(/(\d{4})-(\d{2})-(\d{2})/)
  return match ? `${match[1]}.${match[2]}.${match[3]}` : value
}

const barWidth = (row: PersonalSalesRanking) => `${Math.max(1.6, metricValue(row) / maxValue.value * 100)}%`
const publicSnapshotMessage = (value?: string | null) => value
  ? '上游数据刷新暂时异常，当前保留最近一次成功快照；请稍后重试。'
  : '部分根数据尚未到齐，当前保留上次成功结果，不会将缺失值显示为 0。'
const qianchuanShare = (row: PersonalSalesRanking) => row.totalGmvYuan > 0 ? row.qianchuanGmvYuan / row.totalGmvYuan * 100 : 0
const videoShare = (row: PersonalSalesRanking) => row.totalGmvYuan > 0 ? row.videoGmvYuan / row.totalGmvYuan * 100 : 0
const toggleDetailSort = (key: DetailSortKey) => {
  if (detailSortKey.value === key) detailSortDirection.value = detailSortDirection.value === 'asc' ? 'desc' : 'asc'
  else {
    detailSortKey.value = key
    detailSortDirection.value = key === 'personName' || key === 'department' ? 'asc' : 'desc'
  }
}
const sortIcon = (key: DetailSortKey) => detailSortKey.value !== key ? ArrowUpDown : detailSortDirection.value === 'asc' ? ArrowUp : ArrowDown
const resetDetailFilters = () => {
  search.value = ''
  department.value = ''
  numericFilterKey.value = 'none'
  numericMin.value = ''
  numericMax.value = ''
  detailSortKey.value = 'totalGmvYuan'
  detailSortDirection.value = 'desc'
}

const load = async (useSelectedDates = false) => {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.personalSales(useSelectedDates ? startDate.value : '', useSelectedDates ? endDate.value : '')
    if (!startDate.value) startDate.value = data.value.meta.periodStart
    if (!endDate.value) endDate.value = data.value.meta.periodEnd
    if (data.value?.automation.lastError) {
      data.value.automation.lastError = publicSnapshotMessage(data.value.automation.lastError)
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '暂时无法读取个人成交数据'
  } finally {
    loading.value = false
  }
}

const applyDateRange = () => void load(true)
const showCompleteDay = () => {
  if (!data.value?.meta.completeThroughDate) return
  startDate.value = data.value.meta.completeThroughDate
  endDate.value = data.value.meta.completeThroughDate
  void load(true)
}
const showCurrentMonth = () => {
  if (!data.value) return
  startDate.value = data.value.meta.periodStart.slice(0, 8) + '01'
  endDate.value = data.value.meta.completeThroughDate
  void load(true)
}

const changeDetailPage = (nextPage: number) => { detailPage.value = nextPage }
watch([search, channel, department, numericFilterKey, numericMin, numericMax, detailSortKey, detailSortDirection, detailPageSize], () => { detailPage.value = 1 })
watch(() => rankedRows.value.length, () => {
  if (detailPage.value > detailPageCount.value) detailPage.value = detailPageCount.value
})

onMounted(load)
</script>

<template>
  <section class="personal-sales-panel">
    <header class="sales-heading">
      <div>
        <span class="sales-eyebrow">PERSONAL PERFORMANCE</span>
        <h1>个人成交数据</h1>
        <p>统一查看千川与视频号的个人成交GMV、素材贡献和当月排名。</p>
      </div>
      <div v-if="data" class="sales-freshness" :class="{ stale: !data.automation.isFresh }">
        <CheckCircle2 v-if="data.automation.isFresh" :size="18" />
        <AlertTriangle v-else :size="18" />
        <span><strong>{{ data.automation.isFresh ? '数据已更新' : '使用最近成功快照' }}</strong><small>完整至 {{ formatDate(data.meta.completeThroughDate) }} · {{ data.automation.schedule }}更新</small></span>
      </div>
    </header>

    <div v-if="loading" class="sales-loading">
      <div v-for="item in 4" :key="item" class="sales-skeleton"></div>
    </div>

    <div v-else-if="error" class="sales-error">
      <AlertTriangle :size="28" />
      <div><strong>暂时无法读取个人成交数据</strong><span>{{ error }}</span></div>
      <button @click="load(true)"><RefreshCw :size="15" />重试</button>
    </div>

    <template v-else-if="data">
      <div v-if="!data.automation.isFresh || data.automation.lastError" class="snapshot-notice">
        <AlertTriangle :size="17" />
        <span>{{ data.automation.lastError || '部分根数据尚未到齐，当前保留上次成功结果，不会将缺失值显示为0。' }}</span>
      </div>

      <section class="sales-summary">
        <article>
          <span class="summary-icon primary"><WalletCards :size="21" /></span>
          <div><small>{{ selectedChannelLabel }}已归属GMV</small><strong>{{ formatGmv(selectedTotalGmv) }}</strong><em>{{ selectedPeriodLabel }}</em></div>
        </article>
        <article>
          <span class="summary-icon"><Users :size="21" /></span>
          <div><small>有成交同事</small><strong>{{ rankedRows.length }} 人</strong><em>GMV为0不进入</em></div>
        </article>
        <article>
          <span class="summary-icon"><BarChart3 :size="21" /></span>
          <div><small>去重素材数</small><strong>{{ integerFormatter.format(selectedMaterialCount) }} 条</strong><em>同名素材合并计数</em></div>
        </article>
        <article>
          <span class="summary-icon"><Database :size="21" /></span>
          <div><small>GMV归属率</small><strong>{{ data.quality.mappedGmvRate === null ? '待核验' : `${(data.quality.mappedGmvRate * 100).toFixed(2)}%` }}</strong><em>未归属不强行分摊</em></div>
        </article>
      </section>

      <section class="sales-control-bar">
        <div class="sales-period-control"><div class="sales-period"><CalendarDays :size="17" /><span>数据周期</span><strong>{{ formatDate(data.meta.periodStart) }}—{{ formatDate(data.meta.periodEnd) }}</strong></div><div class="sales-date-inputs"><input v-model="startDate" type="date" :max="data.meta.completeThroughDate" aria-label="成交开始日期" /><i>—</i><input v-model="endDate" type="date" :max="data.meta.completeThroughDate" aria-label="成交结束日期" /><button :disabled="loading || !startDate || !endDate" @click="applyDateRange">查询</button><button @click="showCompleteDay">最新完整日</button><button @click="showCurrentMonth">当月</button></div></div>
        <div class="channel-tabs" aria-label="成交渠道筛选">
          <button :class="{ active: channel === 'all' }" @click="channel = 'all'">综合</button>
          <button :class="{ active: channel === 'qianchuan' }" @click="channel = 'qianchuan'">千川</button>
          <button :class="{ active: channel === 'video' }" @click="channel = 'video'">视频号</button>
        </div>
      </section>

      <section class="ranking-card">
        <div class="card-heading">
          <div><span>GMV RANKING</span><h2>{{ selectedChannelLabel }}成交GMV TOP10</h2><p>单位：元；横轴从0开始，综合视图用深浅蓝区分千川与视频号。</p></div>
          <span class="source-chip">{{ data.meta.sourceLabel }} · {{ data.meta.schedule }}</span>
        </div>

        <div v-if="topRows.length" class="ranking-column-labels" aria-hidden="true">
          <span>成交额</span>
          <span>素材数</span>
        </div>
        <div v-if="topRows.length" class="ranking-bars">
          <div v-for="(row, index) in topRows" :key="row.employeeId || row.personName" class="ranking-row">
            <span class="rank-number" :class="`rank-${Math.min(index + 1, 4)}`">{{ index + 1 }}</span>
            <span class="rank-person"><strong>{{ row.personName }}</strong><small>{{ row.department }}</small></span>
            <div class="rank-bar-track">
              <div class="rank-bar-fill" :class="channel" :style="{ width: barWidth(row) }">
                <template v-if="channel === 'all'">
                  <i class="qianchuan-segment" :style="{ width: `${qianchuanShare(row)}%` }"></i>
                  <i class="video-segment" :style="{ width: `${videoShare(row)}%` }"></i>
                </template>
              </div>
            </div>
            <strong class="rank-value">{{ formatGmv(metricValue(row)) }}</strong>
            <span class="rank-material"><strong>{{ metricMaterialCount(row) }}</strong><small>条素材</small></span>
          </div>
        </div>
        <div v-else class="sales-empty">当前筛选下暂无正GMV数据</div>
      </section>

      <section class="ranking-table-card">
        <div class="card-heading table-heading">
          <div><span>ALL CONTRIBUTORS</span><h2>全部个人成交明细</h2><p>支持按姓名、部门或工号查找。</p></div>
          <label class="sales-search"><Search :size="16" /><input v-model="search" placeholder="搜索同事或部门" /></label>
        </div>
        <div class="sales-detail-filters" aria-label="个人成交明细筛选">
          <label><span>部门</span><select v-model="department"><option value="">全部部门</option><option v-for="item in departmentOptions" :key="item" :value="item">{{ item }}</option></select></label>
          <label><span>数值字段</span><select v-model="numericFilterKey"><option value="none">不限数值</option><option value="rank">排名（名）</option><option value="qianchuanGmvYuan">千川GMV（元）</option><option value="videoGmvYuan">视频号GMV（元）</option><option value="totalGmvYuan">总GMV（元）</option><option value="materialCount">去重素材（条）</option></select></label>
          <label><span>最小值</span><input v-model="numericMin" type="number" min="0" placeholder="不限" :disabled="numericFilterKey === 'none'" /></label>
          <label><span>最大值</span><input v-model="numericMax" type="number" min="0" placeholder="不限" :disabled="numericFilterKey === 'none'" /></label>
          <button type="button" @click="resetDetailFilters"><RotateCcw :size="15" />重置</button>
        </div>
        <nav v-if="rankedRows.length" class="sales-detail-pagination sales-detail-pagination-top" aria-label="个人成交明细顶部分页">
          <span>{{ detailRangeLabel }}</span>
          <label>每页<select v-model.number="detailPageSize"><option :value="10">10 条</option><option :value="20">20 条</option><option :value="50">50 条</option></select></label>
          <PaginationControls :page="detailPage" :total-pages="detailPageCount" @change="changeDetailPage" />
        </nav>
        <div class="ranking-table-wrap">
          <table>
            <thead><tr><th><button @click="toggleDetailSort('rank')">排名（名）<component :is="sortIcon('rank')" :size="13" /></button></th><th><button @click="toggleDetailSort('personName')">姓名<component :is="sortIcon('personName')" :size="13" /></button></th><th><button @click="toggleDetailSort('department')">部门<component :is="sortIcon('department')" :size="13" /></button></th><th><button @click="toggleDetailSort('qianchuanGmvYuan')">千川GMV（元）<component :is="sortIcon('qianchuanGmvYuan')" :size="13" /></button></th><th><button @click="toggleDetailSort('videoGmvYuan')">视频号GMV（元）<component :is="sortIcon('videoGmvYuan')" :size="13" /></button></th><th><button @click="toggleDetailSort('totalGmvYuan')">总GMV（元）<component :is="sortIcon('totalGmvYuan')" :size="13" /></button></th><th><button @click="toggleDetailSort('materialCount')">去重素材（条）<component :is="sortIcon('materialCount')" :size="13" /></button></th></tr></thead>
            <tbody>
              <tr v-for="(row, index) in detailRows" :key="`table-${row.employeeId || row.personName}`">
                <td><span class="table-rank">{{ rowRank(row) }}</span></td>
                <td><strong>{{ row.personName }}</strong><small>{{ row.employeeId }}</small></td>
                <td>{{ row.department }}</td>
                <td>{{ numberFormatter.format(row.qianchuanGmvYuan) }} <small class="cell-unit">元</small></td>
                <td>{{ numberFormatter.format(row.videoGmvYuan) }} <small class="cell-unit">元</small></td>
                <td class="table-total">{{ numberFormatter.format(row.totalGmvYuan) }} <small class="cell-unit">元</small></td>
                <td>{{ row.materialCount }} <small class="cell-unit">条</small></td>
              </tr>
            </tbody>
          </table>
        </div>
        <nav v-if="rankedRows.length" class="sales-detail-pagination sales-detail-pagination-bottom" aria-label="个人成交明细底部分页">
          <span>{{ detailRangeLabel }}</span>
          <PaginationControls :page="detailPage" :total-pages="detailPageCount" @change="changeDetailPage" />
        </nav>
      </section>

      <section class="sales-footnotes">
        <div><strong>数据口径</strong><span>千川使用ROI2含券成交GMV；视频号海豚使用素材成交金额；ADQ暂以下单金额作为GMV代理口径。</span></div>
        <div><strong>归属说明</strong><span>根表总GMV {{ formatGmv(data.quality.totalSourceGmvYuan) }}，未归属 {{ formatGmv(data.quality.unmappedGmvYuan) }}，多人冲突 {{ formatGmv(data.quality.conflictGmvYuan) }}；均不强行计入个人。</span></div>
      </section>
    </template>
  </section>
</template>

<style scoped>
.personal-sales-panel { min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr); gap: 22px; color: #17324a; font-variant-numeric: tabular-nums lining-nums; }
.personal-sales-panel > * { min-width: 0; }
.sales-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 28px; padding: 8px 2px 2px; }
.sales-eyebrow, .card-heading > div > span { color: #238bc4; font-size: 12px; font-weight: 800; letter-spacing: 2px; }
.sales-heading h1 { margin: 8px 0 6px; font-size: clamp(31px, 3vw, 48px); line-height: 1.05; letter-spacing: -.045em; }
.sales-heading p, .card-heading p { margin: 0; color: #6f879a; font-size: 15px; line-height: 1.7; }
.sales-freshness { display: flex; align-items: center; gap: 10px; min-width: 230px; max-width: 100%; padding: 13px 15px; border: 1px solid #cfe8dc; border-radius: 13px; color: #31845c; background: #f5fbf7; }
.sales-freshness.stale { color: #946d24; border-color: #ecdcae; background: #fffaf0; }
.sales-freshness span, .sales-freshness strong, .sales-freshness small { display: block; }
.sales-freshness strong { font-size: 14px; }.sales-freshness small { margin-top: 3px; color: #779082; font-size: 12px; }
.sales-loading, .sales-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.sales-skeleton { height: 105px; border-radius: 16px; background: linear-gradient(100deg, #e9f2f7 20%, #f7fbfd 38%, #e9f2f7 58%); background-size: 200% 100%; animation: pulse 1.4s infinite; }
@keyframes pulse { to { background-position-x: -200%; } }
.sales-error { display: flex; align-items: center; gap: 14px; padding: 22px; border: 1px solid #f0d4cd; border-radius: 16px; color: #9d4f43; background: #fff8f6; }
.sales-error div { flex: 1; }.sales-error strong, .sales-error span { display: block; }.sales-error span { margin-top: 4px; font-size: 14px; }
.sales-error button { display: flex; align-items: center; gap: 6px; padding: 9px 12px; border: 1px solid #e4b8ad; border-radius: 9px; color: #8f493f; background: #fff; }
.snapshot-notice { display: flex; align-items: center; gap: 9px; padding: 11px 14px; border: 1px solid #ebd89f; border-radius: 11px; color: #836321; font-size: 13px; background: #fffaf0; }
.sales-summary article { min-height: 102px; display: flex; align-items: center; gap: 13px; padding: 18px; border: 1px solid #dceaf2; border-radius: 16px; background: #fff; box-shadow: 0 12px 28px rgba(37, 102, 140, .06); }
.summary-icon { width: 40px; height: 40px; flex: 0 0 auto; display: grid; place-items: center; border-radius: 11px; color: #238bc4; background: #eaf7fd; }
.summary-icon.primary { color: #fff; background: #218fc9; box-shadow: 0 8px 20px rgba(33, 143, 201, .22); }
.sales-summary small, .sales-summary strong, .sales-summary em { display: block; font-style: normal; }
.sales-summary small { color: #71879a; font-size: 12px; }.sales-summary strong { margin-top: 4px; color: #14324a; font-size: 23px; letter-spacing: -.035em; }.sales-summary em { margin-top: 3px; color: #9aacb9; font-size: 11px; }
.sales-control-bar { display: flex; align-items: center; justify-content: space-between; gap: 14px; padding: 11px 13px; border: 1px solid #d8e8f1; border-radius: 13px; background: rgba(255,255,255,.82); }
.sales-period { display: flex; align-items: center; gap: 8px; color: #6a8294; font-size: 13px; }.sales-period strong { color: #26465e; }
.sales-period-control { min-width:0; display:grid; gap:8px; }
.sales-date-inputs { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }.sales-date-inputs i { color:#9badb9; font-style:normal; }.sales-date-inputs input { height:34px; padding:0 8px; border:1px solid #cfe1eb; border-radius:8px; color:#31536a; background:#fff; }.sales-date-inputs button { height:34px; padding:0 9px; border:1px solid #cfe1eb; border-radius:8px; color:#187fae; background:#fff; }.sales-date-inputs button:disabled { opacity:.5; }
.channel-tabs { display: flex; gap: 3px; padding: 3px; border-radius: 9px; background: #edf4f8; }
.channel-tabs button { min-width: 65px; padding: 7px 12px; border: 0; border-radius: 7px; color: #6c8393; font-size: 13px; background: transparent; transition: .2s ease; }
.channel-tabs button:hover { color: #1d79a9; }.channel-tabs button.active { color: #fff; background: #218fc9; box-shadow: 0 5px 14px rgba(33,143,201,.2); }
.ranking-card, .ranking-table-card { padding: 22px; border: 1px solid #d9e8f1; border-radius: 18px; background: #fff; box-shadow: 0 16px 40px rgba(34, 94, 128, .06); }
.card-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; margin-bottom: 19px; }
.card-heading h2 { margin: 5px 0 4px; color: #17344c; font-size: 20px; letter-spacing: -.02em; }
.source-chip { padding: 7px 10px; border-radius: 8px; color: #397a9e; font-size: 12px; background: #edf8fd; }
.ranking-column-labels { display: grid; grid-template-columns: 32px 122px minmax(220px, 1fr) 90px 65px; gap: 11px; margin: -3px 0 5px; color: #7890a1; font-size: 12px; font-weight: 700; letter-spacing: .08em; }
.ranking-column-labels span:first-child { grid-column: 4; text-align: right; }
.ranking-column-labels span:last-child { grid-column: 5; text-align: right; }
.ranking-bars { display: grid; gap: 8px; }
.ranking-row { display: grid; grid-template-columns: 32px 122px minmax(220px, 1fr) 90px 65px; align-items: center; gap: 11px; min-height: 44px; }
.rank-number { width: 28px; height: 28px; display: grid; place-items: center; border: 1px solid #d7e6ef; border-radius: 9px; color: #648195; font-size: 14px; font-weight: 800; background: #f6fafc; }
.rank-number.rank-1 { color: #fff; border-color: #ec8d32; background: #ec8d32; box-shadow: 0 5px 13px rgba(236,141,50,.22); }
.rank-number.rank-2 { color: #fff; border-color: #268ec8; background: #268ec8; }.rank-number.rank-3 { color: #fff; border-color: #55a96b; background: #55a96b; }
.rank-person { min-width: 0; }.rank-person strong, .rank-person small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.rank-person strong { font-size: 15px; }.rank-person small { margin-top: 3px; color: #8ca0ae; font-size: 11px; }
.rank-bar-track { height: 26px; overflow: hidden; border-radius: 8px; background: #eef4f7; }
.rank-bar-fill { height: 100%; display: flex; overflow: hidden; border-radius: 8px; background: #238fc8; }
.rank-bar-fill.qianchuan { background: #1d719f; }.rank-bar-fill.video { background: #45b5df; }
.rank-bar-fill i { height: 100%; }.qianchuan-segment { background: #176b99; }.video-segment { background: #48b7df; }
.rank-value { color: #1f6f9b; text-align: right; font-size: 15px; }.rank-material { display: flex; align-items: baseline; justify-content: flex-end; gap: 2px; color: #52778e; text-align: right; white-space: nowrap; }.rank-material strong { color: #245f80; font-size: 15px; font-weight: 800; }.rank-material small { font-size: 11px; font-weight: 600; }
.table-heading { align-items: center; }.sales-search { width: min(280px, 36%); height: 36px; display: flex; align-items: center; gap: 7px; padding: 0 10px; border: 1px solid #dce8ef; border-radius: 9px; color: #91a3af; background: #f8fbfd; }.sales-search input { width: 100%; border: 0; outline: 0; color: #29485e; font-size: 13px; background: transparent; }
.sales-detail-filters { display: grid; grid-template-columns: minmax(145px, 1fr) minmax(180px, 1.15fr) minmax(110px, .7fr) minmax(110px, .7fr) auto; gap: 10px; margin: -5px 0 15px; padding: 13px; border: 1px solid #dce9f0; border-radius: 13px; background: #f8fbfd; }.sales-detail-filters label { min-width: 0; display: grid; gap: 5px; color: #688195; font-size: 11px; font-weight: 700; }.sales-detail-filters select, .sales-detail-filters input { width: 100%; height: 38px; padding: 0 10px; border: 1px solid #d2e3ec; border-radius: 9px; outline: 0; color: #29485e; font: inherit; font-size: 13px; font-weight: 500; background: #fff; }.sales-detail-filters select:focus, .sales-detail-filters input:focus { border-color: #2b99d0; box-shadow: 0 0 0 3px rgba(43, 153, 208, .1); }.sales-detail-filters button { align-self: end; height: 38px; display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 0 12px; border: 1px solid #d2e3ec; border-radius: 9px; color: #41728f; background: #fff; }
.ranking-table-wrap { overflow-x: auto; }.ranking-table-card table { width: 100%; min-width: 920px; border-collapse: collapse; font-size: 13px; }.ranking-table-card th { padding: 0; color: #708798; text-align: left; font-weight: 600; border-bottom: 1px solid #dfeaf0; background: #f7fafc; }.ranking-table-card th button { width: 100%; min-height: 42px; display: flex; align-items: center; justify-content: space-between; gap: 5px; padding: 9px; border: 0; color: inherit; font: inherit; font-weight: 700; text-align: left; white-space: nowrap; background: transparent; }.ranking-table-card th button:hover { color: #167fb8; background: #edf8fd; }.ranking-table-card td { padding: 11px 9px; color: #4f687a; border-bottom: 1px solid #edf2f5; }.ranking-table-card tbody tr:hover { background: #f7fbfd; }.ranking-table-card td strong, .ranking-table-card td > small:not(.cell-unit) { display: block; }.ranking-table-card td strong { color: #203f55; font-size: 14px; }.ranking-table-card td > small:not(.cell-unit) { margin-top: 3px; color: #9aabb6; font-size: 11px; }.cell-unit { display: inline; margin: 0; color: #8da1ae; font-size: 10px; font-weight: 500; }.table-rank { width: 24px; height: 24px; display: grid; place-items: center; border-radius: 7px; color: #397a9e; background: #ecf7fc; }.ranking-table-card .table-total { color: #166f9f; font-weight: 800; }
.sales-empty { padding: 38px; color: #8ea0ac; text-align: center; font-size: 14px; }
.sales-footnotes { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }.sales-footnotes div { padding: 14px 15px; border: 1px solid #dce9f0; border-radius: 12px; background: #f8fbfd; }.sales-footnotes strong, .sales-footnotes span { display: block; }.sales-footnotes strong { color: #345b74; font-size: 13px; }.sales-footnotes span { margin-top: 5px; color: #7b909f; font-size: 12px; line-height: 1.6; }
@media (max-width: 1100px) { .sales-summary { grid-template-columns: 1fr 1fr; }.ranking-column-labels, .ranking-row { grid-template-columns: 32px 100px minmax(120px, 1fr) 78px 70px; }.sales-detail-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }.sales-detail-filters button { align-self: stretch; } }
@media (max-width: 720px) { .sales-heading { align-items: flex-start; flex-direction: column; }.sales-freshness { width: 100%; }.sales-summary { grid-template-columns: 1fr 1fr; }.sales-control-bar, .card-heading { align-items: stretch; flex-direction: column; }.channel-tabs { width: 100%; }.channel-tabs button { flex: 1; }.ranking-card, .ranking-table-card { padding: 15px; }.ranking-column-labels, .rank-material { display: none; }.ranking-row { grid-template-columns: 28px 84px minmax(100px, 1fr) 70px; gap: 7px; }.rank-person small { display: none; }.rank-bar-track { height: 22px; }.sales-search { width: 100%; }.sales-detail-filters { grid-template-columns: 1fr 1fr; }.sales-footnotes { grid-template-columns: 1fr; } }
@media (max-width: 480px) { .sales-summary, .sales-detail-filters { grid-template-columns: 1fr; }.ranking-row { grid-template-columns: 26px 70px minmax(70px, 1fr) 62px; }.rank-value { font-size: 13px; }.sales-period { align-items: flex-start; flex-wrap: wrap; } }
</style>
