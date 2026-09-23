<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Activity, ArrowLeft, ArrowRight, BadgeCheck, BarChart3, Bot, Boxes, Camera, Clapperboard, ClipboardList, Cloud, Film, FolderSync, Grid2X2, Heart, Image, LayoutDashboard, Library, ListFilter, Menu, RefreshCw, Search, Send, Settings, ShieldCheck, SlidersHorizontal, Sparkles, Trash2, UploadCloud, UserCog, UserRound, Users, Video } from 'lucide-vue-next'
import AssetCard from './components/AssetCard.vue'
import PrivateAssetPanel from './components/PrivateAssetPanel.vue'
import AssetDrawer from './components/AssetDrawer.vue'
import { linkedAssetRequest } from './linked-asset'
import LoginPage from './components/LoginPage.vue'
import UploadModal from './components/UploadModal.vue'
import QianchuanPanel from './components/QianchuanPanel.vue'
import QianchuanPushModal from './components/QianchuanPushModal.vue'
import AdqPanel from './components/AdqPanel.vue'
import AdqPushModal from './components/AdqPushModal.vue'
import ChannelsPanel from './components/ChannelsPanel.vue'
import ChannelsPushModal from './components/ChannelsPushModal.vue'
import ConfirmModal from './components/ConfirmModal.vue'
import AccessAdminPanel from './components/AccessAdminPanel.vue'
import PersonalSalesPanel from './components/PersonalSalesPanel.vue'
import UploadAnalyticsPanel from './components/UploadAnalyticsPanel.vue'
import AdminPermissionsPanel from './components/AdminPermissionsPanel.vue'
import OperationLogPanel from './components/OperationLogPanel.vue'
import JianyingBridgePanel from './components/JianyingBridgePanel.vue'
import JianyingImportProgress from './components/JianyingImportProgress.vue'
import NotificationCenter from './components/NotificationCenter.vue'
import VideoRequestPanel from './components/VideoRequestPanel.vue'
import ReviewCenterPanel from './components/ReviewCenterPanel.vue'
import ReviewBatchSubmitModal from './components/ReviewBatchSubmitModal.vue'
import BatchOrganizeModal from './components/BatchOrganizeModal.vue'
import TrashClearModal from './components/TrashClearModal.vue'
import PaginationControls from './components/PaginationControls.vue'
import { api, ApiError } from './api'
import type { Asset, EffectiveClipLibraryStatus, Facets, JianyingImportTask, OaPermissions, OaUser, ProductImageCategory, ReviewWorkflowConfig, Stats, SyncStatus } from './types'

type View = 'hall' | 'private' | 'favorites' | 'hits' | 'effective' | 'requests' | 'review-submit' | 'reviews' | 'review-config' | 'analytics' | 'sales' | 'categories' | 'tags' | 'oss' | 'qianchuan' | 'adq' | 'channels' | 'trash' | 'access' | 'admins' | 'logs'
type LibraryType = 'source' | 'remix'
type HallScope = 'all' | LibraryType
type HallStage = 'overview' | 'categories' | 'results'
type LibraryFilterState = { search: string; category: string; contentType: string; mediaType: '' | 'video' | 'image'; status: string; sort: string; directory: string; ingestSource: string; assetSubtype: string; folderName: string; onlyMine: boolean }
type ConfirmAction = { kind: 'trash' | 'purge'; asset: Asset } | { kind: 'batchTrash'; assetIds: number[] } | null

const assets = ref<Asset[]>([])
const user = ref<OaUser | null>(null)
const permissions = ref<OaPermissions>({ asset_admin: false, asset_delete_manager: false, super_admin: false, operation_admin: false, manage_permissions: false, video_request_assigner: false, video_request_supervisor_viewer: false, review_config_admin: false, can_mark_effective: false, reviewer_roles: [] })
const checking = ref(true)
const bootError = ref('')
const bootNeedsLogin = ref(false)
const hubPrefix = (import.meta.env.BASE_URL || '').match(/^\/(yxb|fd-026222)\/wis-marketing-hub\/modules\/cloud-manager\//)?.[1] || ''
const integratedIntoHub = Boolean(hubPrefix)
const returnToHub = () => {
  const hubUrl = hubPrefix ? `/${hubPrefix}/wis-marketing-hub/` : '/fd-026222/wis-marketing-hub/'
  try { window.top?.location.replace(hubUrl) } catch { window.location.replace(hubUrl) }
}
let bootInFlight = false
const stats = ref<Stats>({ total: 0, oss_total: 0, videos: 0, images: 0, source_materials: 0, remix_outputs: 0, product_images: 0, favorites: 0, trash: 0, new_this_week: 0, storage_bytes: 0, indexed_records: 0, hit_materials: 0, effective_materials: 0, source: '连接中' })
const facets = ref<Facets>({ categories: [], content_types: [], statuses: [], ingest_sources: [], library_types: [], asset_subtypes: [], tags: [], directories: [], folders: [], source: '连接中' })
const sourceFacets = ref<Facets>({ categories: [], content_types: [], statuses: [], ingest_sources: [], library_types: [], asset_subtypes: [], tags: [], directories: [], folders: [], source: '连接中' })
const remixFacets = ref<Facets>({ categories: [], content_types: [], statuses: [], ingest_sources: [], library_types: [], asset_subtypes: [], tags: [], directories: [], folders: [], source: '连接中' })
const productCategories = ref<ProductImageCategory[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 60
const loading = ref(true)
const error = ref('')
const assetRefreshWarning = ref('')
const reviewPendingCount = ref(0)
const reviewRejectedCount = ref(0)
const reviewAiAttentionCount = ref(0)
const reviewSubmitAttentionCount = computed(() => reviewRejectedCount.value + reviewAiAttentionCount.value)
let reviewPendingTimer: number | undefined
const selected = ref<Asset | null>(null)
const linkedAsset = ref<{id: number; etag: string} | null>(null)
const linkedAssetError = ref('')
const openLinkedAsset = async () => {
  linkedAssetError.value = ''
  try {
    linkedAsset.value = linkedAssetRequest(window.location.search)
    if (!linkedAsset.value) return
    const asset = await api.asset(linkedAsset.value.id)
    if (asset.id !== linkedAsset.value.id) throw Error('返回素材与交付编号不一致')
    selected.value = asset
  } catch (cause) {
    linkedAssetError.value = `无法打开原交付素材：${cause instanceof Error ? cause.message : '读取失败'}。请核对原任务，不会用同名素材替代。`
  }
}
const saving = ref(false)
const syncing = ref(false)
const syncInfo = ref<SyncStatus | null>(null)
const uploadOpen = ref(false)
const productUploadOpen = ref(false)
const jianyingOpen = ref(false)
const jianyingImportTask = ref<JianyingImportTask | null>(null)
const qianchuanAssets = ref<Asset[]>([])
const adqAssets = ref<Asset[]>([])
const channelsAssets = ref<Asset[]>([])
const pushOrigin = ref<'single' | 'batch'>('single')
const toast = ref('')
const confirmAction = ref<ConfirmAction>(null)
const actionBusy = ref(false)
const effectiveBusyIds = ref<number[]>([])
const clipLibraryBusyIds = ref<number[]>([])
const clipLibraryStates = ref<Record<number, EffectiveClipLibraryStatus>>({})
const batchMode = ref(false)
const batchPurpose = ref<'delete' | 'push' | 'review' | 'tags' | 'folder'>('delete')
const selectedAssetIds = ref<number[]>([])
const batchToolbarElement = ref<HTMLElement | null>(null)
const batchToolbarPassed = ref(false)
let batchToolbarObserver: IntersectionObserver | undefined
const organizeOpen = ref(false)
const organizeBusy = ref(false)
const reviewBatchOpen = ref(false)
const reviewBatchConfig = ref<ReviewWorkflowConfig | null>(null)
const trashClearOpen = ref(false)
const mobileNav = ref(false)
const view = ref<View>('hall')
const requestFocusId = ref('')
const search = ref('')
const category = ref('')
const contentType = ref('')
const mediaType = ref<'' | 'video' | 'image'>('')
const status = ref('')
const sort = ref('newest')
const onlyFavorites = ref(false)
const directory = ref('')
const ingestSource = ref('')
const onlyMine = ref(false)
const uploadStartDate = ref('')
const uploadEndDate = ref('')
const libraryType = ref<LibraryType>('source')
const hallScope = ref<HallScope>('all')
const hallStage = ref<HallStage>('overview')
const assetSubtype = ref('')
const folderName = ref('')
const sourceSubtypes = ['明星信息流原片', '达人/KOL原片', '达人/KOC原片', '实拍自产素材', '产品镜', 'AI原创素材', '品牌创意广告', '品牌IP广告', '其他视频素材']
const remixSubtypes = ['商品卖点混剪', '明星素材混剪', '达人素材混剪', 'AI混剪成片', '其他混剪成片']
const libraryFilters = ref<Record<LibraryType, LibraryFilterState>>({
  source: { search: '', category: '', contentType: '', mediaType: '', status: '', sort: 'newest', directory: '', ingestSource: '', assetSubtype: '', folderName: '', onlyMine: false },
  remix: { search: '', category: '', contentType: '', mediaType: '', status: '', sort: 'newest', directory: '', ingestSource: '', assetSubtype: '', folderName: '', onlyMine: false },
})
const countFormatter = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 })
const formatCount = (value: number) => countFormatter.format(Number.isFinite(value) ? value : 0)
const librarySubtypes = computed(() => libraryType.value === 'source' ? sourceSubtypes : remixSubtypes)
const libraryTitle = computed(() => libraryType.value === 'source' ? '视频素材' : '混剪成片')
const librarySubtitle = computed(() => libraryType.value === 'source'
  ? '集中储存明星、KOL、KOC、自产自拍、产品镜、AI 一创与品牌广告素材'
  : '集中储存已完成剪辑、可供复用与投放的混剪成片')
const productCategoryOptions = computed(() => productCategories.value.map(item => item.name))
const sourceCategoryCards = computed(() => [
  { label: '明星', value: '明星信息流原片', description: '明星信息流原片与授权片段', icon: UserRound },
  { label: 'KOL', value: '达人/KOL原片', description: '达人与专业内容创作者原片', icon: Users },
  { label: 'KOC', value: '达人/KOC原片', description: '真实用户与素人口碑原片', icon: UserRound },
  { label: '自产自拍', value: '实拍自产素材', description: '团队自产、门店与生活化实拍', icon: Camera },
  { label: '产品镜', value: '产品镜', description: '产品展示、质地与使用过程等原始镜头', icon: Film },
  { label: 'AI 一创', value: 'AI原创素材', description: 'AI 原创画面、人物与创意片段', icon: Bot },
  { label: '品牌创意广告', value: '品牌创意广告', description: '围绕品牌主张与创意概念制作的广告', icon: Sparkles },
  { label: '品牌IP广告', value: '品牌IP广告', description: '品牌人物、栏目与长期 IP 内容广告', icon: BadgeCheck },
].map(item => ({
  ...item,
  count: sourceFacets.value.asset_subtypes.find(facet => facet.name === item.value)?.count || 0,
})))
const remixProductCards = computed(() => remixFacets.value.categories
  .filter(item => item.name && item.name !== '待分类')
  .sort((left, right) => right.count - left.count))
const hallResultTitle = computed(() => {
  if (mediaType.value === 'image') return '全部图片素材'
  if (mediaType.value === 'video') return '全部视频素材'
  if (hallScope.value === 'all') return '整体素材大厅'
  if (hallScope.value === 'source') return assetSubtype.value || '全部视频素材'
  return category.value || '全部混剪成片'
})
const hallResultSubtitle = computed(() => mediaType.value === 'image'
  ? '团队共享的全部图片素材，点击卡片即可预览、收藏或下载原图'
  : mediaType.value === 'video'
    ? '团队共享的全部视频素材，点击卡片即可播放和查看详情'
    : hallScope.value === 'all'
      ? '团队共享的全部视频素材、图片素材与混剪成片'
  : hallScope.value === 'source'
    ? '一创原片与源素材，可继续按类型、产品和标签筛选'
    : '已完成剪辑的成片，可继续按产品、状态和成交表现筛选')
const hallResultKicker = computed(() => hallScope.value === 'all' ? 'ALL ASSETS' : hallScope.value === 'source' ? 'SOURCE LIBRARY' : 'REMIX LIBRARY')

const viewInfo = computed(() => ({
  hall: { title: '素材大厅', subtitle: '浏览、检索和整理全部 WIS 素材' },
  private: { title: '我的私人素材', subtitle: '仅本人可见的素材、上传与文件夹' },
  favorites: { title: '我的收藏', subtitle: '集中查看已收藏的素材' },
  hits: { title: '爆款素材', subtitle: '历史累计千川回流成交额严格超过 5 万元的素材' },
  effective: { title: '有效一创素材', subtitle: '由同事人工确认、可继续复用和验证效果的视频素材' },
  requests: { title: '视频中心提需', subtitle: '提交参考内容，跟踪分配、制作、验收与数据回流' },
  'review-submit': { title: '提交视频审核', subtitle: '搜索自己上传的视频并发起审核流程' },
  reviews: { title: '视频审核中心', subtitle: 'AI给出风险与放宽建议，最终由指定审核人决定是否通过' },
  'review-config': { title: '审核角色配置', subtitle: '配置组员和各级审核人，并调整上线必审节点' },
  analytics: { title: '数据统计', subtitle: '查看团队每日上传产能、素材结构、成交覆盖与爆款数量' },
  sales: { title: '个人成交数据', subtitle: '查看千川与视频号的个人GMV、素材数和当月排名' },
  categories: { title: '产品图片', subtitle: '按 WIS 产品线集中管理可复用的高清产品原图' },
  tags: { title: '标签管理', subtitle: '按已保存标签快速定位素材' },
  oss: { title: 'OSS 素材目录', subtitle: '查看真实 yxb/ 存储目录和同步状态' },
  qianchuan: { title: '千川推送', subtitle: '推送素材到千川账户与计划，并回流素材投放数据' },
  adq: { title: '腾讯 ADQ', subtitle: '原视频推送到腾讯 ADQ 营销单元，并回流素材级投放数据' },
  channels: { title: '视频号主页推送', subtitle: '扫码授权个人视频号，并将原视频直接发布到主页' },
  trash: { title: '回收站', subtitle: '可恢复误删素材；满 7 天后由每周清理任务永久删除 OSS 原文件' },
  access: { title: '登录权限', subtitle: '管理非品牌营销部同事的个人登录白名单' },
  admins: { title: '管理员权限', subtitle: '授予或取消操作日志及全员数据回流查看权限' },
  logs: { title: '操作日志', subtitle: '查看同事们在素材与推送模块中的关键操作' },
})[view.value])
const activeFilters = computed(() => [category.value, contentType.value, mediaType.value, status.value, directory.value, ingestSource.value, assetSubtype.value, folderName.value, uploadStartDate.value || uploadEndDate.value].filter(Boolean).length + (onlyFavorites.value ? 1 : 0) + (onlyMine.value ? 1 : 0))
const dataView = computed(() => view.value === 'sales' || view.value === 'analytics')
const workspaceView = computed(() => dataView.value || ['private', 'requests', 'review-submit', 'reviews', 'review-config'].includes(view.value))
const libraryVisible = computed(() => ['hall', 'favorites', 'hits', 'effective', 'categories', 'trash'].includes(view.value))
const manageableVisibleAssets = computed(() => assets.value.filter(asset => asset.can_manage))
const deletableVisibleAssets = computed(() => assets.value.filter(asset => asset.can_delete))
const batchSelectableVisibleAssets = computed(() => {
  if (batchPurpose.value === 'push') return assets.value.filter(asset => asset.media_type === 'video')
  if (batchPurpose.value === 'review') return assets.value.filter(asset => asset.media_type === 'video')
  if (batchPurpose.value === 'folder') return manageableVisibleAssets.value.filter(asset => asset.asset_scope === 'marketing_video' && asset.library_type === 'source')
  if (batchPurpose.value === 'delete') return deletableVisibleAssets.value
  return manageableVisibleAssets.value
})
const selectAllCandidates = computed(() => batchPurpose.value === 'push'
  ? batchSelectableVisibleAssets.value.slice(0, 10)
  : batchSelectableVisibleAssets.value)
const allBatchCandidatesSelected = computed(() => selectAllCandidates.value.length > 0 && selectAllCandidates.value.every(asset => selectedAssetIds.value.includes(asset.id)))
const batchSelectAllLabel = computed(() => {
  if (allBatchCandidatesSelected.value) return '取消全选'
  if (batchPurpose.value === 'push' && batchSelectableVisibleAssets.value.length > 10) return '选择当前前 10 条视频'
  return `全选当前页 ${selectAllCandidates.value.length} 条`
})
watch(batchToolbarElement, (element, previousElement) => {
  if (previousElement) batchToolbarObserver?.unobserve(previousElement)
  batchToolbarPassed.value = false
  if (element) batchToolbarObserver?.observe(element)
}, { flush: 'post' })
const formatStorage = computed(() => stats.value.storage_bytes > 1024 ** 3
  ? `${(stats.value.storage_bytes / 1024 ** 3).toFixed(1)} GB`
  : stats.value.storage_bytes > 0 ? `${(stats.value.storage_bytes / 1024 ** 2).toFixed(0)} MB` : '大小待读取')
const sourceDate = computed(() => {
  const value = facets.value.source_updated_at || stats.value.source_updated_at
  if (!value) return '来源时间待读取'
  return `索引更新 ${new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value.replace(' ', 'T') + (value.includes('T') ? '' : 'Z')))}`
})
const hitCount = computed(() => stats.value.hit_materials || 0)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const pageRangeLabel = computed(() => {
  if (!total.value) return '0 条'
  const start = (page.value - 1) * pageSize + 1
  return `第 ${start}–${Math.min(total.value, page.value * pageSize)} 条，共 ${formatCount(total.value)} 条`
})
const emptyCopy = computed(() => {
  if (view.value === 'favorites') return { title: '还没有收藏素材', text: '在素材卡片或详情页点击收藏后，会出现在这里。' }
  if (view.value === 'hits') return { title: '暂无成交额超过 5 万的素材', text: '这里只采用已回读、已核验的千川日级成交额；暂无数据不会按 0 或人工标签处理。' }
  if (view.value === 'effective') return { title: '还没有有效一创素材', text: '在任意视频卡片下点击“标记为有效素材”，它会保留原分类并同步出现在这里。' }
  if (view.value === 'categories') return { title: '该分类暂无素材', text: '可选择其它产品分类或清除筛选条件。' }
  if (view.value === 'tags') return { title: '该标签暂无素材', text: '可选择其它标签，或在素材详情里保存新标签。' }
  if (view.value === 'trash') return { title: '回收站是空的', text: '被删除的素材会先在这里保留 7 天，期间可以随时恢复。' }
  return { title: '没有匹配的素材', text: '换个关键词或清除筛选条件试试。' }
})

let timer: ReturnType<typeof setTimeout>
let assetLoadSequence = 0
const loadAssets = async () => {
  const loadSequence = ++assetLoadSequence
  if (workspaceView.value) {
    assets.value = []
    total.value = 0
    loading.value = false
    error.value = ''
    return
  }
  if (view.value === 'hall' && hallStage.value !== 'results') {
    assets.value = []
    total.value = 0
    loading.value = false
    error.value = ''
    return
  }
  loading.value = true
  error.value = ''
  assetRefreshWarning.value = ''
  try {
    const params = new URLSearchParams({
      sort: view.value === 'trash' ? (['name', 'size'].includes(sort.value) ? sort.value : 'deleted') : sort.value,
      page: String(page.value),
      page_size: String(pageSize),
    })
    if (search.value) params.set('q', search.value)
    if (view.value !== 'trash') {
      if (view.value === 'hall' && hallScope.value !== 'all') params.set('library_type', hallScope.value)
      if (view.value === 'hall' && mediaType.value !== 'image') params.set('asset_scope', 'marketing_video')
      if (view.value === 'hits') params.set('hot_only', 'true')
      if (view.value === 'effective') {
        params.set('effective_only', 'true')
        params.set('asset_scope', 'marketing_video')
        params.set('media_type', 'video')
      }
      if (view.value === 'categories') {
        params.set('asset_scope', 'product_image')
        params.set('media_type', 'image')
      }
      if (category.value) params.set('category', category.value)
      if (contentType.value) params.set('content_type', contentType.value)
      if (mediaType.value) params.set('media_type', mediaType.value)
      if (assetSubtype.value) params.set('asset_subtype', assetSubtype.value)
      if (folderName.value) params.set('folder_name', folderName.value)
      if (status.value) params.set('status', status.value)
      if (directory.value) params.set('directory', directory.value)
      if (ingestSource.value) params.set('ingest_source', ingestSource.value)
      if (onlyMine.value) params.set('mine_only', 'true')
      if (onlyFavorites.value) params.set('favorite', 'true')
      if (uploadStartDate.value) params.set('upload_start_date', uploadStartDate.value)
      if (uploadEndDate.value) params.set('upload_end_date', uploadEndDate.value)
    }
    const data = view.value === 'trash' ? await api.trash(params) : await api.assets(params)
    if (loadSequence !== assetLoadSequence) return
    assets.value = data.items
    total.value = data.total
    const availablePages = Math.max(1, Math.ceil(data.total / pageSize))
    if (page.value > availablePages) page.value = availablePages
  } catch (e) {
    if (loadSequence !== assetLoadSequence) return
    const message = e instanceof Error ? e.message : '加载失败'
    if (assets.value.length) {
      assetRefreshWarning.value = `本次刷新未完成，当前继续显示上一次成功读取的素材。${message}`
    } else {
      error.value = message
    }
  } finally {
    if (loadSequence === assetLoadSequence) loading.value = false
  }
}

watch([search, category, contentType, mediaType, status, sort, onlyFavorites, onlyMine, directory, ingestSource, assetSubtype, folderName, uploadStartDate, uploadEndDate, libraryType, hallScope, hallStage, view], () => {
  if (view.value === 'hall') {
    libraryFilters.value[libraryType.value] = {
      search: search.value,
      category: category.value,
      contentType: contentType.value,
      mediaType: mediaType.value,
      status: status.value,
      sort: sort.value,
      directory: directory.value,
      ingestSource: ingestSource.value,
      assetSubtype: assetSubtype.value,
      folderName: folderName.value,
      onlyMine: onlyMine.value,
    }
  }
  if (workspaceView.value) return
  page.value = 1
  clearTimeout(timer)
  timer = setTimeout(loadAssets, 180)
})

watch(page, () => {
  if (workspaceView.value) return
  clearTimeout(timer)
  timer = setTimeout(loadAssets, 80)
})

const changeAssetPage = (nextPage: number) => {
  page.value = Math.min(Math.max(1, nextPage), totalPages.value)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}

const switchLibrary = (next: LibraryType) => {
  if (next === libraryType.value) return
  const current = libraryFilters.value[libraryType.value]
  Object.assign(current, {
    search: search.value, category: category.value, contentType: contentType.value, mediaType: mediaType.value, status: status.value,
    sort: sort.value, directory: directory.value, ingestSource: ingestSource.value, assetSubtype: assetSubtype.value, folderName: folderName.value, onlyMine: onlyMine.value,
  })
  libraryType.value = next
  const saved = libraryFilters.value[next]
  search.value = saved.search
  category.value = saved.category
  contentType.value = saved.contentType
  mediaType.value = saved.mediaType
  status.value = saved.status
  sort.value = saved.sort
  directory.value = saved.directory
  ingestSource.value = saved.ingestSource
  assetSubtype.value = saved.assetSubtype
  folderName.value = saved.folderName
  onlyMine.value = saved.onlyMine
  batchMode.value = false
  selectedAssetIds.value = []
}

const resetHallFilters = () => {
  search.value = ''
  category.value = ''
  contentType.value = ''
  mediaType.value = ''
  status.value = ''
  directory.value = ''
  ingestSource.value = ''
  assetSubtype.value = ''
  folderName.value = ''
  onlyMine.value = false
  onlyFavorites.value = false
  sort.value = 'newest'
  batchMode.value = false
  selectedAssetIds.value = []
}

const openHallOverview = () => {
  resetHallFilters()
  hallScope.value = 'all'
  hallStage.value = 'overview'
}

const openHallOverall = () => {
  resetHallFilters()
  hallScope.value = 'all'
  hallStage.value = 'results'
}

const filterHallByMedia = (next: '' | 'video' | 'image') => {
  resetHallFilters()
  hallScope.value = 'all'
  hallStage.value = 'results'
  mediaType.value = next
}

const openMyUploads = () => {
  resetHallFilters()
  hallScope.value = 'all'
  hallStage.value = 'results'
  onlyMine.value = true
}

const openHallCategories = (scope: LibraryType) => {
  resetHallFilters()
  hallScope.value = scope
  libraryType.value = scope
  hallStage.value = 'categories'
}

const openSourceCategory = (value: string) => {
  resetHallFilters()
  hallScope.value = 'source'
  libraryType.value = 'source'
  assetSubtype.value = value
  hallStage.value = 'results'
}

const openRemixProduct = (value: string) => {
  resetHallFilters()
  hallScope.value = 'remix'
  libraryType.value = 'remix'
  category.value = value
  hallStage.value = 'results'
}

const openAllInScope = (scope: LibraryType) => {
  resetHallFilters()
  hallScope.value = scope
  libraryType.value = scope
  hallStage.value = 'results'
}

const loadCatalogMetadata = async () => {
  const results = await Promise.allSettled([
      api.stats(),
      view.value === 'categories' ? api.facets('', 'product_image') : api.facetBundle(),
      api.productImageCategories(),
  ])
  if (results[0].status === 'fulfilled') stats.value = results[0].value
  if (results[1].status === 'fulfilled') {
    if ('all' in results[1].value) {
      facets.value = results[1].value.all
      sourceFacets.value = results[1].value.source
      remixFacets.value = results[1].value.remix
    } else {
      facets.value = results[1].value
    }
  }
  if (results[2].status === 'fulfilled') productCategories.value = results[2].value.items
}

const loadAll = async () => {
  // The usable asset list is the critical path. Counts and filter metadata can
  // fill in immediately afterwards without keeping colleagues on the boot page.
  const metadataRequest = loadCatalogMetadata()
  await loadAssets()
  void metadataRequest
}

const loadReviewPendingCount = async () => {
  if (!user.value) return
  try {
    const data = await api.reviewPendingCount()
    reviewPendingCount.value = data.count
    reviewRejectedCount.value = data.rejected_count || 0
    reviewAiAttentionCount.value = data.ai_attention_count || 0
  } catch {
    // Preserve the last successfully read count when the network is temporarily unavailable.
  }
}

const openView = (next: View) => {
  if (next === 'private' && !permissions.value.private_assets) return
  mobileNav.value = false
  view.value = next
  search.value = ''
  category.value = ''
  contentType.value = ''
  mediaType.value = ''
  status.value = ''
  directory.value = ''
  ingestSource.value = ''
  assetSubtype.value = ''
  folderName.value = ''
  onlyMine.value = false
  onlyFavorites.value = next === 'favorites'
  batchMode.value = false
  batchPurpose.value = 'delete'
  selectedAssetIds.value = []
  if (next === 'hall') {
    hallScope.value = 'all'
    hallStage.value = 'overview'
  }
  if (next === 'trash' && !['newest', 'name', 'size'].includes(sort.value)) sort.value = 'newest'
  if (next === 'reviews' || next === 'review-submit') void loadReviewPendingCount()
  void api.facets('', next === 'categories' ? 'product_image' : 'marketing_video').then(data => { facets.value = data })
}

const openRequestFromNotification = (requestId: string) => {
  requestFocusId.value = requestId
  openView('requests')
}

const requestDelete = (asset: Asset) => { confirmAction.value = { kind: 'trash', asset } }
const requestPurge = (asset: Asset) => { confirmAction.value = { kind: 'purge', asset } }
const startBatchMode = (purpose: 'delete' | 'push' | 'review' | 'tags' | 'folder') => {
  batchMode.value = true
  batchPurpose.value = purpose
  selectedAssetIds.value = []
}
const exitBatchMode = () => {
  batchMode.value = false
  batchPurpose.value = 'delete'
  selectedAssetIds.value = []
}
const toggleBatchSelect = (asset: Asset) => {
  if (batchPurpose.value === 'delete' && !asset.can_delete) return
  if (batchPurpose.value === 'tags' && !asset.can_manage) return
  if (batchPurpose.value === 'folder' && (!asset.can_manage || asset.asset_scope !== 'marketing_video' || asset.library_type !== 'source')) return
  if (batchPurpose.value === 'push' && asset.media_type !== 'video') return
  if (batchPurpose.value === 'review' && asset.media_type !== 'video') return
  if (batchPurpose.value === 'push' && !selectedAssetIds.value.includes(asset.id) && selectedAssetIds.value.length >= 10) {
    toast.value = '一次最多选择 10 条视频素材'
    setTimeout(() => { toast.value = '' }, 3000)
    return
  }
  selectedAssetIds.value = selectedAssetIds.value.includes(asset.id)
    ? selectedAssetIds.value.filter(id => id !== asset.id)
    : [...selectedAssetIds.value, asset.id]
}
const toggleSelectAllVisible = () => {
  const visibleIds = selectAllCandidates.value.map(asset => asset.id)
  if (allBatchCandidatesSelected.value) {
    selectedAssetIds.value = selectedAssetIds.value.filter(id => !visibleIds.includes(id))
    return
  }
  const remaining = batchPurpose.value === 'push' ? Math.max(0, 10 - selectedAssetIds.value.length) : visibleIds.length
  const additions = visibleIds.filter(id => !selectedAssetIds.value.includes(id)).slice(0, remaining)
  selectedAssetIds.value = Array.from(new Set([...selectedAssetIds.value, ...additions]))
}
const requestBatchDelete = () => {
  if (selectedAssetIds.value.length) confirmAction.value = { kind: 'batchTrash', assetIds: [...selectedAssetIds.value] }
}
const openBatchOrganize = () => {
  if (!selectedAssetIds.value.length || !['tags', 'folder'].includes(batchPurpose.value)) return
  organizeOpen.value = true
}
const submitBatchOrganize = async (payload: { tags?: string[]; tagMode?: 'add' | 'remove' | 'replace'; folderName?: string }) => {
  if (!selectedAssetIds.value.length) return
  organizeBusy.value = true
  try {
    if (batchPurpose.value === 'tags') {
      const result = await api.batchTags(selectedAssetIds.value, payload.tags || [], payload.tagMode || 'add')
      toast.value = `已更新 ${result.count} 条素材的标签`
    } else {
      const result = await api.batchFolder(selectedAssetIds.value, payload.folderName || '')
      toast.value = payload.folderName ? `已将 ${result.count} 条素材整理到“${payload.folderName}”` : `已将 ${result.count} 条素材移出二级文件夹`
    }
    organizeOpen.value = false
    exitBatchMode()
    await loadAll()
    setTimeout(() => { toast.value = '' }, 3500)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '批量整理失败'
  } finally {
    organizeBusy.value = false
  }
}
const openBatchReviewSubmit = async () => {
  if (!selectedAssetIds.value.length) return
  actionBusy.value = true
  error.value = ''
  try {
    reviewBatchConfig.value = await api.reviewConfig()
    reviewBatchOpen.value = true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '审核人名单读取失败'
  } finally {
    actionBusy.value = false
  }
}

const submitBatchReview = async (assignment: { note: string; assignment_mode: 'organization' | 'designated'; designated_reviewer_number: string; designated_reviewer_name: string }) => {
  if (!selectedAssetIds.value.length) return
  actionBusy.value = true
  error.value = ''
  try {
    const result = await api.reviewBatchSubmit(selectedAssetIds.value.map(assetId => ({ asset_id: assetId, ...assignment })))
    const failedIds = result.failed.map(item => item.asset_id)
    selectedAssetIds.value = failedIds
    const failedText = result.failed.length ? `；${result.failed.length} 条未提交：${result.failed[0].detail}` : ''
    const reviewerText = assignment.assignment_mode === 'designated' ? `，审核人：${assignment.designated_reviewer_name}` : ''
    toast.value = `已批量提审 ${result.succeeded.length} 条${reviewerText}${failedText}`
    if (!failedIds.length) { reviewBatchOpen.value = false; exitBatchMode() }
    await loadAll()
    void loadReviewPendingCount()
    setTimeout(() => { toast.value = '' }, 5000)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '批量提审失败'
  } finally {
    actionBusy.value = false
  }
}
const clearTrashAll = async (confirmText: string) => {
  actionBusy.value = true
  try {
    const result = await api.purgeTrashAll(confirmText)
    toast.value = `已永久清理 ${result.purged} 条素材${result.failed?.length ? `，${result.failed.length} 条失败` : ''}`
    trashClearOpen.value = false
    selected.value = null
    await loadAll()
    setTimeout(() => { toast.value = '' }, 4000)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '永久清空失败'
  } finally {
    actionBusy.value = false
  }
}
const openBatchQianchuan = () => {
  const chosen = selectedAssetIds.value
    .map(id => assets.value.find(asset => asset.id === id))
    .filter((asset): asset is Asset => Boolean(asset && asset.media_type === 'video'))
    .slice(0, 10)
  if (!chosen.length) return
  pushOrigin.value = 'batch'
  qianchuanAssets.value = chosen
}
const openBatchAdq = () => {
  const chosen = selectedAssetIds.value
    .map(id => assets.value.find(asset => asset.id === id))
    .filter((asset): asset is Asset => Boolean(asset && asset.media_type === 'video'))
    .slice(0, 10)
  if (!chosen.length) return
  pushOrigin.value = 'batch'
  adqAssets.value = chosen
}
const openBatchChannels = () => {
  const chosen = selectedAssetIds.value
    .map(id => assets.value.find(asset => asset.id === id))
    .filter((asset): asset is Asset => Boolean(asset && asset.media_type === 'video'))
    .slice(0, 10)
  if (!chosen.length) return
  pushOrigin.value = 'batch'
  channelsAssets.value = chosen
}

const restoreAsset = async (asset: Asset) => {
  try {
    await api.restore(asset.id)
    selected.value = null
    toast.value = `已恢复：${asset.filename}`
    await loadAll()
    setTimeout(() => { toast.value = '' }, 3500)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '恢复失败'
  }
}

const executeConfirmedAction = async () => {
  if (!confirmAction.value) return
  actionBusy.value = true
  const action = confirmAction.value
  try {
    if (action.kind === 'batchTrash') {
      const result = await api.batchDelete(action.assetIds)
      toast.value = `已将 ${result.count} 条素材移入回收站`
      selectedAssetIds.value = []
      batchMode.value = false
      batchPurpose.value = 'delete'
    } else if (action.kind === 'trash') {
      await api.delete(action.asset.id)
      toast.value = `已移入回收站：${action.asset.filename}`
    } else {
      await api.purge(action.asset.id)
      toast.value = `已永久删除：${action.asset.filename}`
    }
    selected.value = null
    confirmAction.value = null
    await loadAll()
    setTimeout(() => { toast.value = '' }, 3500)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '操作失败'
  } finally {
    actionBusy.value = false
  }
}

const selectCategory = (name: string) => {
  if (view.value === 'categories') {
    category.value = category.value === name ? '' : name
  } else {
    openView('hall')
    hallScope.value = 'all'
    hallStage.value = 'results'
    category.value = name
  }
}

const selectTag = (name: string) => {
  openView('hall')
  hallScope.value = 'all'
  hallStage.value = 'results'
  search.value = name
}

const selectDirectory = (name: string) => {
  openView('hall')
  hallScope.value = 'all'
  hallStage.value = 'results'
  directory.value = name
}

const toggleFavorite = async (asset: Asset) => {
  const updated = await api.favorite(asset.id)
  if (view.value === 'favorites' && !updated.favorite) {
    assets.value = assets.value.filter(item => item.id !== updated.id)
    total.value = Math.max(0, total.value - 1)
  } else {
    assets.value = assets.value.map(item => item.id === updated.id ? updated : item)
  }
  if (selected.value?.id === updated.id) selected.value = updated
  stats.value.favorites += updated.favorite ? 1 : -1
}

const toggleEffective = async (asset: Asset) => {
  if (effectiveBusyIds.value.includes(asset.id)) return
  const wasEffective = asset.effective
  effectiveBusyIds.value = [...effectiveBusyIds.value, asset.id]
  try {
    const updated = await api.effective(asset.id, !wasEffective)
    if (view.value === 'effective' && !updated.effective) {
      assets.value = assets.value.filter(item => item.id !== updated.id)
      total.value = Math.max(0, total.value - 1)
    } else {
      assets.value = assets.value.map(item => item.id === updated.id ? updated : item)
    }
    if (selected.value?.id === updated.id) selected.value = updated
    if (updated.effective !== wasEffective) {
      stats.value.effective_materials = Math.max(0, stats.value.effective_materials + (updated.effective ? 1 : -1))
    }
    toast.value = updated.effective ? '已加入“有效一创素材”' : '已取消有效素材标记'
    setTimeout(() => { toast.value = '' }, 2800)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '有效素材标记保存失败'
  } finally {
    effectiveBusyIds.value = effectiveBusyIds.value.filter(id => id !== asset.id)
  }
}

const importEffectiveClipLibrary = async (asset: Asset) => {
  if (clipLibraryBusyIds.value.includes(asset.id)) return
  if (['', '待分类', '其他 WIS 素材'].includes(asset.category?.trim() || '')) {
    error.value = '请先把有效一创素材分类为“通用”或具体产品，再加入切片库'
    return
  }
  clipLibraryBusyIds.value = [...clipLibraryBusyIds.value, asset.id]
  try {
    const queued = await api.effectiveClipImport(asset.id)
    toast.value = queued.message
    for (let attempt = 0; attempt < 60; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 2000))
      const result = await api.effectiveClipStatus(asset.id)
      clipLibraryStates.value = { ...clipLibraryStates.value, [asset.id]: result.status }
      if (result.status.state === 'approved') {
        toast.value = `已按“${asset.category}”录入 ${result.status.approvedCount} 条审核通过切片`
        return
      }
      if (result.status.state === 'technical_attention') {
        toast.value = `已拆分 ${result.status.clipCount} 条；其中 ${result.status.technicalAttentionCount} 条技术项待处理，未进入共享可用池`
        return
      }
    }
    toast.value = '切片仍在后台识别与技术校验，可稍后再次点击读取最新状态'
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '有效一创加入切片库失败'
  } finally {
    clipLibraryBusyIds.value = clipLibraryBusyIds.value.filter(id => id !== asset.id)
    setTimeout(() => { toast.value = '' }, 6000)
  }
}

const importToJianying = async (asset: Asset) => {
  try {
    const result = await api.jianyingImport(asset.id)
    jianyingImportTask.value = result
    if (result.scheme_url) window.location.assign(result.scheme_url)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法发送到剪映'
  }
}

const retryJianyingImport = async (assetId: number) => {
  try {
    const result = await api.jianyingImport(assetId)
    jianyingImportTask.value = result
    if (result.scheme_url) window.location.assign(result.scheme_url)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法重新发起剪映导入'
  }
}

const saveAsset = async (data: Partial<Asset>) => {
  if (!selected.value) return
  saving.value = true
  try {
    const updated = await api.update(selected.value.id, data)
    assets.value = assets.value.map(item => item.id === updated.id ? updated : item)
    selected.value = updated
    facets.value = await api.facets('', view.value === 'categories' ? 'product_image' : 'marketing_video')
    if (updated.asset_scope === 'product_image') productCategories.value = (await api.productImageCategories()).items
  } finally { saving.value = false }
}

const syncOss = async () => {
  syncing.value = true
  error.value = ''
  try {
    syncInfo.value = await api.refresh()
    while (syncInfo.value.state === 'running') {
      await new Promise(resolve => setTimeout(resolve, 3000))
      syncInfo.value = await api.refreshStatus()
    }
    if (syncInfo.value.state === 'failed') throw new Error(syncInfo.value.error || 'OSS 扫描失败')
    await loadAll()
    toast.value = `OSS 扫描完成，共 ${syncInfo.value.total.toLocaleString('zh-CN')} 个媒体对象`
    setTimeout(() => { toast.value = '' }, 3500)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '同步失败'
  } finally { syncing.value = false }
}

const uploaded = async (uploadedAssets: Asset[], completed: boolean) => {
  const productImages = uploadedAssets[0]?.asset_scope === 'product_image'
  view.value = productImages ? 'categories' : 'hall'
  if (uploadedAssets[0]?.library_type) {
    libraryType.value = uploadedAssets[0].library_type
    hallScope.value = uploadedAssets[0].library_type
  }
  if (!productImages) hallStage.value = 'results'
  search.value = ''
  directory.value = ''
  ingestSource.value = 'oa_upload'
  assetSubtype.value = productImages ? '' : uploadedAssets[0]?.asset_subtype || ''
  category.value = productImages ? uploadedAssets[0]?.category || '' : ''
  sort.value = 'newest'
  await loadAll()
  stats.value = await api.stats()
  facets.value = await api.facets('', productImages ? 'product_image' : 'marketing_video')
  productCategories.value = (await api.productImageCategories()).items
  if (completed) {
    uploadOpen.value = false
    productUploadOpen.value = false
  }
  toast.value = completed
    ? `已完成上传 ${uploadedAssets.length} ${productImages ? '张产品图片' : '条素材'}`
    : `本轮成功 ${uploadedAssets.length} 条，失败项可在窗口中重试`
  setTimeout(() => { toast.value = '' }, 3500)
}

const clearFilters = () => {
  search.value = ''
  category.value = ''
  contentType.value = ''
  mediaType.value = ''
  status.value = ''
  directory.value = ''
  ingestSource.value = ''
  assetSubtype.value = ''
  folderName.value = ''
  onlyMine.value = false
  uploadStartDate.value = ''
  uploadEndDate.value = ''
  onlyFavorites.value = view.value === 'favorites'
}

const localDateValue = (offsetDays = 0) => {
  const value = new Date()
  value.setDate(value.getDate() + offsetDays)
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
}
const setUploadDatePreset = (offsetDays?: number) => {
  if (offsetDays === undefined) {
    uploadStartDate.value = ''
    uploadEndDate.value = ''
    return
  }
  const value = localDateValue(offsetDays)
  uploadStartDate.value = value
  uploadEndDate.value = value
}

const openQianchuan = (asset: Asset) => {
  selected.value = null
  pushOrigin.value = 'single'
  qianchuanAssets.value = [asset]
}
const openAdq = (asset: Asset) => {
  selected.value = null
  pushOrigin.value = 'single'
  adqAssets.value = [asset]
}
const openChannels = (asset: Asset) => {
  selected.value = null
  pushOrigin.value = 'single'
  channelsAssets.value = [asset]
}

const acceptLogin = async (loggedInUser: OaUser, loggedInPermissions: OaPermissions) => {
  bootError.value = ''
  bootNeedsLogin.value = false
  user.value = loggedInUser
  permissions.value = loggedInPermissions
  await loadAll()
  void loadReviewPendingCount()
}

const logout = async () => {
  await api.logout().catch(() => undefined)
  if (integratedIntoHub) { returnToHub(); return }
  user.value = null
  permissions.value = { asset_admin: false, asset_delete_manager: false, super_admin: false, operation_admin: false, manage_permissions: false, video_request_assigner: false, video_request_supervisor_viewer: false, review_config_admin: false, can_mark_effective: false, reviewer_roles: [] }
  assets.value = []
  reviewPendingCount.value = 0
  reviewRejectedCount.value = 0
  reviewAiAttentionCount.value = 0
}

const boot = async () => {
  if (bootInFlight) return
  bootInFlight = true
  checking.value = true
  bootError.value = ''
  bootNeedsLogin.value = false
  try {
    const data = await api.me()
    if (!data?.user || typeof data.user.number !== 'string' || !data.user.number.trim()
        || !data.permissions || typeof data.permissions !== 'object' || Array.isArray(data.permissions)) {
      throw new ApiError('登录核验结果不完整，请重试', 502)
    }
    user.value = data.user
    permissions.value = data.permissions
    await openLinkedAsset()
    await loadAll()
    void loadReviewPendingCount()
    if (new URLSearchParams(window.location.search).has('qianchuan')) view.value = 'qianchuan'
    if (new URLSearchParams(window.location.search).has('sales')) view.value = 'sales'
    void api.refreshStatus().then(currentSync => {
      syncInfo.value = currentSync
      if (currentSync.state === 'running') void syncOss()
    }).catch(() => undefined)
  } catch (cause) {
    bootError.value = cause instanceof Error ? cause.message : '暂时无法核验登录状态，请重试'
    bootNeedsLogin.value = cause instanceof ApiError && cause.status === 401
    if (integratedIntoHub && bootNeedsLogin.value) returnToHub()
  } finally {
    bootInFlight = false
    checking.value = false
  }
}

const qianchuanQueued = (count: number) => {
  const shouldContinueBatch = pushOrigin.value === 'batch'
  qianchuanAssets.value = []
  selectedAssetIds.value = []
  if (shouldContinueBatch) {
    batchMode.value = true
    batchPurpose.value = 'push'
  } else {
    batchMode.value = false
    batchPurpose.value = 'delete'
    openView('qianchuan')
  }
  pushOrigin.value = 'single'
  toast.value = shouldContinueBatch
    ? `已创建 ${count} 个千川推送任务，可继续选择下一批`
    : `已创建 ${count} 个千川推送任务`
  setTimeout(() => { toast.value = '' }, 3500)
}
const adqQueued = (count: number) => {
  const shouldContinueBatch = pushOrigin.value === 'batch'
  adqAssets.value = []
  selectedAssetIds.value = []
  if (shouldContinueBatch) {
    batchMode.value = true
    batchPurpose.value = 'push'
  } else {
    batchMode.value = false
    batchPurpose.value = 'delete'
    openView('adq')
  }
  pushOrigin.value = 'single'
  toast.value = shouldContinueBatch
    ? `已创建 ${count} 个腾讯 ADQ 推送任务，可继续选择下一批`
    : `已创建 ${count} 个腾讯 ADQ 推送任务`
  setTimeout(() => { toast.value = '' }, 3500)
}
const channelsQueued = (count: number) => {
  const shouldContinueBatch = pushOrigin.value === 'batch'
  channelsAssets.value = []
  selectedAssetIds.value = []
  if (shouldContinueBatch) {
    batchMode.value = true
    batchPurpose.value = 'push'
  } else {
    batchMode.value = false
    batchPurpose.value = 'delete'
    openView('channels')
  }
  pushOrigin.value = 'single'
  toast.value = shouldContinueBatch
    ? `已创建 ${count} 个视频号发布任务，可继续选择下一批`
    : `已创建 ${count} 个视频号发布任务`
  setTimeout(() => { toast.value = '' }, 3500)
}

onMounted(() => {
  batchToolbarObserver = new IntersectionObserver(([entry]) => {
    batchToolbarPassed.value = Boolean(entry && !entry.isIntersecting && entry.boundingClientRect.bottom <= 76)
  }, { rootMargin: '-76px 0px 0px 0px', threshold: 0 })
  if (batchToolbarElement.value) batchToolbarObserver.observe(batchToolbarElement.value)
  void boot()
  reviewPendingTimer = window.setInterval(() => void loadReviewPendingCount(), 60000)
})
onBeforeUnmount(() => {
  if (reviewPendingTimer) window.clearInterval(reviewPendingTimer)
  batchToolbarObserver?.disconnect()
})
</script>

<template>
  <div v-if="checking" class="boot" role="status"><b>W</b><span>正在核验登录状态，最多等待 15 秒…</span></div>
  <div v-else-if="bootError" class="boot" role="alert">
    <b>W</b><strong>暂时无法进入 WIS 内容中心</strong><span>{{ bootError }}</span>
    <button type="button" class="primary-button" @click="boot">重新核验登录状态</button>
    <button v-if="bootNeedsLogin" type="button" class="secondary-button" @click="integratedIntoHub ? returnToHub() : (bootError = '')">{{ integratedIntoHub ? '返回中枢登录' : '重新登录' }}</button>
  </div>
  <div v-else-if="integratedIntoHub && (bootNeedsLogin || !user)" class="boot" role="alert"><strong>请从中枢完成统一登录</strong><button type="button" class="primary-button" @click="returnToHub">返回中枢</button></div>
  <LoginPage v-else-if="bootNeedsLogin || !user" @login="acceptLogin" />
  <div v-else class="app-shell">
    <aside class="sidebar" :class="{ open: mobileNav }">
      <div class="brand"><div class="brand-mark">W</div><div><strong>WIS</strong><span>CONTENT HUB</span></div></div>
      <nav>
        <span class="nav-label">素材库</span>
        <button :class="{ active: view === 'hall' }" @click="openView('hall')"><LayoutDashboard :size="19" />素材大厅<span class="nav-badge">{{ formatCount(stats.source_materials + stats.remix_outputs) }}</span></button>
        <button v-if="permissions.private_assets" :class="{ active: view === 'private' }" @click="openView('private')"><UserRound :size="19" />我的私人素材</button>
        <button :class="{ active: view === 'favorites' }" @click="openView('favorites')"><Heart :size="19" />我的收藏<span v-if="stats.favorites" class="nav-badge">{{ formatCount(stats.favorites) }}</span></button>
        <button :class="{ active: view === 'hits' }" @click="openView('hits')"><Sparkles :size="19" />爆款素材<span v-if="hitCount" class="nav-badge">{{ formatCount(hitCount) }}</span></button>
        <button :class="{ active: view === 'effective' }" @click="openView('effective')"><BadgeCheck :size="19" />有效一创素材<span v-if="stats.effective_materials" class="nav-badge">{{ formatCount(stats.effective_materials) }}</span></button>
        <span class="nav-label">内容协作</span>
        <button :class="{ active: view === 'requests' }" @click="openView('requests')"><ClipboardList :size="19" />视频中心提需</button>
        <span class="nav-label">视频审核</span>
        <button class="review-nav-button" :class="{ active: view === 'review-submit' }" @click="openView('review-submit')"><UploadCloud :size="19" />提交审核<span v-if="reviewSubmitAttentionCount" class="review-unread-badge" :aria-label="`${reviewSubmitAttentionCount} 条驳回或AI失败素材待处理`">{{ reviewSubmitAttentionCount > 99 ? '99+' : reviewSubmitAttentionCount }}</span></button>
        <button class="review-nav-button" :class="{ active: view === 'reviews' }" @click="openView('reviews')"><BadgeCheck :size="19" />审核中心<span v-if="reviewPendingCount" class="review-unread-badge" :aria-label="`${reviewPendingCount} 条待我审核`">{{ reviewPendingCount > 99 ? '99+' : reviewPendingCount }}</span></button>
        <button v-if="permissions.review_config_admin" :class="{ active: view === 'review-config' }" @click="openView('review-config')"><UserCog :size="19" />审核配置</button>
        <span class="nav-label">数据看板</span>
        <button :class="{ active: view === 'analytics' }" @click="openView('analytics')"><Activity :size="19" />数据统计</button>
        <button class="nav-standalone" :class="{ active: view === 'sales' }" @click="openView('sales')"><BarChart3 :size="19" />个人成交数据</button>
        <span class="nav-label">素材推送</span>
        <button :class="{ active: view === 'qianchuan' }" @click="openView('qianchuan')"><Send :size="19" />千川推送</button>
        <button :class="{ active: view === 'adq' }" @click="openView('adq')"><Send :size="19" />腾讯 ADQ</button>
        <button :class="{ active: view === 'channels' }" @click="openView('channels')"><Send :size="19" />视频号主页推送</button>
        <span class="nav-label">内容管理</span>
        <button :class="{ active: view === 'categories' }" @click="openView('categories')"><Boxes :size="19" />产品图片<span v-if="stats.product_images" class="nav-badge">{{ formatCount(stats.product_images) }}</span></button>
        <button :class="{ active: view === 'tags' }" @click="openView('tags')"><ListFilter :size="19" />标签管理</button>
        <button :class="{ active: view === 'oss' }" @click="openView('oss')"><Cloud :size="19" />OSS 素材目录</button>
        <button :class="{ active: view === 'trash' }" @click="openView('trash')"><Trash2 :size="19" />回收站<span v-if="stats.trash" class="nav-badge">{{ formatCount(stats.trash) }}</span></button>
        <template v-if="permissions.operation_admin"><span class="nav-label">系统管理</span><button :class="{ active: view === 'logs' }" @click="openView('logs')"><ClipboardList :size="19" />操作日志</button><template v-if="permissions.manage_permissions"><button :class="{ active: view === 'access' }" @click="openView('access')"><ShieldCheck :size="19" />登录权限</button><button :class="{ active: view === 'admins' }" @click="openView('admins')"><UserCog :size="19" />管理员权限</button></template></template>
      </nav>
      <div class="sidebar-bottom"><span class="sidebar-settings"><Settings :size="19" />系统由 OA 访问保护</span><div class="user"><div class="avatar">{{ user.realName.slice(0, 1) }}</div><div><strong>{{ user.realName }}</strong><span>{{ user.groupName }}</span></div><button class="logout-button" @click="logout">退出</button></div></div>
    </aside>

    <main>
      <header class="topbar">
        <button class="menu-button" @click="mobileNav = !mobileNav"><Menu /></button>
        <div class="crumb">{{ dataView ? '数据看板' : view === 'requests' ? '内容协作' : ['review-submit', 'reviews', 'review-config'].includes(view) ? '视频审核' : '内容资产' }} <span>/</span> {{ viewInfo.title }}</div>
        <div class="top-actions"><span class="source"><i></i>{{ view === 'sales' ? 'FanDo 根数据 · 每日更新' : view === 'analytics' ? '素材写入 + 已核验千川回流' : view === 'requests' ? '需求流转 + 成片关联回流' : ['review-submit', 'reviews', 'review-config'].includes(view) ? 'OA 身份 + 审核留痕 + 上线拦截' : stats.source }}</span><NotificationCenter @open-request="openRequestFromNotification" /><button v-if="view === 'hall' || view === 'favorites'" class="jianying-top-button" @click="jianyingOpen = true"><FolderSync :size="16" />剪映互传</button><button v-if="!workspaceView" class="upload-button" @click="view === 'categories' ? productUploadOpen = true : uploadOpen = true"><UploadCloud :size="16" />{{ view === 'categories' ? '上传产品图' : '上传素材' }}</button><button v-if="permissions.asset_admin && !workspaceView" class="sync-button" :disabled="syncing" @click="syncOss"><RefreshCw :size="16" :class="{ spin: syncing }" />{{ syncing ? `扫描中 ${syncInfo?.processed?.toLocaleString('zh-CN') || 0}` : '扫描 OSS' }}</button></div>
      </header>

      <div class="content">
        <div v-if="linkedAssetError" role="alert" class="notice"><strong>原交付素材尚未读回</strong><span>{{ linkedAssetError }}</span><button @click="openLinkedAsset">重新读取原素材</button></div>
        <section v-if="view === 'hall' && hallStage === 'overview'" class="hero-row hero-row-simple">
          <div><span class="eyebrow">WIS MARKETING ASSETS</span><h1>让每一条好素材都能被找到、被复用。</h1><p>统一沉淀品牌内容资产，为创意、剪辑与投放团队提供高效素材协作。</p></div>
        </section>

        <section v-if="!workspaceView && (view !== 'hall' || hallStage === 'results')" class="data-scope-note">
          <span><i></i>当前展示公司 OSS <code>yxb/</code> 的真实媒体对象</span>
          <strong>OSS 存储对象 {{ formatCount(stats.oss_total) }}</strong>
          <span>当前可用 {{ formatCount(stats.total) }} 条 · 回收站 {{ formatCount(stats.trash) }} 条</span>
          <small>{{ sourceDate }}</small>
        </section>

        <section v-if="view === 'hall' && hallStage === 'results'" class="stats-grid" aria-label="素材类型快捷筛选">
          <button type="button" class="stat-card stat-card-action" :class="{ active: !mediaType && hallScope === 'all' && !assetSubtype && !category }" aria-label="查看全部可用素材" :aria-pressed="!mediaType && hallScope === 'all' && !assetSubtype && !category" @click="filterHallByMedia('')"><span class="stat-icon coral"><Boxes /></span><span class="stat-card-copy"><small>当前可用素材</small><strong>{{ formatCount(stats.total) }}</strong></span><span class="trend">点击查看全部</span></button>
          <button type="button" class="stat-card stat-card-action" :class="{ active: mediaType === 'video' }" aria-label="查看全部可用视频素材" :aria-pressed="mediaType === 'video'" @click="filterHallByMedia('video')"><span class="stat-icon purple"><Video /></span><span class="stat-card-copy"><small>可用视频素材</small><strong>{{ formatCount(stats.videos) }}</strong></span><span class="mini-bar"><i :style="{ width: `${stats.total ? stats.videos / stats.total * 100 : 0}%` }"></i></span></button>
          <button type="button" class="stat-card stat-card-action" :class="{ active: mediaType === 'image' }" aria-label="查看全部可用图片素材" :aria-pressed="mediaType === 'image'" @click="filterHallByMedia('image')"><span class="stat-icon blue"><Image /></span><span class="stat-card-copy"><small>可用图片素材</small><strong>{{ formatCount(stats.images) }}</strong></span><span class="trend">点击查看图片</span></button>
          <button type="button" class="stat-card stat-card-action" aria-label="查看我的收藏" @click="openView('favorites')"><span class="stat-icon gold"><Heart /></span><span class="stat-card-copy"><small>已收藏</small><strong>{{ formatCount(stats.favorites) }}</strong></span><span class="storage">点击查看</span></button>
        </section>

        <UploadAnalyticsPanel v-if="view === 'analytics'" />
        <PrivateAssetPanel v-else-if="view === 'private'" :categories="(facets.categories || []).map(item => item.name)" />
        <PersonalSalesPanel v-else-if="view === 'sales'" />
        <VideoRequestPanel v-else-if="view === 'requests'" :user="user" :permissions="permissions" :focus-id="requestFocusId" @changed="loadCatalogMetadata" />
        <ReviewCenterPanel v-else-if="view === 'review-submit'" mode="submit" :permissions="permissions" @changed="loadReviewPendingCount" />
        <ReviewCenterPanel v-else-if="view === 'reviews'" mode="queue" :permissions="permissions" @changed="loadReviewPendingCount" />
        <ReviewCenterPanel v-else-if="view === 'review-config'" mode="config" :permissions="permissions" />

        <section v-if="view === 'hall' && hallStage === 'overview'" class="hall-home" aria-label="素材大厅入口">
          <header class="hall-home-head">
            <div><span>WIS ASSET WORKSPACE</span><h2>先选素材类型，再快速找到需要的内容</h2><p>按团队实际工作流程分层进入；也可以直接进入整体大厅查看全部素材。</p></div>
            <button class="hall-overall-button" @click="openHallOverall"><Grid2X2 :size="20" /><span><strong>进入整体大厅</strong><small>不限定分类，查看全部</small></span><ArrowRight :size="20" /></button>
          </header>
          <div class="hall-primary-grid">
            <button class="hall-primary-card source" @click="openHallCategories('source')">
              <span class="hall-primary-icon"><Library :size="32" /></span>
              <span class="hall-primary-copy"><small>ORIGINAL ASSETS</small><strong>视频素材</strong><em>明星、KOL、KOC、自产自拍、产品镜与 AI 一创等原始素材</em></span>
              <span class="hall-primary-cta"><b>{{ formatCount(stats.source_materials) }} 条</b><i>选择分类 <ArrowRight :size="18" /></i></span>
            </button>
            <button class="hall-primary-card remix" @click="openHallCategories('remix')">
              <span class="hall-primary-icon"><Clapperboard :size="32" /></span>
              <span class="hall-primary-copy"><small>REMIX OUTPUTS</small><strong>混剪成片</strong><em>按产品集中查找已完成剪辑、可复用和投放的成片</em></span>
              <span class="hall-primary-cta"><b>{{ formatCount(stats.remix_outputs) }} 条</b><i>选择产品 <ArrowRight :size="18" /></i></span>
            </button>
          </div>
          <div class="hall-quick-grid" aria-label="常用素材入口">
            <button class="hall-quick-card mine" @click="openMyUploads">
              <span class="hall-quick-icon"><UploadCloud :size="23" /></span>
              <span><small>MY UPLOADS</small><strong>我的上传</strong><em>快速找到自己上传与剪映导出的素材</em></span>
              <ArrowRight :size="19" />
            </button>
            <button class="hall-quick-card favorite-card" @click="openView('favorites')">
              <span class="hall-quick-icon"><Heart :size="23" /></span>
              <span><small>FAVORITES</small><strong>我的收藏</strong><em>{{ formatCount(stats.favorites) }} 条收藏，仅自己可见</em></span>
              <ArrowRight :size="19" />
            </button>
            <button class="hall-quick-card hits" @click="openView('hits')">
              <span class="hall-quick-icon"><Sparkles :size="23" /></span>
              <span><small>TOP PERFORMERS</small><strong>爆款素材</strong><em>{{ formatCount(hitCount) }} 条成交额超过 5 万</em></span>
              <ArrowRight :size="19" />
            </button>
            <button class="hall-quick-card effective" @click="openView('effective')">
              <span class="hall-quick-icon"><BadgeCheck :size="23" /></span>
              <span><small>VERIFIED BY TEAM</small><strong>有效一创素材</strong><em>{{ formatCount(stats.effective_materials) }} 条由同事人工确认</em></span>
              <ArrowRight :size="19" />
            </button>
          </div>
        </section>

        <section v-if="view === 'hall' && hallStage === 'categories'" class="hall-category-home">
          <header class="hall-category-head">
            <button class="hall-back-button" @click="openHallOverview"><ArrowLeft :size="18" />返回素材大厅</button>
            <div><span>{{ hallScope === 'source' ? 'SOURCE CATEGORIES' : 'REMIX BY PRODUCT' }}</span><h2>{{ hallScope === 'source' ? '选择视频素材类型' : '选择混剪成片对应产品' }}</h2><p>{{ hallScope === 'source' ? '进入明星、KOL、KOC、自产自拍、产品镜或 AI 一创素材库。' : '直接按产品查看对应混剪成片，后续仍可继续筛选。' }}</p></div>
            <button class="hall-scope-all" @click="openAllInScope(hallScope === 'source' ? 'source' : 'remix')"><Grid2X2 :size="18" />查看全部{{ hallScope === 'source' ? '视频素材' : '混剪成片' }}<ArrowRight :size="17" /></button>
          </header>
          <div v-if="hallScope === 'source'" class="hall-source-grid">
            <button v-for="item in sourceCategoryCards" :key="item.value" @click="openSourceCategory(item.value)">
              <span class="hall-category-icon"><component :is="item.icon" :size="25" /></span>
              <span><strong>{{ item.label }}</strong><small>{{ item.description }}</small></span>
              <b>{{ formatCount(item.count) }}</b><ArrowRight class="hall-card-arrow" :size="19" />
            </button>
          </div>
          <div v-else-if="remixProductCards.length" class="hall-product-grid">
            <button v-for="item in remixProductCards" :key="item.name" @click="openRemixProduct(item.name)"><span><strong>{{ item.name }}</strong><small>{{ formatCount(item.count) }} 条混剪成片</small></span><ArrowRight :size="19" /></button>
          </div>
          <div v-else class="hall-category-empty"><Clapperboard :size="30" /><strong>暂未识别到已分类产品</strong><span>可先查看全部混剪成片，再在素材详情中补充产品分类。</span><button @click="openAllInScope('remix')">查看全部混剪成片</button></div>
        </section>

        <section v-if="view === 'hall' && hallStage === 'results'" class="hall-result-nav">
          <button @click="hallScope === 'all' ? openHallOverview() : openHallCategories(hallScope)"><ArrowLeft :size="17" />{{ hallScope === 'all' ? '返回素材大厅' : '重新选择分类' }}</button>
          <span><b>{{ hallResultTitle }}</b><small>{{ hallResultSubtitle }}</small></span>
          <button class="hall-result-overall" @click="openHallOverall"><Grid2X2 :size="16" />整体大厅</button>
        </section>

        <section v-if="view === 'categories'" class="management-panel product-category-panel">
          <div class="panel-copy"><span>PRODUCT IMAGE LIBRARY</span><h2>WIS 产品图片资产库</h2><p>按产品集中储存高清原图，保留透明底和原始尺寸。点击产品即可筛选，图片类型可继续细分。</p><button class="primary-button product-panel-upload" @click="productUploadOpen = true"><UploadCloud :size="15" />上传产品图片</button></div>
          <div class="product-category-grid">
            <button v-for="item in productCategories" :key="item.name" :class="{ active: category === item.name }" @click="selectCategory(item.name)">
              <span class="product-category-cover"><img v-if="item.cover_url" :src="item.cover_url" :alt="item.name" loading="lazy" /><Image v-else :size="25" /></span>
              <span><strong>{{ item.name }}</strong><small>{{ item.count.toLocaleString('zh-CN') }} 张原图</small></span>
            </button>
          </div>
        </section>

        <section v-else-if="view === 'tags'" class="management-panel">
          <div class="panel-copy"><span>CONTENT TAGS</span><h2>标签管理</h2><p>点击标签快速筛选；打开任一素材详情即可新增、删除并保存标签。</p></div>
          <div class="facet-grid tags"><button v-for="item in facets.tags" :key="item.name" :class="{ active: search === item.name }" @click="selectTag(item.name)"><strong># {{ item.name }}</strong><span>{{ item.count }} 条</span></button></div>
        </section>

        <section v-else-if="view === 'oss'" class="management-panel oss-panel">
          <div class="panel-copy"><span>OSS DIRECTORY</span><h2>真实素材目录</h2><p>直接扫描公司 OSS 的小写 <code>yxb/</code>。目录数量是媒体对象口径，业务索引数量单独显示，不再混用。</p></div>
          <div class="oss-status"><div><i></i><span>连接正常</span><strong>{{ facets.source }}</strong><small>{{ sourceDate }}</small></div><button :disabled="syncing" @click="syncOss"><RefreshCw :size="16" :class="{ spin: syncing }" />重新同步</button></div>
          <div class="directory-list"><button v-for="item in facets.directories" :key="item.name" @click="selectDirectory(item.name)"><Cloud :size="18" /><span><strong>{{ item.name }}</strong><small>{{ item.count.toLocaleString('zh-CN') }} 个媒体对象 · 点击查看</small></span></button></div>
        </section>

        <QianchuanPanel v-else-if="view === 'qianchuan'" />
        <AdqPanel v-else-if="view === 'adq'" />
        <ChannelsPanel v-else-if="view === 'channels'" />
        <AccessAdminPanel v-else-if="view === 'access'" />
        <AdminPermissionsPanel v-else-if="view === 'admins'" />
        <OperationLogPanel v-else-if="view === 'logs'" />

        <section v-else-if="view === 'hits' && !hitCount" class="management-panel compact-panel">
          <div class="panel-copy"><span>VERIFIED GMV ONLY</span><h2>只认真实回流</h2><p>按素材跨账户、跨计划累计已核验的千川日级成交额；重试记录会去重，严格超过 ¥50,000 后自动进入。</p></div>
        </section>

        <section v-if="libraryVisible && (view !== 'hall' || hallStage === 'results')" class="library">
          <div ref="batchToolbarElement" class="library-heading">
            <div>
              <span v-if="view === 'hall'" class="library-kicker">{{ hallResultKicker }}</span>
              <span v-else-if="view === 'categories'" class="library-kicker">PRODUCT IMAGE ASSETS</span>
              <h2>{{ view === 'hall' ? hallResultTitle : view === 'categories' ? (category || '全部产品图片') : viewInfo.title }}</h2>
              <p>{{ view === 'hall' ? hallResultSubtitle : viewInfo.subtitle }} · 共 {{ total.toLocaleString('zh-CN') }} 条结果<span v-if="total > pageSize">，当前第 {{ page }} / {{ totalPages }} 页</span><span v-if="directory"> · 目录 {{ directory }}</span></p>
            </div>
            <div class="library-actions">
              <template v-if="view !== 'trash'">
                <button v-if="batchMode" class="secondary-button batch-select-all" :disabled="!selectAllCandidates.length" @click="toggleSelectAllVisible">{{ batchSelectAllLabel }}</button>
                <button v-if="batchMode && batchPurpose === 'push'" class="primary-button batch-push-trigger" :disabled="!selectedAssetIds.length" @click="openBatchQianchuan"><Send :size="15" />推送千川 {{ selectedAssetIds.length }}/10</button>
                <button v-if="batchMode && batchPurpose === 'push'" class="primary-button batch-push-trigger" :disabled="!selectedAssetIds.length" @click="openBatchAdq"><Send :size="15" />推送 ADQ {{ selectedAssetIds.length }}/10</button>
                <button v-if="batchMode && batchPurpose === 'push'" class="primary-button batch-push-trigger" :disabled="!selectedAssetIds.length" @click="openBatchChannels"><Send :size="15" />推送视频号 {{ selectedAssetIds.length }}/10</button>
                <button v-if="batchMode && batchPurpose === 'review'" class="primary-button" :disabled="!selectedAssetIds.length || actionBusy" @click="openBatchReviewSubmit"><ShieldCheck :size="15" />批量提审 {{ selectedAssetIds.length }}</button>
                <button v-if="batchMode && batchPurpose === 'delete'" class="danger-button batch-delete-trigger" :disabled="!selectedAssetIds.length" @click="requestBatchDelete"><Trash2 :size="15" />移入回收站 {{ selectedAssetIds.length }}</button>
                <button v-if="batchMode && batchPurpose === 'tags'" class="primary-button" :disabled="!selectedAssetIds.length" @click="openBatchOrganize"><ListFilter :size="15" />批量打标签 {{ selectedAssetIds.length }}</button>
                <button v-if="batchMode && batchPurpose === 'folder'" class="primary-button" :disabled="!selectedAssetIds.length" @click="openBatchOrganize"><FolderSync :size="15" />整理到文件夹 {{ selectedAssetIds.length }}</button>
                <button v-if="batchMode" class="secondary-button" @click="exitBatchMode">退出批量</button>
                <template v-else>
                  <button v-if="view !== 'categories'" class="secondary-button" @click="startBatchMode('push')"><Send :size="15" />批量推送</button>
                  <button v-if="view === 'hall' && hallScope === 'remix'" class="secondary-button" @click="startBatchMode('review')"><ShieldCheck :size="15" />批量提审</button>
                  <button class="secondary-button" @click="startBatchMode('tags')"><ListFilter :size="15" />批量标签</button>
                  <button v-if="view === 'hall' && hallScope !== 'remix'" class="secondary-button" @click="startBatchMode('folder')"><FolderSync :size="15" />整理文件夹</button>
                  <button class="secondary-button" @click="startBatchMode('delete')"><Trash2 :size="15" />批量删除</button>
                  <button class="primary-button" @click="view === 'categories' ? productUploadOpen = true : uploadOpen = true"><UploadCloud :size="15" />{{ view === 'categories' ? '上传产品图' : '上传素材' }}</button>
                </template>
              </template>
              <button v-else-if="permissions.super_admin && total" class="danger-button trash-clear-trigger" @click="trashClearOpen = true"><Trash2 :size="15" />一键清空回收站</button>
              <div class="view-toggle"><button class="active" aria-label="网格视图"><Grid2X2 :size="17" /></button><button aria-label="筛选"><SlidersHorizontal :size="17" /></button></div>
            </div>
          </div>

          <Transition name="batch-dock">
            <aside
              v-if="batchToolbarPassed && view !== 'trash' && view !== 'categories' && (!batchMode || batchPurpose === 'push' || batchPurpose === 'review')"
              class="batch-floating-dock"
              :class="{ active: batchMode && (batchPurpose === 'push' || batchPurpose === 'review'), review: batchPurpose === 'review' }"
              :aria-label="batchPurpose === 'review' ? '批量提审快捷操作' : '批量推送快捷操作'"
            >
              <template v-if="batchMode && batchPurpose === 'push'">
                <header class="batch-floating-head" aria-live="polite">
                  <span><Send :size="17" /></span>
                  <div><strong>已选 {{ selectedAssetIds.length }}/10</strong><small>选完后直接推送</small></div>
                </header>
                <div class="batch-floating-targets">
                  <button type="button" :disabled="!selectedAssetIds.length" @click="openBatchQianchuan">千川</button>
                  <button type="button" :disabled="!selectedAssetIds.length" @click="openBatchAdq">ADQ</button>
                  <button type="button" :disabled="!selectedAssetIds.length" @click="openBatchChannels">视频号</button>
                </div>
                <div class="batch-floating-tools">
                  <button type="button" :disabled="!selectAllCandidates.length" @click="toggleSelectAllVisible">{{ batchSelectAllLabel }}</button>
                  <button type="button" @click="exitBatchMode">退出批量</button>
                </div>
              </template>
              <template v-else-if="batchMode && batchPurpose === 'review'">
                <header class="batch-floating-head" aria-live="polite"><span><ShieldCheck :size="17" /></span><div><strong>已选 {{ selectedAssetIds.length }} 条</strong><small>可统一指定一名审核人</small></div></header>
                <div class="batch-floating-targets"><button type="button" :disabled="!selectedAssetIds.length || actionBusy" @click="openBatchReviewSubmit">指定审核人并提审</button></div>
                <div class="batch-floating-tools"><button type="button" :disabled="!selectAllCandidates.length" @click="toggleSelectAllVisible">{{ batchSelectAllLabel }}</button><button type="button" @click="exitBatchMode">退出批量</button></div>
              </template>
              <button v-else-if="view === 'hall' && hallScope === 'remix'" type="button" class="batch-floating-start" @click="startBatchMode('review')">
                <ShieldCheck :size="18" />
                <span><strong>批量提审</strong><small>选择素材并指定审核人</small></span>
              </button>
              <button v-else type="button" class="batch-floating-start" @click="startBatchMode('push')">
                <Send :size="18" />
                <span><strong>批量推送</strong><small>选择视频后投放</small></span>
              </button>
            </aside>
          </Transition>

          <div v-if="view === 'hall' && hallScope !== 'all'" class="library-category-strip">
            <span>素材分类</span>
            <button :class="{ active: !assetSubtype }" @click="assetSubtype = ''">全部</button>
            <button v-for="item in librarySubtypes" :key="item" :class="{ active: assetSubtype === item }" @click="assetSubtype = item">{{ item }}</button>
          </div>

          <div class="filters" :class="{ 'trash-filters': view === 'trash' }">
            <label class="search-box"><Search :size="19" /><input v-model="search" :placeholder="view === 'trash' ? '搜索已删除素材、删除人或标签…' : view === 'categories' ? '搜索产品图名称、分类或标签…' : '搜索文件名、产品、上传同事或标签…'" /><kbd>搜索</kbd></label>
            <select v-if="view !== 'trash' && view !== 'categories'" v-model="ingestSource"><option value="">全部收录方式</option><option value="oa_upload">同事上传</option><option value="oss_scan">OSS 扫描</option></select>
            <select v-if="view === 'hall' && hallScope !== 'all'" v-model="assetSubtype"><option value="">全部{{ libraryTitle }}分类</option><option v-for="item in librarySubtypes" :key="item">{{ item }}</option></select>
            <select v-if="view === 'hall' && hallScope !== 'remix' && sourceFacets.folders.length" v-model="folderName"><option value="">全部二级文件夹</option><option v-for="item in sourceFacets.folders" :key="item.name" :value="item.name">{{ item.name }}（{{ item.count }}）</option></select>
            <select v-if="view !== 'trash'" v-model="category"><option value="">全部产品</option><option v-for="item in (view === 'categories' ? productCategories : facets.categories)" :key="item.name">{{ item.name }}</option></select>
            <select v-if="view !== 'trash'" v-model="contentType"><option value="">{{ view === 'categories' ? '全部图片类型' : '全部类型' }}</option><option v-for="item in facets.content_types" :key="item.name">{{ item.name }}</option></select>
            <select v-if="view !== 'trash' && view !== 'categories' && view !== 'hits'" v-model="status"><option value="">全部状态</option><option v-for="item in facets.statuses" :key="item.name">{{ item.name }}</option><option v-if="!facets.statuses.some(item => item.name === '爆款素材')">爆款素材</option></select>
            <select v-model="sort"><option v-if="view !== 'trash'" value="newest">最近收录</option><option v-if="view !== 'trash'" value="oldest">最早收录</option><option v-if="view !== 'trash'" value="favorites">收藏量从高到低</option><option v-if="view !== 'trash'" value="gmv_desc">成交额从高到低</option><option v-if="view !== 'trash'" value="gmv_asc">成交额从低到高</option><option v-if="view === 'trash'" value="newest">最近删除</option><option value="name">文件名</option><option value="size">文件大小</option></select>
            <div v-if="view !== 'trash'" class="asset-date-filter" aria-label="按上传日期筛选"><span>上传日期</span><input v-model="uploadStartDate" type="date" aria-label="上传开始日期" /><i>-</i><input v-model="uploadEndDate" type="date" aria-label="上传结束日期" /><button type="button" @click="setUploadDatePreset(0)">今日</button><button type="button" @click="setUploadDatePreset(-1)">昨日</button><button v-if="uploadStartDate || uploadEndDate" type="button" @click="setUploadDatePreset()">全部</button></div>
            <button v-if="view !== 'trash' && view !== 'categories'" class="filter-button" :class="{ active: onlyMine }" @click="onlyMine = !onlyMine"><UploadCloud :size="17" />我的上传</button>
            <button v-if="view !== 'trash'" class="filter-button" :class="{ active: onlyFavorites }" @click="onlyFavorites = !onlyFavorites"><Heart :size="17" />收藏<span v-if="activeFilters">{{ activeFilters }}</span></button>
          </div>

          <nav v-if="total" class="asset-pagination asset-pagination-top" aria-label="素材列表顶部分页">
            <span>{{ pageRangeLabel }}</span>
            <PaginationControls :page="page" :total-pages="totalPages" @change="changeAssetPage" />
          </nav>

          <div v-if="assetRefreshWarning" class="notice compact asset-refresh-warning" role="status"><strong>连接波动，素材仍保留显示</strong><span>{{ assetRefreshWarning }}</span><button @click="loadAssets">立即重试</button></div>
          <div v-if="error && assets.length" class="notice compact asset-refresh-warning" role="alert"><strong>本次操作未完成</strong><span>{{ error }}</span><button @click="error = ''">关闭</button></div>
          <div v-if="error && !assets.length" class="notice"><strong>暂时无法读取素材</strong><span>{{ error }}</span><button @click="loadAll">重试</button></div>
          <div v-else-if="loading && !assets.length" class="asset-grid"><div v-for="i in 8" :key="i" class="skeleton"></div></div>
          <div v-else-if="assets.length" class="asset-grid"><AssetCard v-for="(asset, index) in assets" :key="asset.id" :asset="asset" :accent="index" :trash="view === 'trash'" :jianying="view === 'favorites' && asset.favorite && asset.media_type === 'video'" :can-mark-effective="permissions.can_mark_effective" :effective-busy="effectiveBusyIds.includes(asset.id)" :clip-library-busy="clipLibraryBusyIds.includes(asset.id)" :clip-library-state="clipLibraryStates[asset.id]?.state || ''" :selectable="batchMode && (batchPurpose === 'push' || batchPurpose === 'review' ? asset.media_type === 'video' : batchPurpose === 'delete' ? asset.can_delete : batchPurpose === 'folder' ? asset.can_manage && asset.asset_scope === 'marketing_video' && asset.library_type === 'source' : asset.can_manage)" :selected="selectedAssetIds.includes(asset.id)" @open="selected = $event" @select="toggleBatchSelect" @favorite="toggleFavorite" @effective="toggleEffective" @clip-library="importEffectiveClipLibrary" @jianying="importToJianying" @delete="requestDelete" @restore="restoreAsset" @purge="requestPurge" /></div>
          <div v-else class="empty"><Search :size="38" /><h3>{{ emptyCopy.title }}</h3><p>{{ emptyCopy.text }}</p><button class="secondary-button" @click="clearFilters">清除筛选</button></div>
          <nav v-if="total && !loading" class="asset-pagination" aria-label="素材列表底部分页">
            <span>{{ pageRangeLabel }}</span>
            <PaginationControls :page="page" :total-pages="totalPages" @change="changeAssetPage" />
          </nav>
        </section>
      </div>
    </main>

    <AssetDrawer :asset="selected" :expected-etag="selected?.id === linkedAsset?.id ? linkedAsset?.etag : ''" :saving="saving" :trash="view === 'trash'" @close="selected = null" @save="saveAsset" @favorite="toggleFavorite" @qianchuan="openQianchuan" @adq="openAdq" @channels="openChannels" @jianying="importToJianying" @delete="requestDelete" @restore="restoreAsset" @purge="requestPurge" />
    <JianyingBridgePanel v-if="jianyingOpen" @close="jianyingOpen = false" />
    <JianyingImportProgress v-if="jianyingImportTask" :task="jianyingImportTask" @close="jianyingImportTask = null" @retry="retryJianyingImport" />
    <UploadModal v-if="uploadOpen" :user="user" :categories="facets.categories.map(item => item.name)" :folders="sourceFacets.folders.map(item => item.name)" default-library-type="source" @close="uploadOpen = false" @uploaded="uploaded" />
    <UploadModal v-if="productUploadOpen" :user="user" :categories="productCategoryOptions" mode="product-image" @close="productUploadOpen = false" @uploaded="uploaded" />
    <QianchuanPushModal v-if="qianchuanAssets.length" :assets="qianchuanAssets" @close="qianchuanAssets = []" @queued="qianchuanQueued" />
    <AdqPushModal v-if="adqAssets.length" :assets="adqAssets" @close="adqAssets = []" @queued="adqQueued" />
    <ChannelsPushModal v-if="channelsAssets.length" :assets="channelsAssets" @close="channelsAssets = []" @queued="channelsQueued" />
    <BatchOrganizeModal :open="organizeOpen" :mode="batchPurpose === 'folder' ? 'folder' : 'tags'" :count="selectedAssetIds.length" :folders="sourceFacets.folders.map(item => item.name)" :busy="organizeBusy" @close="!organizeBusy && (organizeOpen = false)" @submit="submitBatchOrganize" />
    <ReviewBatchSubmitModal :open="reviewBatchOpen" :count="selectedAssetIds.length" :config="reviewBatchConfig" :busy="actionBusy" @close="!actionBusy && (reviewBatchOpen = false)" @submit="submitBatchReview" />
    <TrashClearModal :open="trashClearOpen" :count="total" :busy="actionBusy" @close="!actionBusy && (trashClearOpen = false)" @confirm="clearTrashAll" />
    <ConfirmModal
      :open="Boolean(confirmAction)"
      :title="confirmAction?.kind === 'purge' ? '永久删除这条素材？' : confirmAction?.kind === 'batchTrash' ? `将 ${confirmAction.assetIds.length} 条素材移入回收站？` : '将素材移入回收站？'"
      :description="confirmAction?.kind === 'purge' ? 'OSS 原文件会立即永久删除，之后无法恢复；投放与审计记录仍会保留。' : confirmAction?.kind === 'batchTrash' ? '这些素材会统一从大厅隐藏，并在回收站保留 7 天；期间可逐条恢复，不会立即删除 OSS 原文件。' : '素材会从大厅隐藏，并在回收站保留 7 天。期间可恢复，满 7 天后由每周任务永久删除。'"
      :confirm-label="confirmAction?.kind === 'purge' ? '永久删除' : confirmAction?.kind === 'batchTrash' ? `确认移入 ${confirmAction.assetIds.length} 条` : '移入回收站'"
      :danger="confirmAction?.kind === 'purge'"
      :busy="actionBusy"
      @close="!actionBusy && (confirmAction = null)"
      @confirm="executeConfirmedAction"
    />
    <Transition name="toast"><div v-if="toast" class="toast">✓ {{ toast }}</div></Transition>
  </div>
</template>
