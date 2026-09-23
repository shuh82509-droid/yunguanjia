<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Activity, CalendarRange, CircleDollarSign, Clapperboard, Film, Flame, RefreshCw, Search, Users } from 'lucide-vue-next'
import { api } from '../api'
import type { UploadAnalyticsContributor, UploadAnalyticsDaily, UploadAnalyticsResult } from '../types'

type PeriodKey = 'today' | '7' | '30' | 'custom'
type RankingMetric = 'total' | 'source' | 'remix' | 'transacted' | 'hits'

const todayText = () => {
  const now = new Date()
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 10)
}
const shiftDate = (value: string, offset: number) => {
  const current = new Date(`${value}T00:00:00`)
  current.setDate(current.getDate() + offset)
  return current.toISOString().slice(0, 10)
}

const period = ref<PeriodKey>('7')
const endDate = ref(todayText())
const startDate = ref(shiftDate(endDate.value, -6))
const data = ref<UploadAnalyticsResult | null>(null)
const loading = ref(true)
const error = ref('')
const rankingMetric = ref<RankingMetric>('total')
const peopleSearch = ref('')
const activeDay = ref(0)
const detailPage = ref(1)
const detailPageSize = ref(10)

const load = async () => {
  if (!startDate.value || !endDate.value || startDate.value > endDate.value) {
    error.value = '开始日期不能晚于结束日期'
    return
  }
  loading.value = true
  error.value = ''
  try {
    data.value = await api.uploadAnalytics(startDate.value, endDate.value)
    activeDay.value = Math.max(0, (data.value.daily.length || 1) - 1)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '数据统计暂时无法读取'
  } finally {
    loading.value = false
  }
}

const choosePeriod = (next: PeriodKey) => {
  period.value = next
  if (next === 'custom') return
  endDate.value = todayText()
  startDate.value = next === 'today' ? endDate.value : shiftDate(endDate.value, -(Number(next) - 1))
  void load()
}

const chartWidth = 760
const chartHeight = 248
const chartTop = 22
const chartBottom = 44
const chartLeft = 18
const chartRight = 18
const chartInnerHeight = chartHeight - chartTop - chartBottom
const chartItems = computed(() => {
  const rows = data.value?.daily || []
  const max = Math.max(1, ...rows.map(item => item.total))
  const slot = (chartWidth - chartLeft - chartRight) / Math.max(1, rows.length)
  const barWidth = Math.max(5, Math.min(28, slot * .56))
  return rows.map((item, index) => {
    const sourceHeight = item.source / max * chartInnerHeight
    const remixHeight = item.remix / max * chartInnerHeight
    const x = chartLeft + slot * index + (slot - barWidth) / 2
    const base = chartTop + chartInnerHeight
    return {
      ...item,
      index,
      x,
      width: barWidth,
      sourceY: base - sourceHeight,
      sourceHeight,
      remixY: base - sourceHeight - remixHeight,
      remixHeight,
      pointX: x + barWidth / 2,
      labelY: Math.max(15, base - item.total / max * chartInnerHeight - 8),
    }
  })
})
const activeDaily = computed<UploadAnalyticsDaily | null>(() => chartItems.value[activeDay.value] || null)
const sourceShare = computed(() => {
  const summary = data.value?.summary
  if (!summary?.total) return 0
  return Math.round(summary.source / summary.total * 100)
})
const metricLabels: Record<RankingMetric, string> = {
  total: '上传总量', source: '视频素材', remix: '混剪成片', transacted: '有成交素材', hits: '爆款素材',
}
const ranking = computed(() => {
  const keyword = peopleSearch.value.trim().toLowerCase()
  return [...(data.value?.contributors || [])]
    .filter(item => !keyword || item.name.toLowerCase().includes(keyword) || item.number.toLowerCase().includes(keyword))
    .sort((left, right) => right[rankingMetric.value] - left[rankingMetric.value] || right.total - left.total)
})
const rankingMax = computed(() => Math.max(1, ...ranking.value.map(item => item[rankingMetric.value])))
const detailPageCount = computed(() => Math.max(1, Math.ceil(ranking.value.length / detailPageSize.value)))
const detailRows = computed(() => {
  const start = (detailPage.value - 1) * detailPageSize.value
  return ranking.value.slice(start, start + detailPageSize.value)
})
const detailStart = computed(() => ranking.value.length ? (detailPage.value - 1) * detailPageSize.value + 1 : 0)
const detailEnd = computed(() => Math.min(ranking.value.length, detailPage.value * detailPageSize.value))
const heatmapPeople = computed(() => ranking.value.slice(0, 12))
const heatmapMax = computed(() => Math.max(1, ...heatmapPeople.value.flatMap(person => person.daily.map(item => item.total))))
const formatShortDate = (value: string) => value.slice(5).replace('-', '/')
const formatNumber = (value: number | null) => value === null ? '待回流' : value.toLocaleString('zh-CN')
const cellOpacity = (value: number) => value ? .58 + value / heatmapMax.value * .42 : .18

watch([peopleSearch, rankingMetric, detailPageSize], () => { detailPage.value = 1 })
watch(detailPageCount, value => { detailPage.value = Math.min(detailPage.value, value) })

onMounted(load)
</script>

<template>
  <section class="analytics-panel">
    <header class="analytics-header">
      <div>
        <span class="analytics-kicker">TEAM CONTENT INTELLIGENCE</span>
        <h1>素材生产数据统计</h1>
        <p>按同事、日期和素材库查看上传产能，并结合已核验千川回流识别成交素材与爆款。</p>
      </div>
      <div class="analytics-periods" aria-label="统计周期">
        <button :class="{ active: period === 'today' }" @click="choosePeriod('today')">今天</button>
        <button :class="{ active: period === '7' }" @click="choosePeriod('7')">近 7 天</button>
        <button :class="{ active: period === '30' }" @click="choosePeriod('30')">近 30 天</button>
        <button :class="{ active: period === 'custom' }" @click="choosePeriod('custom')">自定义</button>
      </div>
    </header>

    <div class="analytics-toolbar">
      <CalendarRange :size="19" />
      <label>开始日期<input v-model="startDate" type="date" @change="period = 'custom'" /></label>
      <span>至</span>
      <label>结束日期<input v-model="endDate" type="date" @change="period = 'custom'" /></label>
      <button class="analytics-query" :disabled="loading" @click="load"><RefreshCw :size="17" :class="{ spinning: loading }" />{{ loading ? '读取中' : '应用周期' }}</button>
      <small v-if="data">上传时间按上海时区；{{ data.range.days }} 个自然日</small>
    </div>

    <div v-if="error" class="analytics-error"><strong>统计读取失败</strong><span>{{ error }}</span><button @click="load">重新读取</button></div>
    <div v-else-if="loading && !data" class="analytics-loading"><Activity :size="28" /><strong>正在汇总团队素材数据</strong><span>上传量、分类与已核验成交数据正在并行整理…</span></div>

    <template v-else-if="data">
      <div class="analytics-kpis">
        <article><span class="analytics-kpi-icon total"><Activity /></span><div><small>周期上传总数</small><strong>{{ formatNumber(data.summary.total) }}</strong><em>{{ data.range.start_date }} — {{ data.range.end_date }}</em></div></article>
        <article><span class="analytics-kpi-icon source"><Film /></span><div><small>视频素材</small><strong>{{ formatNumber(data.summary.source) }}</strong><em>一创原片与源素材</em></div></article>
        <article><span class="analytics-kpi-icon remix"><Clapperboard /></span><div><small>混剪成片</small><strong>{{ formatNumber(data.summary.remix) }}</strong><em>完成剪辑的可投放成片</em></div></article>
        <article><span class="analytics-kpi-icon gmv"><CircleDollarSign /></span><div><small>有成交数据</small><strong>{{ formatNumber(data.summary.transacted) }}</strong><em>已核验千川回流素材</em></div></article>
        <article><span class="analytics-kpi-icon hits"><Flame /></span><div><small>爆款素材</small><strong>{{ formatNumber(data.summary.hits) }}</strong><em>周期累计成交额 &gt; ¥50,000</em></div></article>
      </div>

      <div class="analytics-coverage" :class="data.coverage.state">
        <span><i></i>{{ data.coverage.message }}</span>
        <small>{{ data.coverage.gmv_source }} · 已核验 {{ data.coverage.verified_rows }} 条唯一日级记录</small>
      </div>

      <div class="analytics-main-grid">
        <article class="analytics-card trend-card">
          <header><div><span>DAILY OUTPUT</span><h2>每日上传趋势</h2><p>浅蓝色为视频素材，深蓝色为混剪成片；柱顶直接显示当日上传总量。</p></div><div v-if="activeDaily" class="active-day"><small>{{ activeDaily.date }}</small><strong>{{ activeDaily.total }} 条</strong><span>素材 {{ activeDaily.source }} · 成片 {{ activeDaily.remix }}</span></div></header>
          <div class="chart-legend"><span><i class="source"></i>视频素材</span><span><i class="remix"></i>混剪成片</span></div>
          <div class="trend-chart" role="img" aria-label="每日上传趋势图">
            <svg :viewBox="`0 0 ${chartWidth} ${chartHeight}`" preserveAspectRatio="none">
              <line v-for="level in 4" :key="level" :x1="chartLeft" :x2="chartWidth - chartRight" :y1="chartTop + chartInnerHeight / 4 * level" :y2="chartTop + chartInnerHeight / 4 * level" class="grid-line" />
              <g v-for="item in chartItems" :key="item.date" class="bar-group" :class="{ active: item.index === activeDay }" @mouseenter="activeDay = item.index" @focus="activeDay = item.index" tabindex="0">
                <rect :x="item.x" :y="item.sourceY" :width="item.width" :height="item.sourceHeight" rx="3" class="source-bar" />
                <rect :x="item.x" :y="item.remixY" :width="item.width" :height="item.remixHeight" rx="3" class="remix-bar" />
                <rect :x="item.x - 5" :y="chartTop" :width="item.width + 10" :height="chartInnerHeight" class="hover-target" />
                <text :x="item.pointX" :y="item.labelY" text-anchor="middle" class="bar-total">{{ item.total }}</text>
                <text :x="item.pointX" :y="chartHeight - 14" text-anchor="middle" class="bar-date">{{ formatShortDate(item.date) }}</text>
              </g>
            </svg>
          </div>
        </article>

        <article class="analytics-card mix-card">
          <header><div><span>LIBRARY MIX</span><h2>素材库构成</h2><p>周期内团队上传结构</p></div></header>
          <div class="mix-visual">
            <div class="mix-ring" :style="{ '--source-share': `${sourceShare * 3.6}deg` }"><strong>{{ sourceShare }}%</strong><span>视频素材占比</span></div>
            <div class="mix-values"><div><i class="source"></i><span>视频素材</span><strong>{{ data.summary.source }}</strong></div><div><i class="remix"></i><span>混剪成片</span><strong>{{ data.summary.remix }}</strong></div></div>
          </div>
          <div class="mix-note"><Users :size="18" /><span><strong>{{ data.contributors.length }}</strong> 位同事在本周期产生上传或回流记录</span></div>
        </article>
      </div>

      <div class="analytics-secondary-grid">
        <article class="analytics-card ranking-card">
          <header><div><span>CONTRIBUTOR RANKING</span><h2>同事贡献排行</h2></div><label class="people-search"><Search :size="16" /><input v-model="peopleSearch" placeholder="搜索姓名或工号" /></label></header>
          <div class="metric-tabs"><button v-for="(label, key) in metricLabels" :key="key" :class="{ active: rankingMetric === key }" @click="rankingMetric = key as RankingMetric">{{ label }}</button></div>
          <div v-if="ranking.length" class="ranking-list">
            <div v-for="(person, index) in ranking.slice(0, 12)" :key="person.number || person.name" class="ranking-row">
              <b>{{ String(index + 1).padStart(2, '0') }}</b><span class="person"><strong>{{ person.name }}</strong><small>{{ person.number || '未绑定工号' }}</small></span>
              <span class="ranking-bar"><i :style="{ width: `${person[rankingMetric] / rankingMax * 100}%` }"></i></span><strong class="ranking-value">{{ person[rankingMetric] }}</strong>
            </div>
          </div>
          <div v-else class="analytics-empty">当前条件没有同事记录</div>
        </article>

        <article class="analytics-card heatmap-card">
          <header><div><span>UPLOAD HEATMAP</span><h2>团队上传热力</h2><p>水润蓝越深表示当天上传越集中</p></div></header>
          <div class="heatmap-scroll">
            <div class="heatmap" :style="{ '--days': String(data.daily.length) }">
              <span></span><small v-for="day in data.daily" :key="day.date">{{ formatShortDate(day.date) }}</small>
              <template v-for="person in heatmapPeople" :key="person.number || person.name">
                <strong>{{ person.name }}</strong>
                <i v-for="day in person.daily" :key="`${person.number}-${day.date}`" :style="{ opacity: cellOpacity(day.total) }" :title="`${person.name} · ${day.date} · 上传 ${day.total} 条`"><span>{{ day.total || '' }}</span></i>
              </template>
            </div>
          </div>
        </article>
      </div>

      <article class="analytics-card detail-card">
        <header><div><span>DETAIL LEDGER</span><h2>同事明细</h2><p>同一统计口径下的视频素材、混剪、成交与爆款拆分</p></div></header>
        <nav class="detail-pagination detail-pagination-top" aria-label="同事明细顶部分页">
          <span>第 {{ detailStart }}–{{ detailEnd }} 条，共 {{ ranking.length }} 条</span>
          <div>
            <label>每页<select v-model.number="detailPageSize"><option :value="10">10 条</option><option :value="20">20 条</option><option :value="50">50 条</option></select></label>
            <button :disabled="detailPage <= 1" @click="detailPage--">上一页</button>
            <strong>第 {{ detailPage }} / {{ detailPageCount }} 页</strong>
            <button :disabled="detailPage >= detailPageCount" @click="detailPage++">下一页</button>
          </div>
        </nav>
        <div class="analytics-table-wrap">
          <table>
            <thead><tr><th>同事</th><th>工号</th><th>上传总量</th><th>视频素材</th><th>混剪成片</th><th>有成交素材</th><th>爆款素材</th></tr></thead>
            <tbody><tr v-for="person in detailRows" :key="person.number || person.name"><td><strong>{{ person.name }}</strong></td><td>{{ person.number || '—' }}</td><td>{{ person.total }}</td><td>{{ person.source }}</td><td>{{ person.remix }}</td><td>{{ data.coverage.state === 'available' ? person.transacted : '待回流' }}</td><td>{{ data.coverage.state === 'available' ? person.hits : '待回流' }}</td></tr></tbody>
          </table>
        </div>
        <nav class="detail-pagination" aria-label="同事明细底部分页">
          <span>第 {{ detailStart }}–{{ detailEnd }} 条，共 {{ ranking.length }} 条</span>
          <div><button :disabled="detailPage <= 1" @click="detailPage--">上一页</button><strong>第 {{ detailPage }} / {{ detailPageCount }} 页</strong><button :disabled="detailPage >= detailPageCount" @click="detailPage++">下一页</button></div>
        </nav>
      </article>
    </template>
  </section>
</template>

<style scoped>
.analytics-panel{display:grid;gap:20px;color:#edf4f8;font-family:"Microsoft YaHei","微软雅黑",sans-serif}.analytics-header{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;padding:32px;border:1px solid #253342;border-radius:22px;background:radial-gradient(circle at 84% 14%,rgba(44,196,167,.14),transparent 34%),linear-gradient(145deg,#111923,#0b1119);box-shadow:0 24px 50px rgba(0,0,0,.22)}.analytics-kicker,.analytics-card header span{display:block;color:#3dd3b3;font-size:11px;font-weight:800;letter-spacing:.19em}.analytics-header h1{margin:8px 0 9px;font-size:34px;line-height:1.2}.analytics-header p,.analytics-card header p{margin:0;color:#8fa1ae;font-size:14px;line-height:1.7}.analytics-periods{display:flex;gap:7px;padding:5px;border:1px solid #293844;border-radius:13px;background:#080d13}.analytics-periods button,.metric-tabs button{border:0;border-radius:9px;background:transparent;color:#91a3b0;font-weight:700;padding:10px 14px;cursor:pointer}.analytics-periods button.active,.metric-tabs button.active{background:#25353f;color:#55dfc0;box-shadow:inset 0 0 0 1px rgba(85,223,192,.2)}.analytics-toolbar{display:flex;align-items:center;gap:12px;padding:14px 18px;border:1px solid #22313e;border-radius:16px;background:#0e151e;color:#8fa3b0}.analytics-toolbar label{display:flex;align-items:center;gap:8px;font-weight:700;color:#b7c6cf}.analytics-toolbar input{border:1px solid #2a3a48;border-radius:9px;background:#090f16;color:#eef5f8;padding:8px 10px;font-family:inherit}.analytics-toolbar small{margin-left:auto;color:#758895}.analytics-query,.analytics-error button{display:inline-flex;align-items:center;gap:7px;border:0;border-radius:10px;background:#2bc4a7;color:#04110f;font-weight:800;padding:9px 14px;cursor:pointer}.spinning{animation:spin 1s linear infinite}.analytics-error,.analytics-loading{display:flex;align-items:center;justify-content:center;gap:12px;min-height:180px;border:1px solid #3e3030;border-radius:18px;background:#161317;color:#d7e1e7}.analytics-error span,.analytics-loading span{color:#95a4af}.analytics-kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}.analytics-kpis article{display:flex;align-items:center;gap:13px;min-height:116px;padding:18px;border:1px solid #22313e;border-radius:17px;background:linear-gradient(145deg,#121b25,#0d141c);box-shadow:0 14px 30px rgba(0,0,0,.16)}.analytics-kpi-icon{display:grid;place-items:center;width:46px;height:46px;border-radius:12px}.analytics-kpi-icon svg{width:22px}.analytics-kpi-icon.total{color:#57ddc0;background:rgba(43,196,167,.12)}.analytics-kpi-icon.source{color:#72a8ff;background:rgba(75,123,214,.14)}.analytics-kpi-icon.remix{color:#ffbc63;background:rgba(255,173,65,.13)}.analytics-kpi-icon.gmv{color:#9c8cff;background:rgba(128,101,255,.13)}.analytics-kpi-icon.hits{color:#ff7868;background:rgba(255,95,75,.13)}.analytics-kpis small{display:block;color:#8194a1;font-weight:700}.analytics-kpis strong{display:block;margin:5px 0;font-size:26px}.analytics-kpis em{display:block;color:#667986;font-style:normal;font-size:11px;line-height:1.4}.analytics-coverage{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:12px 16px;border:1px solid rgba(43,196,167,.2);border-radius:13px;background:rgba(43,196,167,.065);color:#b9d9d1}.analytics-coverage i{display:inline-block;width:7px;height:7px;margin-right:9px;border-radius:50%;background:#2bc4a7;box-shadow:0 0 12px #2bc4a7}.analytics-coverage small{color:#77918c}.analytics-coverage.unavailable{border-color:rgba(255,184,86,.23);background:rgba(255,184,86,.07);color:#e4c28d}.analytics-coverage.unavailable i{background:#ffb856;box-shadow:0 0 12px #ffb856}.analytics-main-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,.8fr);gap:16px}.analytics-secondary-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.analytics-card{min-width:0;padding:23px;border:1px solid #22313e;border-radius:19px;background:linear-gradient(150deg,#111a24,#0b121a);box-shadow:0 18px 40px rgba(0,0,0,.16)}.analytics-card>header{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}.analytics-card h2{margin:6px 0 4px;font-size:20px}.active-day{text-align:right}.active-day small,.active-day span{display:block;color:#788d99}.active-day strong{display:block;margin:3px 0;color:#57ddc0;font-size:19px}.chart-legend{display:flex;gap:17px;margin:22px 0 2px;color:#8094a0;font-size:12px}.chart-legend span{display:flex;align-items:center;gap:6px}.chart-legend i{width:8px;height:8px;border-radius:2px}.chart-legend .source,.mix-values .source{background:#4b86e8}.chart-legend .remix,.mix-values .remix{background:#ffae45}.chart-legend .line{width:17px;height:2px;background:#51ddbf}.trend-chart{width:100%;overflow-x:auto}.trend-chart svg{display:block;width:100%;min-width:590px;height:260px;overflow:visible}.grid-line{stroke:#23313d;stroke-width:1}.source-bar{fill:#4b86e8;transition:opacity .2s}.remix-bar{fill:#ffae45;transition:opacity .2s}.hover-target{fill:transparent;cursor:pointer}.bar-group text{fill:#657985;font-size:10px}.bar-group.active .source-bar,.bar-group.active .remix-bar{filter:brightness(1.24)}.trend-line{fill:none;stroke:#51ddbf;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}.trend-point{fill:#091118;stroke:#51ddbf;stroke-width:2;transition:r .2s}.mix-visual{display:grid;place-items:center;gap:22px;padding:26px 0 16px}.mix-ring{--source-share:0deg;display:grid;place-items:center;align-content:center;width:170px;aspect-ratio:1;border-radius:50%;background:radial-gradient(circle at center,#0e1720 0 56%,transparent 57%),conic-gradient(#4b86e8 0 var(--source-share),#ffae45 var(--source-share) 360deg);box-shadow:0 0 38px rgba(75,134,232,.12)}.mix-ring strong{font-size:29px}.mix-ring span{margin-top:4px;color:#8296a2}.mix-values{display:grid;grid-template-columns:1fr 1fr;width:100%;gap:8px}.mix-values div{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:8px;padding:11px;border:1px solid #233341;border-radius:11px;background:#0a1118}.mix-values i{width:8px;height:8px;border-radius:50%}.mix-values span{color:#8ea0ab}.mix-note{display:flex;align-items:center;gap:9px;margin-top:10px;padding:13px;border-top:1px solid #22303b;color:#7f939f}.mix-note strong{color:#eef4f7}.people-search{display:flex;align-items:center;gap:7px;padding:8px 10px;border:1px solid #2b3a47;border-radius:10px;background:#090f16}.people-search input{width:130px;border:0;outline:0;background:transparent;color:#e9f1f5;font-family:inherit}.metric-tabs{display:flex;flex-wrap:wrap;gap:5px;margin:18px 0}.metric-tabs button{padding:8px 10px}.ranking-list{display:grid;gap:9px}.ranking-row{display:grid;grid-template-columns:28px minmax(112px,.7fr) minmax(100px,1.3fr) 42px;align-items:center;gap:10px}.ranking-row>b{color:#586d7a}.ranking-row .person strong,.ranking-row .person small{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ranking-row .person small{margin-top:2px;color:#657986}.ranking-bar{height:7px;border-radius:8px;background:#192630;overflow:hidden}.ranking-bar i{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#367fc8,#42d4b5);}.ranking-value{text-align:right;color:#c9d7df}.heatmap-scroll{margin-top:20px;overflow:auto;padding-bottom:8px}.heatmap{display:grid;grid-template-columns:110px repeat(var(--days),minmax(28px,1fr));gap:5px;min-width:max-content;align-items:center}.heatmap>small{color:#607681;text-align:center;writing-mode:vertical-rl;height:48px}.heatmap>strong{max-width:105px;overflow:hidden;color:#bdcad1;text-overflow:ellipsis;white-space:nowrap}.heatmap>i{display:grid;place-items:center;width:28px;height:28px;border-radius:6px;background:#39c9ad;color:#04110f;font-style:normal;cursor:help}.heatmap>i span{font-size:9px;font-weight:800}.analytics-empty{display:grid;place-items:center;min-height:220px;color:#718591}.detail-card{padding-bottom:14px}.analytics-table-wrap{margin-top:18px;overflow:auto;border:1px solid #22313d;border-radius:13px}.analytics-table-wrap table{width:100%;border-collapse:collapse;min-width:760px}.analytics-table-wrap th,.analytics-table-wrap td{padding:14px 16px;border-bottom:1px solid #1f2c37;text-align:left;color:#9dafb9}.analytics-table-wrap th{background:#0a1118;color:#708591;font-size:12px}.analytics-table-wrap td strong{color:#e2edf1}.analytics-table-wrap tbody tr:hover{background:rgba(43,196,167,.04)}@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:1180px){.analytics-header{align-items:flex-start;flex-direction:column}.analytics-periods{width:100%;overflow:auto}.analytics-toolbar{align-items:stretch;flex-wrap:wrap}.analytics-toolbar label{flex:1 1 220px}.analytics-toolbar small{width:100%;margin-left:0}.analytics-kpis{grid-template-columns:repeat(3,1fr)}.analytics-main-grid,.analytics-secondary-grid{grid-template-columns:1fr}}
@media(max-width:760px){.analytics-header{align-items:flex-start;flex-direction:column;padding:22px}.analytics-header h1{font-size:27px}.analytics-periods{width:100%;overflow:auto}.analytics-toolbar{align-items:stretch;flex-wrap:wrap}.analytics-toolbar label{flex:1 1 180px}.analytics-toolbar small{width:100%;margin-left:0}.analytics-kpis{grid-template-columns:1fr 1fr}.analytics-card{padding:18px}.analytics-card>header{flex-direction:column}.active-day{text-align:left}.analytics-coverage{align-items:flex-start;flex-direction:column}.ranking-row{grid-template-columns:24px minmax(100px,.8fr) minmax(70px,1fr) 34px}}
@media(max-width:480px){.analytics-kpis{grid-template-columns:1fr}.analytics-periods button{white-space:nowrap}.mix-values{grid-template-columns:1fr}}
</style>

<style scoped>
.analytics-panel { color: #fff7f0; }
.analytics-header { border-color: #4a3020; background: radial-gradient(circle at 84% 14%,rgba(255,92,16,.25),transparent 36%),linear-gradient(145deg,#281911,#160e0a); box-shadow: 0 24px 50px rgba(60,18,3,.3); }
.analytics-kicker,.analytics-card header span { color: #ff8d45; }
.analytics-header p,.analytics-card header p { color: #b69f92; }
.analytics-periods { border-color: #4a3021; background: #140d09; }
.analytics-periods button,.metric-tabs button { color: #bba497; }
.analytics-periods button.active,.metric-tabs button.active { color: #fff0e3; background: #4b2614; box-shadow: inset 0 0 0 1px rgba(255,126,52,.32); }
.analytics-toolbar { color: #b9a396; border-color: #432b1d; background: #1d130e; }
.analytics-toolbar label { color: #dbc8bb; }
.analytics-toolbar input,.people-search { color: #fff5ee; border-color: #4a3021; background: #130c09; }
.analytics-query,.analytics-error button { color: #251006; background: linear-gradient(135deg,#ff8b39,#ff5a0a); }
.analytics-kpis article,.analytics-card { border-color: #62422f; background: linear-gradient(145deg,#332219,#251812); box-shadow: 0 16px 34px rgba(57,17,3,.2); }
.analytics-kpi-icon.total,.analytics-kpi-icon.source { color: #ff9a55; background: rgba(255,106,26,.15); }
.analytics-kpi-icon.remix { color: #ffc070; background: rgba(255,164,61,.14); }
.analytics-kpi-icon.gmv { color: #ffd0a0; background: rgba(215,108,36,.15); }
.analytics-kpi-icon.hits { color: #ff7658; background: rgba(255,80,38,.14); }
.analytics-kpis small { color: #d2b9aa; }.analytics-kpis em { color: #a98d7d; }
.analytics-coverage { color: #e3c5b1; border-color: rgba(255,106,26,.3); background: rgba(255,106,26,.085); }
.analytics-coverage i { background: #ff6a1a; box-shadow: 0 0 14px #ff6a1a; }
.active-day strong { color: #ff9a55; }
.chart-legend .source,.mix-values .source { background: #ff8a38; }
.chart-legend .remix,.mix-values .remix { background: #c94b12; }
.chart-legend .line { background: #ffd0a8; }
.grid-line { stroke: #583a2a; }
.source-bar { fill: #ff8a38; }
.remix-bar { fill: #c94b12; }
.trend-line { stroke: #ffd0a8; }
.trend-point { fill: #180e09; stroke: #ffd0a8; }
.mix-ring { background: radial-gradient(circle at center,#20140e 0 56%,transparent 57%),conic-gradient(#ff8a38 0 var(--source-share),#c94b12 var(--source-share) 360deg); box-shadow: 0 0 38px rgba(255,106,26,.16); }
.mix-values div,.analytics-table-wrap th { border-color: #62422f; background: #21150f; }
.mix-note,.analytics-table-wrap,.analytics-table-wrap th,.analytics-table-wrap td { border-color: #5a3b2a; }
.mix-values span,.mix-note,.chart-legend,.active-day small,.active-day span,.ranking-row .person small { color: #bda496; }
.ranking-row>b,.ranking-value { color: #ead8cd; }
.ranking-bar { background: #523425; }
.ranking-bar i { background: linear-gradient(90deg,#d94708,#ff9b48); }
.heatmap>small { color: #c3a999; }.heatmap>strong { color: #f3e4da; }
.heatmap>i { color: #241006; border: 1px solid rgba(255,169,108,.34); background: #ff7a2a; }
.analytics-table-wrap td { color: #d2bdb0; }.analytics-table-wrap th { color: #c6aa99; }
.analytics-table-wrap td strong { color: #fff7f0; }
.analytics-table-wrap tbody tr:nth-child(even) { background: rgba(255,153,91,.035); }
.analytics-table-wrap tbody tr:hover { background: rgba(255,106,26,.09); }
.detail-pagination { display:flex;align-items:center;justify-content:space-between;gap:14px;padding:13px 2px 0;color:#bea697;font-size:13px; }
.detail-pagination-top { margin-top:18px;padding:13px 14px;border:1px solid #62422f;border-radius:12px;background:#2a1b14; }
.detail-pagination>div { display:flex;align-items:center;gap:8px; }
.detail-pagination label { display:flex;align-items:center;gap:7px;white-space:nowrap; }
.detail-pagination select,.detail-pagination button { height:34px;padding:0 11px;border:1px solid #704a34;border-radius:8px;color:#f6e8df;background:#352218;font-family:inherit; }
.detail-pagination button { cursor:pointer;font-weight:700; }.detail-pagination button:hover:not(:disabled) { border-color:#ff8d45;background:#4a2818; }
.detail-pagination button:disabled { opacity:.38;cursor:not-allowed; }.detail-pagination strong { min-width:92px;color:#f3dfd2;text-align:center; }
@media(max-width:760px){.detail-pagination{align-items:stretch;flex-direction:column}.detail-pagination>div{flex-wrap:wrap}.detail-pagination label{width:100%}}
</style>
