<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Download, FolderOpen, LockKeyhole, RefreshCw, Search, UploadCloud } from 'lucide-vue-next'
import { droppedPrivateFiles, privateRequest, uploadPrivateFile, type PrivateAsset, type PrivatePage } from '../utils/privateUpload'

defineProps<{ categories: string[] }>()
const fileInput = ref<HTMLInputElement>()
const folderInput = ref<HTMLInputElement>()
const items = ref<PrivateAsset[]>([])
const folders = ref<string[]>([])
const total = ref(0)
const page = ref(1)
const query = ref('')
const category = ref('')
const folder = ref('')
const uploadCategory = ref('待分类')
const uploadFolder = ref('')
const trash = ref(false)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const busy = ref(false)
const mutationBusy = ref(false)
const pendingAction = ref<{ type: 'share' | 'remove'; asset: PrivateAsset } | null>(null)
const edit = ref<{ id: string; filename: string; category: string; folder_name: string } | null>(null)
type QueueItem = { file: File; path: string; category: string; folder: string; stage: string; progress: number; done: boolean; error: string }
const queue = ref<QueueItem[]>([])
let controller: AbortController | undefined
let readController: AbortController | undefined
let mounted = true
let readSequence = 0
const size = (bytes: number) => `${(bytes / 1024 / 1024).toFixed(1)} MiB`
const date = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false })
const load = async () => {
  const sequence = ++readSequence
  readController?.abort()
  readController = new AbortController()
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({ q: query.value, category: category.value, folder: folder.value, page: String(page.value), trash: String(trash.value) })
    const result = await privateRequest<PrivatePage>(`?${params}`, {}, readController.signal)
    if (sequence !== readSequence || !mounted) return
    items.value = result.items; folders.value = result.folders; total.value = result.total
  } catch (cause) {
    if (sequence === readSequence && mounted) error.value = cause instanceof Error ? cause.message : '私人素材读取失败，请重试'
  } finally { if (sequence === readSequence && mounted) loading.value = false }
}
const filter = () => { page.value = 1; void load() }
const run = async () => {
  if (busy.value) return
  busy.value = true
  controller = new AbortController()
  for (const item of queue.value.filter(item => !item.done)) {
    if (controller.signal.aborted || !mounted) break
    item.error = ''
    try {
      const result = await uploadPrivateFile(item.file, item.category, item.folder, (stage, progress) => { item.stage = stage; item.progress = progress }, controller.signal)
      item.done = true; item.stage = result.reused ? '已存在，复用私人素材' : '已入私人库'; item.progress = 1
    } catch (cause) { item.error = cause instanceof Error ? cause.message : '上传未完成，请续传'; item.stage = controller.signal.aborted ? '已暂停' : '待重试' }
  }
  busy.value = false
  if (mounted) await load()
}
const addFiles = (files: File[]) => {
  error.value = ''
  if (!files.length) return
  if (queue.value.length + files.length > 500) { error.value = '每批最多 500 个文件；请先清理已完成记录'; return }
  for (const file of files) {
    const path = file.webkitRelativePath || file.name
    if (queue.value.some(item => item.path === path && item.file.size === file.size && item.file.lastModified === file.lastModified)) continue
    const nested = file.webkitRelativePath.split('/').slice(0, -1).join('/')
    const targetFolder = [uploadFolder.value.trim(), nested].filter(Boolean).join('/')
    queue.value.push({ file, path, category: uploadCategory.value, folder: targetFolder, stage: '等待上传', progress: 0, done: false, error: '' })
  }
  void run()
}
const drop = async (event: DragEvent) => {
  if (!event.dataTransfer || busy.value) return
  try { addFiles(await droppedPrivateFiles(event.dataTransfer)) }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '目录读取失败' }
}
const picked = (event: Event) => { const input = event.target as HTMLInputElement; addFiles(Array.from(input.files || [])); input.value = '' }
const action = async () => {
  if (!pendingAction.value || mutationBusy.value) return
  mutationBusy.value = true
  error.value = ''
  try {
    const { type, asset } = pendingAction.value
    await privateRequest(`/${asset.id}${type === 'share' ? '/share' : ''}`, {
      method: type === 'share' ? 'POST' : 'DELETE', ...(type === 'share' ? { body: JSON.stringify({ confirmed: true }) } : {}),
    })
    notice.value = type === 'share' ? '已复制到团队素材库，私人原文件保留；未触发推送投放。' : '已移入私人回收区，可恢复。'
    pendingAction.value = null
    await load()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '操作未完成，请重试' }
  finally { mutationBusy.value = false }
}
const restore = async (asset: PrivateAsset) => {
  if (mutationBusy.value) return
  mutationBusy.value = true
  try { await privateRequest(`/${asset.id}/restore`, { method: 'POST' }); await load() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '恢复失败' }
  finally { mutationBusy.value = false }
}
const save = async () => {
  if (!edit.value || mutationBusy.value) return
  mutationBusy.value = true
  try {
    const { id, ...metadata } = edit.value
    await privateRequest(`/${id}`, { method: 'PATCH', body: JSON.stringify(metadata) })
    edit.value = null; await load()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '保存失败' }
  finally { mutationBusy.value = false }
}
onMounted(load)
onBeforeUnmount(() => { mounted = false; controller?.abort(); readController?.abort() })
</script>

<template>
  <section class="private-panel" aria-labelledby="private-title">
    <header class="private-head"><div><h1 id="private-title"><LockKeyhole :size="24" />我的私人素材</h1><p>仅本人可见。不会进入公司 OSS 扫描、团队混剪或投放；主动共享后才创建团队副本。</p></div><button class="secondary-button" :disabled="loading" @click="load"><RefreshCw :size="16" />刷新</button></header>
    <section class="private-upload" aria-label="上传私人素材" @dragover.prevent @drop.prevent="drop">
      <div class="private-fields"><label>上传品类<select v-model="uploadCategory" :disabled="busy"><option>待分类</option><option v-for="name in categories.filter(name => name !== '待分类')" :key="name">{{ name }}</option></select></label><label>存入私人文件夹<input v-model="uploadFolder" :disabled="busy" maxlength="160" list="private-folders" placeholder="例如：喷雾/产品镜（可留空）" /></label></div>
      <div class="private-buttons"><button class="primary-button" :disabled="busy" @click="fileInput?.click()"><UploadCloud :size="16" />上传私人素材</button><button class="secondary-button" :disabled="busy" @click="folderInput?.click()"><FolderOpen :size="16" />上传整个文件夹</button><button v-if="busy" class="secondary-button" @click="controller?.abort()">暂停上传</button><button v-else-if="queue.some(item => !item.done)" class="secondary-button" @click="run">重试 / 续传未完成文件</button></div>
      <input ref="fileInput" type="file" hidden multiple accept=".mp4,.mov,.webm,.mkv,.jpg,.jpeg,.png,.webp" @change="picked" /><input ref="folderInput" type="file" hidden multiple webkitdirectory @change="picked" />
      <p>也可将素材或文件夹拖入这里。每批最多 500 个文件，单文件不超过 2 GiB。刷新或中断后，重新选择同一文件即可续传。</p>
      <details v-if="queue.length" open class="private-queue"><summary>本批 {{ queue.length }} 个文件 · 已入库 {{ queue.filter(item => item.done).length }} 个</summary><ol><li v-for="item in queue" :key="item.path + item.file.lastModified"><strong>{{ item.path }}</strong><span>{{ item.stage }} · {{ Math.round(item.progress * 100) }}%</span><progress :value="item.progress" max="1" :aria-label="`${item.path} ${item.stage}`" /><p v-if="item.error" class="private-error">{{ item.error }}</p></li></ol><button v-if="!busy" class="secondary-button" @click="queue = queue.filter(item => !item.done)">清理已完成上传记录</button></details>
    </section>
    <form class="private-filters" @submit.prevent="filter"><label>搜索素材<input v-model="query" placeholder="输入文件名" /></label><label>品类<select v-model="category" @change="filter"><option value="">全部品类</option><option v-for="name in categories" :key="name">{{ name }}</option></select></label><label>私人文件夹<select v-model="folder" @change="filter"><option value="">全部文件夹</option><option v-for="name in folders" :key="name">{{ name }}</option></select></label><button class="secondary-button" type="submit"><Search :size="16" />搜索</button><label class="private-trash"><input v-model="trash" type="checkbox" @change="filter" />私人回收区</label></form>
    <datalist id="private-folders"><option v-for="name in folders" :key="name" :value="name" /></datalist>
    <p v-if="error" class="private-error" role="alert">{{ error }}</p><p v-if="notice" class="private-notice" role="status">{{ notice }}</p>
    <section v-if="pendingAction" class="private-confirm" aria-label="确认素材操作"><p>{{ pendingAction.type === 'share' ? `确认将“${pendingAction.asset.filename}”完整复制到团队素材库？团队同事将能查看和使用，私人原文件保留，不会自动投放。` : `将“${pendingAction.asset.filename}”移入私人回收区？可以恢复，不会删除原文件。` }}</p><button class="primary-button" :disabled="mutationBusy" @click="action">{{ mutationBusy ? '处理中…' : pendingAction.type === 'share' ? '确认共享到团队' : '确认移入回收区' }}</button><button class="secondary-button" :disabled="mutationBusy" @click="pendingAction = null">取消</button></section>
    <p v-if="loading" role="status">正在读取私人素材…</p>
    <p v-else-if="!items.length" class="private-empty">{{ query || category || folder ? '没有匹配的私人素材，请调整筛选条件。' : trash ? '私人回收区是空的。' : '还没有私人素材。上方上传的视频和图片只有你本人可见。' }}</p>
    <div v-else class="private-media-grid"><article v-for="asset in items" :key="asset.id" class="private-media-item">
      <template v-if="!trash"><video v-if="asset.content_type.startsWith('video/')" :src="asset.media_url" controls preload="none" playsinline /><img v-else :src="asset.media_url" :alt="asset.filename" loading="lazy" /></template>
      <h2>{{ asset.filename }}</h2><p>{{ asset.category }} · {{ size(asset.size) }}</p><p>{{ asset.folder_name || '未分文件夹' }}</p><p>上传于 {{ date(asset.created_at) }}</p>
      <form v-if="edit?.id === asset.id" class="private-edit" @submit.prevent="save"><label>文件名<input v-model="edit.filename" maxlength="512" required /></label><label>品类<select v-model="edit.category"><option v-for="name in [...new Set(['待分类', edit.category, ...categories])]" :key="name">{{ name }}</option></select></label><label>私人文件夹<input v-model="edit.folder_name" maxlength="160" list="private-folders" /></label><button class="primary-button" :disabled="mutationBusy">保存</button><button class="secondary-button" type="button" :disabled="mutationBusy" @click="edit = null">取消</button></form>
      <div v-else class="private-buttons"><template v-if="!trash"><a :href="asset.media_url + '?download=true'" class="secondary-button"><Download :size="15" />下载</a><button class="secondary-button" @click="edit = { id: asset.id, filename: asset.filename, category: asset.category, folder_name: asset.folder_name }">修改分类 / 文件夹</button><button class="secondary-button" :disabled="mutationBusy" @click="pendingAction = { type: 'share', asset }">{{ asset.shared_asset_id ? '查看共享结果 / 重试核对' : '共享到团队' }}</button><button class="secondary-button" :disabled="mutationBusy" @click="pendingAction = { type: 'remove', asset }">移入回收区</button></template><button v-else class="secondary-button" :disabled="mutationBusy" @click="restore(asset)">恢复私人素材</button></div>
    </article></div>
    <footer class="private-pagination"><span>共 {{ total }} 条 · 第 {{ page }} / {{ Math.max(1, Math.ceil(total / 24)) }} 页</span><button class="secondary-button" :disabled="loading || page <= 1" @click="page--; load()">上一页</button><button class="secondary-button" :disabled="loading || page * 24 >= total" @click="page++; load()">下一页</button></footer>
  </section>
</template>

<style scoped>
.private-panel { --private-ink: #17364d; --private-muted: #486477; --private-line: #d7e7ef; --private-blue: #087eaf; color: var(--private-ink); min-width: 0; }
.private-head { display: flex; align-items: start; justify-content: space-between; gap: 24px; margin-bottom: 24px; }.private-head h1 { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; font-size: 26px; }.private-panel p { color: var(--private-muted); line-height: 1.65; overflow-wrap: anywhere; }.private-head p { margin: 0; max-width: 72ch; }
.private-upload { background: #fff; padding: 24px; border: 1px solid var(--private-line); border-radius: 14px; }.private-fields { display: grid; grid-template-columns: minmax(180px, 1fr) minmax(220px, 2fr); gap: 16px; margin-bottom: 16px; }.private-panel label { display: grid; gap: 7px; min-width: 0; font-size: 14px; }.private-panel input:not([type=checkbox]), .private-panel select { min-width: 0; width: 100%; min-height: 42px; border: 1px solid #b7cbd8; padding: 8px 12px; border-radius: 8px; background: #fff; color: var(--private-ink); font: inherit; }.private-panel input::placeholder { color: #516d80; }.private-panel :focus-visible { outline: 2px solid var(--private-blue); outline-offset: 3px; }.private-buttons { display: flex; flex-wrap: wrap; gap: 8px; }.private-panel button, .private-panel a { min-height: 40px; font-size: 14px; }.private-panel button:disabled { opacity: .6; cursor: not-allowed; }.private-panel input[type=checkbox] { accent-color: var(--private-blue); }
.private-queue { margin-top: 18px; }.private-queue summary { cursor: pointer; font-weight: 600; }.private-queue ol { max-height: 300px; overflow-y: auto; scrollbar-color: #a3bdce #f2f7f9; padding-inline-start: 24px; }.private-queue li { padding: 10px 0; border-bottom: 1px solid var(--private-line); }.private-queue strong { display: block; overflow-wrap: anywhere; font-size: 14px; }.private-queue span { font-size: 13px; }.private-queue progress { display: block; width: 100%; height: 8px; margin-top: 6px; accent-color: var(--private-blue); }
.private-filters { display: flex; flex-wrap: wrap; align-items: end; gap: 14px; margin: 28px 0 20px; }.private-filters > label { flex: 1 1 180px; }.private-filters .private-trash { flex: 0 0 auto; display: flex; align-items: center; min-height: 42px; }.private-panel .private-error { color: #a42d24; }.private-notice, .private-confirm { padding: 16px; background: #edf7fc; border: 1px solid var(--private-line); border-radius: 10px; }.private-confirm button { margin: 6px 10px 0 0; }.private-empty { padding: 40px 16px; text-align: center; }.private-media-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 250px), 1fr)); gap: 24px; }.private-media-item { min-width: 0; background: #fff; padding: 16px; border: 1px solid var(--private-line); border-radius: 14px; }.private-media-item video, .private-media-item img { width: 100%; aspect-ratio: 4 / 5; object-fit: contain; background: #f1f5f7; border-radius: 8px; }.private-media-item h2 { font-size: 17px; overflow-wrap: anywhere; margin: 14px 0 8px; }.private-media-item p { font-size: 13px; margin: 4px 0; }.private-media-item .private-buttons { margin-top: 16px; }.private-edit { display: grid; gap: 10px; margin-top: 16px; }.private-pagination { display: flex; flex-wrap: wrap; align-items: center; justify-content: end; gap: 12px; margin-top: 24px; }.private-pagination span { margin-inline-end: auto; font-variant-numeric: tabular-nums; }
@media (max-width: 600px) { .private-head { flex-wrap: wrap; gap: 12px; }.private-upload { padding: 16px; }.private-fields { grid-template-columns: minmax(0, 1fr); }.private-panel input:not([type=checkbox]), .private-panel select { font-size: 16px; }.private-head h1 { font-size: 23px; }.private-filters > label { flex-basis: 100%; } }
</style>
