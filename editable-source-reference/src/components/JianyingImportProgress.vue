<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Check, Clapperboard, Download, LoaderCircle, RefreshCw, TriangleAlert, X } from 'lucide-vue-next'
import { api } from '../api'
import type { JianyingImportTask } from '../types'

const props = defineProps<{ task: JianyingImportTask }>()
const emit = defineEmits<{ close: []; retry: [assetId: number] }>()
const current = ref<JianyingImportTask>({ ...props.task })
const pollError = ref('')
const waitingSeconds = ref(0)
let timer: ReturnType<typeof setTimeout> | null = null
let clock: ReturnType<typeof setInterval> | null = null

const terminal = computed(() => ['completed', 'failed', 'expired'].includes(current.value.status))
const failed = computed(() => ['failed', 'expired'].includes(current.value.status))
const stageTitle = computed(() => ({
  waiting: '等待桌面助手接收',
  claimed: '桌面助手已接收',
  downloading: '正在下载原视频',
  downloaded: '原视频下载完成',
  opening: '正在打开剪映',
  importing: '正在加入剪映时间轴',
  completed: '已加入当前时间轴',
  failed: '导入没有完成',
  expired: '本次导入已过期',
}[current.value.status] || '正在处理'))
const helperMayNeedUpdate = computed(() => (
  (current.value.status === 'waiting' && waitingSeconds.value >= 5)
  || (!terminal.value && current.value.status !== 'waiting' && !current.value.helper_version && waitingSeconds.value >= 3)
))
const formatBytes = (value: number) => {
  if (!value) return '0 MB'
  const mb = value / 1024 / 1024
  if (mb < 1024) return `${mb.toFixed(mb >= 100 ? 0 : 1)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}
const formatSpeed = (value: number) => value > 0 ? `${formatBytes(value)}/s` : '正在测速'
const formatEta = (value: number) => {
  if (!value) return '即将完成'
  if (value < 60) return `约 ${value} 秒`
  return `约 ${Math.ceil(value / 60)} 分钟`
}
const liveDownloadText = computed(() => {
  if (current.value.cache_hit) return '已命中本机缓存，无需重新下载'
  if (current.value.status !== 'downloading') return ''
  return `${formatBytes(current.value.downloaded_bytes)} / ${formatBytes(current.value.total_bytes)} · ${formatSpeed(current.value.speed_bps)} · 剩余 ${formatEta(current.value.eta_seconds)}`
})

const stopPolling = () => {
  if (timer) clearTimeout(timer)
  timer = null
}

const poll = async () => {
  stopPolling()
  try {
    current.value = await api.jianyingImportStatus(current.value.ticket_id)
    pollError.value = ''
  } catch (cause) {
    pollError.value = cause instanceof Error ? cause.message : '暂时无法读取导入进度'
  }
  if (!terminal.value) timer = setTimeout(poll, 700)
}

watch(() => props.task.ticket_id, () => {
  current.value = { ...props.task }
  waitingSeconds.value = 0
  pollError.value = ''
  void poll()
})

onMounted(() => {
  void poll()
  clock = setInterval(() => { waitingSeconds.value += 1 }, 1000)
})
onBeforeUnmount(() => {
  stopPolling()
  if (clock) clearInterval(clock)
})
</script>

<template>
  <div class="jy-progress-backdrop" @click.self="emit('close')">
    <section class="jy-progress-card" role="dialog" aria-modal="true" aria-label="剪映导入进度">
      <button class="jy-progress-close" aria-label="关闭" @click="emit('close')"><X :size="22" /></button>
      <div class="jy-progress-icon" :class="{ success: current.status === 'completed', danger: failed }">
        <Check v-if="current.status === 'completed'" :size="30" />
        <TriangleAlert v-else-if="failed" :size="30" />
        <Clapperboard v-else :size="30" />
      </div>
      <span class="jy-progress-kicker">Jianying original import</span>
      <h2>{{ stageTitle }}</h2>
      <p class="jy-progress-file">{{ current.filename }}</p>

      <div class="jy-progress-track"><span :style="{ width: `${Math.max(2, current.progress)}%` }"></span></div>
      <div class="jy-progress-meta"><strong>{{ current.progress }}%</strong><span>{{ current.message }}</span></div>
      <div v-if="liveDownloadText" class="jy-live-download">
        <span class="jy-live-dot"></span><strong>{{ liveDownloadText }}</strong>
      </div>

      <div class="jy-progress-steps">
        <div :class="{ active: current.progress >= 5 }"><Download :size="18" /><span>接收任务</span></div>
        <div :class="{ active: current.progress >= 8 }"><LoaderCircle :size="18" /><span>原片下载</span></div>
        <div :class="{ active: current.progress >= 80 }"><Clapperboard :size="18" /><span>唤起剪映</span></div>
        <div :class="{ active: current.progress >= 100 }"><Check :size="18" /><span>进入时间轴</span></div>
      </div>

      <div v-if="helperMayNeedUpdate" class="jy-progress-warning">
        桌面助手暂未响应，当前可能仍是旧版本。请安装最新版后再次导入。
      </div>
      <div v-if="pollError" class="jy-progress-warning">{{ pollError }}</div>

      <div class="jy-progress-actions">
        <a v-if="helperMayNeedUpdate || failed" class="jy-helper-download" href="downloads/WIS-Jianying-Helper-v1.3.12.exe" download>安装最新版 v1.3.12</a>
        <button v-if="failed" class="jy-retry-button" @click="emit('retry', current.asset_id)"><RefreshCw :size="16" />重新导入</button>
        <button v-if="current.status === 'completed'" class="jy-done-button" @click="emit('close')">完成</button>
        <button v-else class="jy-later-button" @click="emit('close')">后台继续</button>
      </div>
      <p class="jy-progress-note">全程使用原视频，不压缩、不转码；只有原片导入并加入当前时间轴后才会显示 100%。</p>
    </section>
  </div>
</template>

<style scoped>
.jy-progress-backdrop{position:fixed;inset:0;z-index:1200;display:grid;place-items:center;padding:24px;background:rgba(10,35,54,.55);backdrop-filter:blur(10px)}
.jy-progress-card{position:relative;width:min(620px,100%);padding:38px;border:1px solid #cfe5ef;border-radius:28px;background:#fff;box-shadow:0 28px 80px rgba(20,72,96,.24);color:#14354d}
.jy-progress-close{position:absolute;right:22px;top:22px;display:grid;place-items:center;width:42px;height:42px;border:1px solid #d6e7ef;border-radius:14px;background:#fff;color:#547183;cursor:pointer}
.jy-progress-icon{display:grid;place-items:center;width:62px;height:62px;margin-bottom:18px;border-radius:20px;background:#e8f7ff;color:#188dca}.jy-progress-icon.success{background:#e9f8ef;color:#21955a}.jy-progress-icon.danger{background:#fff0ed;color:#d95f4d}
.jy-progress-kicker{font-size: 14px;font-weight:800;letter-spacing:.2em;text-transform:uppercase;color:#2298d0}.jy-progress-card h2{margin:8px 0 5px;font-size:28px}.jy-progress-file{margin:0 0 26px;overflow:hidden;color:#6d8797;text-overflow:ellipsis;white-space:nowrap}
.jy-progress-track{height:12px;overflow:hidden;border-radius:999px;background:#e8f1f5}.jy-progress-track span{display:block;height:100%;border-radius:inherit;background:linear-gradient(90deg,#28a9e4,#39d09b)}
.jy-progress-meta{display:flex;justify-content:space-between;gap:16px;margin-top:10px;color:#658091;font-size: 15px}.jy-progress-meta strong{font-size: 17px;color:#1b5979}.jy-progress-meta span{text-align:right}
.jy-live-download{display:flex;align-items:center;gap:9px;margin-top:14px;padding:12px 14px;border:1px solid #cce9dd;border-radius:13px;background:#effaf5;color:#216b50;font-size: 15px}.jy-live-dot{width:8px;height:8px;border-radius:999px;background:#22b47a;box-shadow:0 0 0 5px rgba(34,180,122,.12);animation:jy-pulse 1.1s ease-in-out infinite}.jy-live-download strong{font-variant-numeric:tabular-nums}@keyframes jy-pulse{50%{opacity:.35;transform:scale(.75)}}
.jy-progress-steps{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:28px 0}.jy-progress-steps div{display:flex;flex-direction:column;align-items:center;gap:7px;padding:14px 6px;border-radius:14px;background:#f5f8fa;color:#9aaab3;font-size: 15px}.jy-progress-steps div.active{background:#edf9f5;color:#18865c;font-weight:700}
.jy-progress-warning{margin:12px 0;padding:13px 15px;border:1px solid #f4d59a;border-radius:12px;background:#fff8e9;color:#8a631c;font-size: 15px;line-height:1.5}.jy-progress-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}.jy-progress-actions button,.jy-helper-download{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:42px;padding:0 18px;border-radius:12px;font-weight:700;text-decoration:none;cursor:pointer}.jy-helper-download,.jy-later-button{border:1px solid #cfe2eb;background:#fff;color:#365f74}.jy-retry-button,.jy-done-button{border:0;background:#178fca;color:#fff}.jy-progress-note{margin:18px 0 0;color:#8ba0ac;font-size: 15px;line-height:1.55}
@media (max-width:640px){.jy-progress-card{padding:30px 22px}.jy-progress-steps{grid-template-columns:repeat(2,1fr)}.jy-progress-actions{flex-wrap:wrap}.jy-progress-actions>*{flex:1}.jy-progress-meta{display:block}.jy-progress-meta span{display:block;margin-top:4px;text-align:left}}
</style>
