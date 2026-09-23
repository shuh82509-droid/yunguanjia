<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { BadgeCheck, CalendarDays, Check, Clapperboard, Heart, Image, Play, RotateCcw, Scissors, Trash2, Video } from 'lucide-vue-next'
import VideoCover from './VideoCover.vue'
import type { Asset } from '../types'

const props = withDefaults(defineProps<{ asset: Asset; accent: number; trash?: boolean; selectable?: boolean; selected?: boolean; jianying?: boolean; canMarkEffective?: boolean; effectiveBusy?: boolean; clipLibraryBusy?: boolean; clipLibraryState?: string }>(), { trash: false, selectable: false, selected: false, jianying: false, canMarkEffective: false, effectiveBusy: false, clipLibraryBusy: false, clipLibraryState: '' })
defineEmits<{ open: [asset: Asset]; favorite: [asset: Asset]; effective: [asset: Asset]; clipLibrary: [asset: Asset]; jianying: [asset: Asset]; delete: [asset: Asset]; restore: [asset: Asset]; purge: [asset: Asset]; select: [asset: Asset] }>()

const imageFailed = ref(false)
const staticCover = computed(() => props.asset.cover_url || (props.asset.media_type === 'image' ? props.asset.preview_url : ''))
const ownerName = computed(() => props.asset.account_name?.trim() || '姓名待标注')
const ownerInitial = computed(() => ownerName.value === '姓名待标注' ? '待' : ownerName.value.slice(0, 1))
const originLabel = computed(() => props.asset.source === 'jianying_export' ? '剪映导出' : props.asset.ingest_source === 'oa_upload' ? '同事上传' : 'OSS 扫描')
const systemTags = new Set(['上传素材', '产品图片', '剪映导出'])
const customTags = computed(() => props.asset.tags.filter(tag => !systemTags.has(tag)))
const displayTags = computed(() => customTags.value.length ? customTags.value : props.asset.tags)
const clipLibraryLabel = computed(() => props.clipLibraryBusy
  ? '正在识别并技术校验…'
  : props.clipLibraryState === 'approved'
    ? '已录入切片库'
    : props.clipLibraryState === 'technical_attention'
      ? '已录入，技术项待处理'
      : props.clipLibraryState === 'source_imported'
        ? '正在拆分完整内容段…'
        : '免二审录入切片库')
const clipLibraryCategoryReady = computed(() => !['', '待分类', '其他 WIS 素材'].includes(props.asset.category?.trim() || ''))

watch(() => props.asset.id, () => { imageFailed.value = false })

const formatSize = (bytes: number) => bytes <= 0 ? '大小待读取' : bytes > 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`
const formatDate = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(new Date(value))
const formatGmv = (value: number) => value >= 10000
  ? `¥${(value / 10000).toLocaleString('zh-CN', { minimumFractionDigits: value >= 100000 ? 1 : 2, maximumFractionDigits: 2 })}万`
  : `¥${value.toLocaleString('zh-CN', { minimumFractionDigits: value % 1 ? 2 : 0, maximumFractionDigits: 2 })}`
const purgeLabel = computed(() => {
  if (!props.asset.purge_after) return '等待定期清理'
  const remaining = new Date(props.asset.purge_after).getTime() - Date.now()
  const days = Math.max(0, Math.ceil(remaining / 86400000))
  return days > 0 ? `${days} 天后可清理` : '已进入清理范围'
})
</script>

<template>
  <article class="asset-card" :class="{ 'batch-selectable': selectable, 'batch-selected': selected, 'product-image-card': asset.asset_scope === 'product_image' }" @click="selectable ? $emit('select', asset) : $emit('open', asset)">
    <div class="asset-thumb" :class="`accent-${accent % 5}`">
      <img v-if="staticCover && !imageFailed" :src="staticCover" :alt="asset.filename" loading="lazy" @error="imageFailed = true" />
      <VideoCover v-else-if="asset.media_type === 'video'" :src="asset.preview_url" :alt="asset.filename" />
      <div v-else class="thumb-art">
        <span class="thumb-brand">WIS</span>
        <span>{{ asset.category }}</span>
      </div>
      <button v-if="selectable" class="batch-card-select" :class="{ selected }" :aria-label="selected ? '取消选择' : '选择素材'" @click.stop="$emit('select', asset)"><Check v-if="selected" :size="17" /></button>
      <button v-if="!trash && !selectable && asset.can_delete" class="card-delete" aria-label="移入回收站" title="移入回收站" @click.stop="$emit('delete', asset)"><Trash2 :size="16" /></button>
      <button v-if="!trash && !selectable" class="favorite" :class="{ active: asset.favorite }" :aria-label="`${asset.favorite ? '取消收藏' : '收藏'}，当前 ${asset.favorite_count || 0} 人收藏`" :title="`${asset.favorite_count || 0} 人收藏`" @click.stop="$emit('favorite', asset)">
        <Heart :size="17" :fill="asset.favorite ? 'currentColor' : 'none'" />
        <span>{{ asset.favorite_count || 0 }}</span>
      </button>
      <span v-else-if="trash" class="trash-expiry">{{ purgeLabel }}</span>
      <span class="asset-origin" :class="asset.ingest_source">{{ originLabel }}</span>
      <div v-if="displayTags.length" class="asset-cover-tags" aria-label="素材标签">
        <span v-for="tag in displayTags.slice(0, 2)" :key="tag" :title="tag">#{{ tag }}</span>
        <span v-if="displayTags.length > 2">+{{ displayTags.length - 2 }}</span>
      </div>
      <span class="media-chip"><Video v-if="asset.media_type === 'video'" :size="13" /><Image v-else :size="13" /> {{ asset.media_type === 'video' ? '视频' : '图片' }}</span>
      <span class="asset-owner" :title="`素材归属：${ownerName}`"><i>{{ ownerInitial }}</i>{{ ownerName }}</span>
      <button v-if="asset.media_type === 'video'" class="play-button" aria-label="播放视频"><Play :size="19" fill="currentColor" /></button>
    </div>
    <div v-if="asset.platform_gmv?.length" class="asset-performance-strip" aria-label="素材成交回流">
      <span v-for="item in asset.platform_gmv" :key="item.platform" :class="`platform-${item.platform}`" :title="`${item.label} ¥${item.gmv_yuan.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`">
        <small>{{ item.label }}</small><strong>{{ formatGmv(item.gmv_yuan) }}</strong>
      </span>
    </div>
    <div class="asset-body">
      <div class="asset-heading">
        <h3 :title="asset.filename">{{ asset.filename }}</h3>
      </div>
      <div class="tag-row">
        <span v-if="asset.folder_name" class="pill asset-folder" :title="asset.folder_name">文件夹 · {{ asset.folder_name }}</span>
        <span v-for="tag in displayTags.slice(0, 3)" :key="tag" class="pill priority-tag">{{ tag }}</span>
        <span v-if="displayTags.length > 3" class="pill priority-tag">+{{ displayTags.length - 3 }}</span>
        <span class="pill library-type" :class="asset.asset_scope === 'product_image' ? 'product-image' : asset.library_type">{{ asset.asset_scope === 'product_image' ? '产品图片' : asset.library_type === 'remix' ? '混剪成片' : '视频素材' }}</span>
        <span v-if="asset.asset_scope !== 'product_image'" class="pill subtype" :title="asset.asset_subtype">{{ asset.asset_subtype }}</span>
        <span class="pill product">{{ asset.category }}</span>
        <span class="pill">{{ asset.content_type }}</span>
      </div>
      <div class="asset-meta">
        <span><CalendarDays :size="14" />{{ formatDate(asset.modified_at) }}</span>
        <span>{{ formatSize(asset.size) }}</span>
        <span class="status-dot" :class="asset.status">{{ asset.status }}</span>
      </div>
      <button v-if="!trash && !selectable && canMarkEffective && asset.media_type === 'video' && asset.asset_scope === 'marketing_video'" type="button" class="asset-effective-toggle" :class="{ active: asset.effective }" :disabled="effectiveBusy" :aria-pressed="asset.effective" @click.stop="$emit('effective', asset)">
        <BadgeCheck :size="16" />{{ effectiveBusy ? '正在保存…' : asset.effective ? '已标记为有效' : '标记为有效素材' }}
      </button>
      <span v-else-if="!trash && !selectable && asset.effective" class="asset-effective-readonly"><BadgeCheck :size="15" />有效一创素材</span>
      <button v-if="!trash && !selectable && asset.effective" type="button" class="asset-jianying-import effective-clip-import" :disabled="clipLibraryBusy || !clipLibraryCategoryReady" :title="clipLibraryCategoryReady ? '继承有效一创内容审核，只保留自动技术与边界校验' : '请先分类为通用或具体产品'" @click.stop="$emit('clipLibrary', asset)"><Scissors :size="15" />{{ clipLibraryCategoryReady ? clipLibraryLabel : '请先补充产品分类' }}</button>
      <button v-if="jianying" type="button" class="asset-jianying-import" @click.stop="$emit('jianying', asset)"><Clapperboard :size="15" />导入剪映</button>
      <div v-if="trash" class="trash-card-actions" @click.stop>
        <button v-if="asset.can_delete" class="restore-action" @click="$emit('restore', asset)"><RotateCcw :size="15" />恢复素材</button>
        <button v-if="asset.can_purge" class="purge-action" @click="$emit('purge', asset)"><Trash2 :size="15" />永久删除</button>
        <span v-else>满 7 天由系统清理</span>
      </div>
    </div>
  </article>
</template>
