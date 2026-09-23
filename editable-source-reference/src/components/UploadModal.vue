<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CheckCircle2, Clapperboard, FileImage, FileVideo, Library, Maximize2, Minimize2, Pause, Plus, RefreshCw, Tag, Trash2, UploadCloud, X } from 'lucide-vue-next'
import { api } from '../api'
import { uploadFileMultipart } from '../utils/multipartUpload'
import { CLEANSER_PRODUCT_CATEGORIES } from '../utils/productCategories'
import type { Asset, MultipartUploadSession, OaUser } from '../types'

type UploadState = 'pending' | 'hashing' | 'creating' | 'uploading' | 'paused' | 'cataloging' | 'success' | 'failed' | 'cancelled'
type UploadItem = {
  id: string
  file: File
  state: UploadState
  progress: number
  error: string
  uploadSession?: MultipartUploadSession
  uploaded: boolean
  asset?: Asset
  reported: boolean
  stageMessage: string
  transferLoaded: number
  transferTotal: number
  speedBps: number
  etaSeconds: number | null
  retries: number
  referenceUrl: string
  referenceFile?: File
  referenceSession?: MultipartUploadSession
  referenceUploaded: boolean
  materialDescription: string
  performanceScreenshots: File[]
  performanceScreenshotUrls: string[]
  performanceScreenshotEntries: { object_key: string; filename: string }[]
  abortController?: AbortController
  activeSession?: MultipartUploadSession
}

const props = withDefaults(defineProps<{
  user: OaUser
  categories: string[]
  folders?: string[]
  defaultLibraryType?: 'source' | 'remix'
  mode?: 'material' | 'product-image'
}>(), { folders: () => [], defaultLibraryType: 'source', mode: 'material' })
const emit = defineEmits<{ close: []; uploaded: [assets: Asset[], completed: boolean] }>()

const files = ref<UploadItem[]>([])
const productMode = computed(() => props.mode === 'product-image')
const category = ref(productMode.value ? (props.categories[0] || '待分类') : '待分类')
const contentType = ref(productMode.value ? '自动识别' : '其他')
const libraryType = ref<'source' | 'remix'>(props.defaultLibraryType)
const sourceSubtypes = ['明星信息流原片', '达人/KOL原片', '达人/KOC原片', '实拍自产素材', '产品镜', 'AI原创素材', '品牌创意广告', '品牌IP广告', '其他视频素材']
const remixSubtypes = ['商品卖点混剪', '明星素材混剪', '达人素材混剪', 'AI混剪成片', '其他混剪成片']
const assetSubtype = ref(libraryType.value === 'source' ? 'AI原创素材' : remixSubtypes[0])
const folderName = ref('')
const running = ref(false)
const error = ref('')
const input = ref<HTMLInputElement | null>(null)
const customTags = ref<string[]>([])
const newTag = ref('')
const minimized = ref(false)

const normalizedCategories = computed(() => props.categories.map(item => item === '燕窝胜肽面膜' ? '燕窝面膜' : item === '清洁泥膜' ? '其他 WIS 素材' : item))
const categoryOptions = computed(() => Array.from(new Set(['待分类', '通用', '颈膜', '黄金面膜', '美白针', '燕窝面膜', ...normalizedCategories.value, ...CLEANSER_PRODUCT_CATEGORIES])))
const subtypeOptions = computed(() => libraryType.value === 'source' ? sourceSubtypes : remixSubtypes)
const busy = computed(() => running.value || files.value.some(item => ['hashing', 'creating', 'uploading', 'cataloging'].includes(item.state)))
const failedCount = computed(() => files.value.filter(item => item.state === 'failed').length)
const successCount = computed(() => files.value.filter(item => item.state === 'success').length)
const pendingCount = computed(() => files.value.filter(item => item.state === 'pending').length)
const resumableCount = computed(() => files.value.filter(item => ['pending', 'paused', 'failed'].includes(item.state)).length)
const overallProgress = computed(() => {
  if (!files.value.length) return 0
  const total = files.value.reduce((sum, item) => sum + (item.state === 'success' ? 100 : item.progress), 0)
  return Math.round(total / files.value.length)
})
const activeItem = computed(() => files.value.find(item => ['hashing', 'creating', 'uploading', 'cataloging'].includes(item.state)))
const batchSpeedBps = computed(() => files.value.reduce((sum, item) => sum + (item.state === 'uploading' ? item.speedBps : 0), 0))
const batchRemainingBytes = computed(() => files.value.reduce((sum, item) => {
  if (['success', 'cancelled', 'cataloging'].includes(item.state)) return sum
  if (['hashing', 'creating', 'uploading', 'paused'].includes(item.state) && item.transferTotal > 0) {
    return sum + Math.max(0, item.transferTotal - item.transferLoaded)
  }
  return sum + (item.uploaded ? 0 : item.file.size)
}, 0))
const batchEtaSeconds = computed(() => batchSpeedBps.value >= 1024 ? Math.ceil(batchRemainingBytes.value / batchSpeedBps.value) : null)

watch(libraryType, () => {
  assetSubtype.value = libraryType.value === 'source' ? 'AI原创素材' : subtypeOptions.value[0]
  if (libraryType.value !== 'source') folderName.value = ''
})

const addCustomTag = () => {
  const incoming = newTag.value.split(/[,，;；\n]+/).map(item => item.trim()).filter(Boolean)
  customTags.value = Array.from(new Set([...customTags.value, ...incoming])).slice(0, 29)
  newTag.value = ''
}

const sizeLabel = (file: File) => file.size > 1024 ** 3
  ? `${(file.size / 1024 ** 3).toFixed(2)} GB`
  : `${(file.size / 1024 ** 2).toFixed(1)} MB`

const speedLabel = (bytesPerSecond: number) => bytesPerSecond >= 1024 ** 2
  ? `${(bytesPerSecond / 1024 ** 2).toFixed(1)} MB/s`
  : bytesPerSecond >= 1024 ? `${Math.round(bytesPerSecond / 1024)} KB/s` : ''

const etaLabel = (seconds: number | null) => {
  if (seconds === null || !Number.isFinite(seconds)) return ''
  if (seconds < 60) return `${Math.max(1, Math.ceil(seconds))} 秒`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${Math.ceil(seconds % 60)} 秒`
  return `${Math.floor(seconds / 3600)} 小时 ${Math.ceil(seconds % 3600 / 60)} 分`
}

const transferLabel = (item: UploadItem) => {
  const details = []
  if (item.state === 'uploading' && item.speedBps >= 1024) details.push(speedLabel(item.speedBps))
  if (item.state === 'uploading' && item.etaSeconds !== null) details.push(`剩余 ${etaLabel(item.etaSeconds)}`)
  if (item.retries) details.push(`已自动重连 ${item.retries} 次`)
  return details.length ? ` · ${details.join(' · ')}` : ''
}

const uploadPlanFor = (queueLength: number) => {
  const cores = Math.max(2, Number(navigator.hardwareConcurrency || 4))
  const maxFileConcurrency = cores >= 8 ? 4 : cores >= 4 ? 3 : 2
  const fileConcurrency = Math.max(1, Math.min(queueLength, maxFileConcurrency))
  return { fileConcurrency, partConcurrency: Math.max(2, Math.ceil(8 / fileConcurrency)) }
}

const stateLabel = (item: UploadItem) => item.stageMessage || ({
  pending: '等待上传',
  hashing: `校验文件 ${item.progress}%`,
  creating: '创建安全通道',
  uploading: `上传中 ${item.progress}%`,
  paused: `已暂停，可从 ${item.progress}% 继续`,
  cataloging: '写入素材目录',
  success: '上传完成',
  failed: '上传失败',
  cancelled: '已取消，未完成的分片已中止',
}[item.state])

const choose = (list: FileList | null) => {
  if (!list?.length || busy.value) return
  error.value = ''
  const existing = new Set(files.value.map(item => item.id))
  const incoming = Array.from(list)
  const accepted: UploadItem[] = []
  for (const file of incoming) {
    const id = `${file.name}-${file.size}-${file.lastModified}`
    if (existing.has(id)) continue
    if (files.value.length + accepted.length >= 10) {
      error.value = '一次最多上传 10 条素材，超出的文件未加入队列。'
      break
    }
    if (file.size <= 0 || file.size > 5 * 1024 ** 3) {
      error.value = `${file.name} 超过 5GB 或文件为空，未加入队列。`
      continue
    }
    if (productMode.value && !file.type.startsWith('image/')) {
      error.value = `${file.name} 不是图片，未加入产品图片资产库。`
      continue
    }
    accepted.push({
      id, file, state: 'pending', progress: 0, error: '', uploaded: false, reported: false,
      stageMessage: '', referenceUrl: '', referenceUploaded: false, materialDescription: '',
      transferLoaded: 0, transferTotal: file.size, speedBps: 0, etaSeconds: null, retries: 0,
      performanceScreenshots: [], performanceScreenshotUrls: [], performanceScreenshotEntries: [],
    })
    existing.add(id)
  }
  files.value = [...files.value, ...accepted]
  if (input.value) input.value.value = ''
}

const removeItem = (item: UploadItem) => {
  if (['hashing', 'creating', 'uploading', 'cataloging'].includes(item.state)) return
  item.performanceScreenshotUrls.forEach(url => URL.revokeObjectURL(url))
  files.value = files.value.filter(current => current.id !== item.id)
  error.value = ''
}

const chooseReference = (item: UploadItem, list: FileList | null) => {
  const file = list?.[0]
  if (!file || busy.value) return
  if (!file.type.startsWith('video/') || file.size <= 0 || file.size > 5 * 1024 ** 3) {
    item.error = '参考视频仅支持不超过 5GB 的视频文件'
    return
  }
  item.referenceFile = file
  item.referenceSession = undefined
  item.referenceUploaded = false
  item.error = ''
}

const clearReference = (item: UploadItem) => {
  if (busy.value) return
  item.referenceFile = undefined
  item.referenceSession = undefined
  item.referenceUploaded = false
}

const choosePerformanceScreenshots = (item: UploadItem, list: FileList | null) => {
  if (busy.value) return
  const accepted: File[] = []
  for (const file of Array.from(list || [])) {
    if (!file.type.startsWith('image/') || file.size <= 0 || file.size > 20 * 1024 ** 2) {
      item.error = `${file.name} 不是可用图片或超过 20MB`
      continue
    }
    if (item.performanceScreenshots.length + accepted.length >= 9) {
      item.error = '每条素材最多上传 9 张数据截图'
      break
    }
    accepted.push(file)
  }
  item.performanceScreenshots = [...item.performanceScreenshots, ...accepted]
  item.performanceScreenshotUrls = [
    ...item.performanceScreenshotUrls,
    ...accepted.map(file => URL.createObjectURL(file)),
  ]
}

const removePerformanceScreenshot = (item: UploadItem, index: number) => {
  if (busy.value) return
  URL.revokeObjectURL(item.performanceScreenshotUrls[index])
  item.performanceScreenshots = item.performanceScreenshots.filter((_, current) => current !== index)
  item.performanceScreenshotUrls = item.performanceScreenshotUrls.filter((_, current) => current !== index)
  item.performanceScreenshotEntries = []
}

const pauseItem = (item: UploadItem) => {
  if (!['hashing', 'creating', 'uploading'].includes(item.state)) return
  item.state = 'paused'
  item.stageMessage = `已暂停，可从 ${item.progress}% 继续`
  item.speedBps = 0
  item.etaSeconds = null
  item.abortController?.abort()
}

const cancelItem = async (item: UploadItem) => {
  if (item.state === 'success' || item.state === 'cancelled' || item.state === 'cataloging') return
  const sessionId = item.activeSession?.status === 'active' ? item.activeSession.session_id : ''
  item.state = 'cancelled'
  item.progress = 0
  item.error = ''
  item.stageMessage = '已取消，未完成的分片已中止'
  item.speedBps = 0
  item.etaSeconds = null
  item.abortController?.abort()
  if (sessionId) {
    try { await api.cancelMultipartUpload(sessionId) }
    catch { item.error = '已停止本地上传，服务器分片将按超时策略自动清理' }
  }
  item.activeSession = undefined
}

const cancelRemaining = async () => {
  await Promise.all(files.value
    .filter(item => !['success', 'cancelled', 'cataloging'].includes(item.state))
    .map(item => cancelItem(item)))
}

const uploadOne = async (item: UploadItem, partConcurrency: number) => {
  item.error = ''
  item.speedBps = 0
  item.etaSeconds = null
  item.retries = 0
  item.abortController = new AbortController()
  try {
    if (!item.uploaded) {
      item.state = 'hashing'
      item.stageMessage = '正在校验原文件'
      item.transferLoaded = 0
      item.transferTotal = item.file.size
      item.uploadSession = await uploadFileMultipart(item.file, {
        assetScope: productMode.value ? 'product_image' : 'marketing_video',
        category: category.value,
      }, {
        onStage: message => { if (item.state !== 'cancelled') { item.stageMessage = message; item.state = message.includes('校验') ? 'hashing' : 'uploading' } },
        onProgress: progress => { if (item.state !== 'cancelled') item.progress = progress },
        onSession: session => { item.activeSession = session },
        onTransfer: stats => {
          if (item.state === 'cancelled') return
          item.transferLoaded = stats.loadedBytes
          item.transferTotal = stats.totalBytes
          item.speedBps = stats.speedBps
          item.etaSeconds = stats.etaSeconds
          item.retries = stats.retries
        },
      }, item.abortController.signal, { partConcurrency })
      item.uploaded = true
    }
    if (!item.uploadSession) throw new Error('安全上传会话已失效，请重试')
    if (item.referenceFile && !item.referenceUploaded) {
      item.state = 'hashing'
      item.stageMessage = '正在校验参考视频'
      item.progress = 0
      item.transferLoaded = 0
      item.transferTotal = item.referenceFile.size
      item.referenceSession = await uploadFileMultipart(item.referenceFile, {
        assetScope: 'reference_video',
        category: '参考视频',
      }, {
        onStage: message => { if (item.state !== 'cancelled') { item.stageMessage = `参考视频：${message}`; item.state = message.includes('校验') ? 'hashing' : 'uploading' } },
        onProgress: progress => { if (item.state !== 'cancelled') item.progress = progress },
        onSession: session => { item.activeSession = session },
        onTransfer: stats => {
          if (item.state === 'cancelled') return
          item.transferLoaded = stats.loadedBytes
          item.transferTotal = stats.totalBytes
          item.speedBps = stats.speedBps
          item.etaSeconds = stats.etaSeconds
          item.retries = stats.retries
        },
      }, item.abortController.signal, { partConcurrency })
      item.referenceUploaded = true
    }
    if (!productMode.value && libraryType.value === 'source' && assetSubtype.value === 'AI原创素材') {
      for (let index = item.performanceScreenshotEntries.length; index < item.performanceScreenshots.length; index += 1) {
        const screenshot = item.performanceScreenshots[index]
        item.state = 'hashing'
        item.stageMessage = `正在上传数据截图 ${index + 1}/${item.performanceScreenshots.length}`
        item.progress = 0
        item.transferLoaded = 0
        item.transferTotal = screenshot.size
        const session = await uploadFileMultipart(screenshot, {
          assetScope: 'reference_video',
          category: '数据截图',
        }, {
          onStage: message => { if (item.state !== 'cancelled') { item.stageMessage = `数据截图 ${index + 1}/${item.performanceScreenshots.length}：${message}`; item.state = message.includes('校验') ? 'hashing' : 'uploading' } },
          onProgress: progress => { if (item.state !== 'cancelled') item.progress = progress },
          onSession: activeSession => { item.activeSession = activeSession },
          onTransfer: stats => {
            if (item.state === 'cancelled') return
            item.transferLoaded = stats.loadedBytes
            item.transferTotal = stats.totalBytes
            item.speedBps = stats.speedBps
            item.etaSeconds = stats.etaSeconds
            item.retries = stats.retries
          },
        }, item.abortController.signal, { partConcurrency })
        item.performanceScreenshotEntries.push({ object_key: session.object_key, filename: screenshot.name })
      }
    }
    item.state = 'cataloging'
    item.stageMessage = '正在写入素材目录'
    item.asset = await api.completeUpload({
      object_key: item.uploadSession.object_key,
      filename: item.file.name,
      category: category.value,
      content_type: contentType.value,
      asset_scope: productMode.value ? 'product_image' : 'marketing_video',
      library_type: productMode.value ? 'source' : libraryType.value,
      asset_subtype: productMode.value ? '产品图片' : assetSubtype.value,
      folder_name: !productMode.value && libraryType.value === 'source' ? folderName.value.trim() : '',
      tags: Array.from(new Set([...customTags.value, productMode.value ? '产品图片' : '上传素材'])).slice(0, 30),
      reference_url: productMode.value ? '' : item.referenceUrl.trim(),
      reference_video_key: productMode.value ? '' : item.referenceSession?.object_key || '',
      reference_video_name: productMode.value ? '' : item.referenceFile?.name || '',
      material_description: !productMode.value && assetSubtype.value === 'AI原创素材' ? item.materialDescription.trim() : '',
      performance_screenshots: !productMode.value && assetSubtype.value === 'AI原创素材' ? item.performanceScreenshotEntries : [],
    })
    item.state = 'success'
    item.stageMessage = '上传完成'
    item.speedBps = 0
    item.etaSeconds = 0
  } catch (cause) {
    if (item.state === 'paused' || item.state === 'cancelled' || (cause instanceof DOMException && cause.name === 'AbortError')) return
    item.state = 'failed'
    item.stageMessage = ''
    item.speedBps = 0
    item.etaSeconds = null
    item.error = cause instanceof Error ? cause.message : '上传失败，请重试'
  } finally {
    item.abortController = undefined
  }
}

const submit = async () => {
  if (busy.value || !files.value.length) return
  const queue = files.value.filter(item => ['pending', 'paused', 'failed'].includes(item.state))
  if (!queue.length) return
  queue.forEach(item => { item.state = 'pending'; item.error = ''; item.stageMessage = '' })
  const uploadPlan = uploadPlanFor(queue.length)
  running.value = true
  error.value = ''
  let cursor = 0
  const worker = async () => {
    while (cursor < queue.length) {
      const item = queue[cursor++]
      if (item.state === 'cancelled') continue
      await uploadOne(item, uploadPlan.partConcurrency)
    }
  }
  try {
    // Keep all eight direct-to-OSS lanes busy while distributing them fairly
    // across several files. Lower-core devices automatically use fewer files.
    await Promise.all(Array.from({ length: uploadPlan.fileConcurrency }, worker))
    const newlyUploaded = files.value.filter(item => item.state === 'success' && item.asset && !item.reported)
    newlyUploaded.forEach(item => { item.reported = true })
    const completed = files.value.every(item => item.state === 'success')
    if (newlyUploaded.length) emit('uploaded', newlyUploaded.map(item => item.asset!), completed)
    if (!completed && failedCount.value) error.value = `${failedCount.value} 条上传失败，已成功的素材不会重复上传；可单独重试失败项。`
  } finally {
    running.value = false
  }
}

onBeforeUnmount(() => {
  files.value.forEach(item => item.performanceScreenshotUrls.forEach(url => URL.revokeObjectURL(url)))
})
</script>

<template>
  <aside v-if="minimized" class="upload-background-tray" aria-live="polite">
    <header><span><UploadCloud :size="18" /><strong>{{ productMode ? '产品图片上传' : '素材后台上传' }}</strong></span><button v-if="busy" type="button" title="取消剩余上传" @click="cancelRemaining"><X :size="16" /></button><button type="button" title="展开上传窗口" @click="minimized = false"><Maximize2 :size="16" /></button><button v-if="!busy" type="button" title="关闭" @click="emit('close')"><X :size="16" /></button></header>
    <p>{{ activeItem ? activeItem.file.name : failedCount ? `${failedCount} 条上传失败，可展开重试` : '本批上传已完成' }}</p>
    <div class="upload-background-progress"><i :style="{ width: `${overallProgress}%` }"></i></div>
    <footer><span>{{ successCount }}/{{ files.length }} 条<span v-if="batchSpeedBps"> · {{ speedLabel(batchSpeedBps) }}</span></span><strong>{{ overallProgress }}%</strong></footer>
    <small v-if="busy && batchEtaSeconds !== null">预计剩余 {{ etaLabel(batchEtaSeconds) }}</small>
    <small v-if="busy">可以继续浏览平台；请保持当前页面打开</small>
  </aside>
  <div v-else class="upload-layer" @mousedown.self="!busy && emit('close')">
    <section class="upload-modal batch-upload-modal" role="dialog" aria-modal="true" :aria-label="productMode ? '批量上传产品图片' : '批量上传素材'">
      <header>
        <div><span>{{ productMode ? 'PRODUCT IMAGE LIBRARY' : 'BATCH UPLOAD' }}</span><h2>{{ productMode ? '上传产品图片' : '批量上传素材' }}</h2><p>{{ productMode ? '选择产品分类后，一次最多上传 10 张原图；保留原尺寸和透明通道。' : '先选择素材归属，再一次上传最多 10 条；原视频不压缩、不转码。' }}</p></div>
        <div class="upload-window-actions"><button v-if="busy" type="button" aria-label="缩小并在后台上传" title="缩小并在后台上传" @click="minimized = true"><Minimize2 :size="18" /></button><button aria-label="关闭上传窗口" :disabled="busy" @click="emit('close')"><X :size="18" /></button></div>
      </header>

      <div v-if="!productMode" class="upload-library-choice" aria-label="选择素材库">
        <button :class="{ active: libraryType === 'source' }" :disabled="busy" @click="libraryType = 'source'"><Library :size="19" /><span><strong>视频素材</strong><small>一创原片与源素材</small></span></button>
        <button :class="{ active: libraryType === 'remix' }" :disabled="busy" @click="libraryType = 'remix'"><Clapperboard :size="19" /><span><strong>混剪成片</strong><small>已完成剪辑的成片</small></span></button>
      </div>

      <button type="button" class="upload-drop batch-drop" :class="{ selected: files.length }" :disabled="busy || files.length >= 10" @click="input?.click()" @dragover.prevent @drop.prevent="choose($event.dataTransfer?.files || null)">
        <input ref="input" type="file" multiple :accept="productMode ? 'image/*' : 'video/*,image/*'" @change="choose(($event.target as HTMLInputElement).files)" />
        <span class="upload-file-icon"><UploadCloud /></span>
        <strong>{{ files.length ? `已选择 ${files.length}/10 ${productMode ? '张图片' : '条素材'}` : `点击选择或拖入最多 10 ${productMode ? '张产品图片' : '条素材'}` }}</strong>
        <small>{{ productMode ? '支持 JPG、PNG、WEBP、GIF；原图直传，不压缩' : '支持 MP4、MOV、M4V、WEBM、JPG、PNG、WEBP；单条最大 5GB' }}</small>
      </button>

      <div v-if="files.length" class="upload-queue">
        <article v-for="item in files" :key="item.id" :class="`state-${item.state}`">
          <span class="upload-queue-icon"><FileVideo v-if="item.file.type.startsWith('video/')" :size="18" /><FileImage v-else :size="18" /></span>
          <div class="upload-queue-main"><strong :title="item.file.name">{{ item.file.name }}</strong><span>{{ sizeLabel(item.file) }} · {{ stateLabel(item) }}{{ transferLabel(item) }}</span><div v-if="['hashing', 'uploading', 'paused'].includes(item.state)" class="upload-progress"><i :style="{ width: `${item.progress}%` }"></i></div><small v-if="item.error">{{ item.error }}</small>
            <div v-if="!productMode" class="upload-reference-fields">
              <label><span>竞对/参考信息（选填）</span><input v-model="item.referenceUrl" type="text" :disabled="busy" placeholder="可粘贴任意链接或文字说明" /></label>
              <label class="upload-reference-file"><span>参考视频（选填）</span><input type="file" accept="video/*" :disabled="busy" @change="chooseReference(item, ($event.target as HTMLInputElement).files)" /><b>{{ item.referenceFile ? item.referenceFile.name : '选择参考视频' }}</b></label>
              <button v-if="item.referenceFile" type="button" :disabled="busy" @click="clearReference(item)"><X :size="13" />清除参考视频</button>
            </div>
            <section v-if="!productMode && libraryType === 'source' && assetSubtype === 'AI原创素材'" class="upload-ai-evidence">
              <label><span>素材说明（选填）</span><textarea v-model="item.materialDescription" rows="3" maxlength="10000" :disabled="busy" placeholder="说明这条素材的开头、卖点、表现形式和适合的投放场景，便于后续 AI 学习。" /></label>
              <label class="upload-evidence-picker"><span>效果/数据截图（选填，最多 9 张）</span><input type="file" accept="image/*" multiple :disabled="busy" @change="choosePerformanceScreenshots(item, ($event.target as HTMLInputElement).files)" /><b><FileImage :size="15" />{{ item.performanceScreenshots.length ? `已选 ${item.performanceScreenshots.length} 张` : '选择截图' }}</b><small>支持 JPG、PNG、WEBP，单张不超过 20MB</small></label>
              <div v-if="item.performanceScreenshots.length" class="upload-evidence-grid"><figure v-for="(screenshot, index) in item.performanceScreenshots" :key="`${screenshot.name}-${screenshot.lastModified}`"><img :src="item.performanceScreenshotUrls[index]" :alt="screenshot.name" /><figcaption>{{ screenshot.name }}</figcaption><button type="button" aria-label="移除数据截图" :disabled="busy" @click="removePerformanceScreenshot(item, index)"><X :size="12" /></button></figure></div>
            </section>
          </div>
          <CheckCircle2 v-if="item.state === 'success'" class="upload-success-icon" :size="19" />
          <span v-else-if="['hashing', 'creating', 'uploading'].includes(item.state)" class="upload-active-actions"><button title="暂停并保留已上传分片" @click="pauseItem(item)"><Pause :size="16" /></button><button class="upload-cancel-item" title="取消这条上传并中止分片" @click="cancelItem(item)"><X :size="16" /></button></span>
          <button v-else-if="['pending', 'paused', 'failed'].includes(item.state)" class="upload-cancel-item" title="取消这条上传" @click="cancelItem(item)"><X :size="16" /></button>
          <button v-else-if="item.state !== 'cataloging'" title="移除" @click="removeItem(item)"><Trash2 :size="16" /></button>
          <RefreshCw v-else class="spin" :size="17" />
        </article>
      </div>

      <div class="upload-fields">
        <label>上传人<input :value="user.realName" disabled /></label>
        <label v-if="!productMode">素材分类<select v-model="assetSubtype" :disabled="busy"><option v-for="item in subtypeOptions" :key="item">{{ item }}</option></select></label>
        <label v-if="!productMode && libraryType === 'source'">二级文件夹<input v-model="folderName" list="upload-folder-options" maxlength="160" :disabled="busy" placeholder="选填，如 KOC原片-林凡清" /><datalist id="upload-folder-options"><option v-for="folder in folders" :key="folder" :value="folder" /></datalist></label>
        <label>产品分类<select v-model="category" :disabled="busy"><option v-for="item in categoryOptions" :key="item">{{ item }}</option></select></label>
        <label>{{ productMode ? '图片类型' : '内容类型' }}<select v-model="contentType" :disabled="busy"><option v-for="item in (productMode ? ['自动识别', '主视觉/KV', '带投影', '无投影/透明底', '仰视', '俯视', '平视/正面', '开盒/内容物', '替换装', '组合装', '其他产品图'] : ['其他', '上脸展示', '数字人', '图文', '产品展示', '痛点', '测评', '口播', '剧情', '科普'])" :key="item">{{ item }}</option></select></label>
      </div>

      <div class="upload-custom-tags">
        <label><Tag :size="15" />自定义标签 <small>本批素材共用，上传后仍可在素材详情中修改</small></label>
        <div v-if="customTags.length" class="upload-tag-list"><button v-for="tag in customTags" :key="tag" type="button" :disabled="busy" @click="customTags = customTags.filter(item => item !== tag)">{{ tag }}<X :size="12" /></button></div>
        <div class="upload-tag-input"><input v-model="newTag" :disabled="busy || customTags.length >= 29" placeholder="输入标签，支持逗号分隔" @keyup.enter.prevent="addCustomTag" /><button type="button" :disabled="busy || !newTag.trim()" @click="addCustomTag"><Plus :size="15" />添加</button></div>
      </div>

      <p v-if="busy" class="upload-stage"><i></i><span>智能并发上传中：成功 {{ successCount }} 条，待处理 {{ resumableCount }} 条<span v-if="batchSpeedBps"> · {{ speedLabel(batchSpeedBps) }}</span><span v-if="batchEtaSeconds !== null"> · 预计剩余 {{ etaLabel(batchEtaSeconds) }}</span>；网络波动会自动续传</span></p>
      <p v-if="error" class="upload-error">{{ error }}</p>

      <footer>
        <span v-if="files.length" class="upload-summary">成功 {{ successCount }} · 失败 {{ failedCount }} · 共 {{ files.length }}</span>
        <button v-if="busy" class="secondary-button upload-cancel-remaining" @click="cancelRemaining"><X :size="15" />取消剩余上传</button><button v-if="busy" class="secondary-button" @click="minimized = true"><Minimize2 :size="15" />后台上传</button><button v-else class="secondary-button" @click="emit('close')">取消</button>
        <button class="primary-button" :disabled="!files.length || busy || !resumableCount" @click="submit"><RefreshCw v-if="failedCount || files.some(item => item.state === 'paused')" :size="16" /><UploadCloud v-else :size="16" />{{ busy ? '上传中…' : files.some(item => item.state === 'paused') ? `继续上传 ${resumableCount} 条` : failedCount && !pendingCount ? `重试失败的 ${failedCount} 条` : `开始上传 ${resumableCount} 条` }}</button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.upload-ai-evidence { display:grid; grid-template-columns:minmax(0,1fr) minmax(210px,.55fr); gap:10px; margin-top:10px; padding:12px; border:1px solid #d6e8f1; border-radius:13px; background:#f7fbfd; }
.upload-ai-evidence label { display:grid; gap:6px; min-width:0; color:#4b6c82; font-size:12px; font-weight:800; }
.upload-ai-evidence textarea { width:100%; min-height:74px; box-sizing:border-box; resize:vertical; border:1px solid #cbdde7; border-radius:10px; padding:9px 11px; color:#153b59; background:#fff; font:inherit; font-weight:500; }
.upload-evidence-picker { position:relative; align-content:start; }
.upload-evidence-picker input { position:absolute; inset:24px 0 auto; height:45px; opacity:0; cursor:pointer; }
.upload-evidence-picker b { display:flex; align-items:center; gap:6px; min-height:42px; padding:0 11px; border:1px dashed #83bdd9; border-radius:10px; color:#087dbb; background:#eef8fc; }
.upload-evidence-picker small { color:#7891a2; font-weight:500; }
.upload-evidence-grid { grid-column:1/-1; display:grid; grid-template-columns:repeat(auto-fill,minmax(84px,1fr)); gap:8px; }
.upload-evidence-grid figure { position:relative; min-width:0; margin:0; overflow:hidden; border:1px solid #d6e6ee; border-radius:10px; background:#fff; }
.upload-evidence-grid img { display:block; width:100%; aspect-ratio:4/3; object-fit:cover; }
.upload-evidence-grid figcaption { overflow:hidden; padding:5px 7px; color:#567086; font-size:10px; text-overflow:ellipsis; white-space:nowrap; }
.upload-evidence-grid button { position:absolute; top:4px; right:4px; display:grid; place-items:center; width:22px; height:22px; border:0; border-radius:7px; color:#fff; background:rgba(14,45,64,.74); }
.upload-cancel-item, .upload-cancel-remaining { color:#b24444 !important; border-color:#e7bcbc !important; background:#fff7f7 !important; }
.upload-active-actions { display:flex; gap:5px; }
@media (max-width:760px) { .upload-ai-evidence { grid-template-columns:1fr; } .upload-evidence-grid { grid-column:auto; } }
</style>
