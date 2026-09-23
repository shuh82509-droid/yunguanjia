<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Clapperboard, Download, ExternalLink, FileImage, Heart, Link2, Plus, RotateCcw, Send, Trash2, Video, X } from 'lucide-vue-next'
import type { Asset } from '../types'
import { CLEANSER_PRODUCT_CATEGORIES } from '../utils/productCategories'
import { deliveryFileStatus } from '../linked-asset'

const props = withDefaults(defineProps<{ asset: Asset | null; saving: boolean; trash?: boolean; expectedEtag?: string }>(), { trash: false, expectedEtag: '' })
const deliveryStatus = computed(() => deliveryFileStatus(props.expectedEtag, props.asset?.etag))
const emit = defineEmits<{ close: []; save: [data: Partial<Asset>]; favorite: [asset: Asset]; qianchuan: [asset: Asset]; adq: [asset: Asset]; channels: [asset: Asset]; jianying: [asset: Asset]; delete: [asset: Asset]; restore: [asset: Asset]; purge: [asset: Asset] }>()

const category = ref('')
const contentType = ref('')
const status = ref('')
const libraryType = ref<'source' | 'remix'>('source')
const assetSubtype = ref('')
const folderName = ref('')
const tags = ref<string[]>([])
const newTag = ref('')
const referenceUrl = ref('')
const materialDescription = ref('')
const performanceScreenshots = ref<Asset['performance_screenshots']>([])
const referenceHref = computed(() => referenceUrl.value.match(/https?:\/\/[^\s]+/i)?.[0] || '')
const filename = ref('')

watch(() => props.asset, (asset) => {
  if (!asset) return
  category.value = asset.category === '燕窝胜肽面膜' ? '燕窝面膜' : asset.category === '清洁泥膜' ? '其他 WIS 素材' : asset.category
  contentType.value = asset.content_type
  status.value = asset.status
  libraryType.value = asset.library_type
  assetSubtype.value = asset.asset_subtype
  folderName.value = asset.folder_name || ''
  tags.value = [...asset.tags]
  referenceUrl.value = asset.reference_url || ''
  materialDescription.value = asset.material_description || ''
  performanceScreenshots.value = [...(asset.performance_screenshots || [])]
  filename.value = asset.filename
}, { immediate: true })

const unavailable = computed(() => !props.asset?.preview_url)
const subtypeOptions = computed(() => libraryType.value === 'source'
  ? ['明星信息流原片', '达人/KOL原片', '达人/KOC原片', '实拍自产素材', '产品镜', 'AI原创素材', '品牌创意广告', '品牌IP广告', '其他视频素材']
  : ['商品卖点混剪', '明星素材混剪', '达人素材混剪', 'AI混剪成片', '其他混剪成片'])
const categoryOptions = computed(() => Array.from(new Set([
  props.asset?.category || '待分类', '待分类', '通用', '隐形水润面膜', '晶润眼膜', '深海次抛', '黑晶面膜',
  '燕窝面膜', '肌活蛋白喷雾', '颈膜', '黄金面膜', '美白针',
  ...CLEANSER_PRODUCT_CATEGORIES,
])))
const contentOptions = computed(() => Array.from(new Set([
  ...(props.asset?.content_type ? [props.asset.content_type] : []),
  ...(props.asset?.asset_scope === 'product_image'
  ? ['主视觉/KV', '带投影', '无投影/透明底', '仰视', '俯视', '平视/正面', '开盒/内容物', '替换装', '组合装', '其他产品图']
  : ['上脸展示', '数字人', '图文', '产品展示', '痛点', '测评', '口播', '剧情', '科普', '明星', '其他']),
])))
watch(libraryType, () => {
  if (!subtypeOptions.value.includes(assetSubtype.value)) assetSubtype.value = subtypeOptions.value[0]
  if (libraryType.value !== 'source') folderName.value = ''
})
const addTag = () => {
  const value = newTag.value.trim()
  if (value && !tags.value.includes(value)) tags.value.push(value)
  newTag.value = ''
}
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer">
      <div v-if="asset" class="drawer-layer" @click.self="emit('close')">
        <aside class="drawer-panel" aria-label="素材详情">
          <div class="drawer-top">
            <div><span class="eyebrow">素材详情</span><h2>{{ asset.filename }}</h2></div>
            <button class="icon-button" aria-label="关闭" @click="emit('close')"><X /></button>
          </div>

          <p v-if="expectedEtag" :role="deliveryStatus === 'same' ? 'status' : 'alert'" class="notice">
            {{ deliveryStatus === 'same' ? '文件标识与任务交付时一致；分类和审核以当前记录为准。' : deliveryStatus === 'changed' ? '文件标识与任务交付时不同，请返回原任务核对版本后再确认。' : '当前文件标识尚未读回，暂不能确认与任务交付版本一致。' }}
          </p>
          <div class="preview-stage" :class="{ vertical: asset.media_type === 'video' }">
            <video v-if="asset.media_type === 'video' && asset.preview_url" :src="asset.preview_url" controls playsinline />
            <img v-else-if="asset.media_type === 'image' && asset.preview_url" :src="asset.preview_url" :alt="asset.filename" />
            <div v-else class="demo-preview"><span>WIS</span><strong>{{ asset.category }}</strong><small>{{ unavailable ? '当前文件地址不可用' : '' }}</small></div>
          </div>

          <section v-if="!trash && asset.can_manage" class="asset-edit-card">
            <div class="asset-edit-heading"><div><span>素材信息</span><strong>上传后仍可随时修改分类和标签</strong></div><small>自定义标签会优先显示在封面</small></div>
            <div class="tag-editor priority-tags">
              <label>营销标签</label>
              <div class="editable-tags"><button v-for="(tag, index) in tags" :key="`${tag}-${index}`" @click="tags.splice(index, 1)">{{ tag }}<X :size="13" /></button></div>
              <div class="tag-input"><input v-model="newTag" placeholder="输入标签后回车" @keyup.enter.prevent="addTag" /><button @click="addTag"><Plus :size="16" />添加</button></div>
            </div>
            <div class="form-grid asset-classification-grid">
              <label class="asset-name-field">素材名称<input v-model="filename" maxlength="512" placeholder="输入素材名称" /><small>仅修改平台展示名称，不移动或重传 OSS 原文件</small></label>
              <label v-if="asset.asset_scope !== 'product_image'">素材库<select v-model="libraryType"><option value="source">视频素材</option><option value="remix">混剪成片</option></select></label>
              <label v-if="asset.asset_scope !== 'product_image'">素材分类<select v-model="assetSubtype"><option v-for="item in subtypeOptions" :key="item">{{ item }}</option></select></label>
              <label v-if="asset.asset_scope !== 'product_image' && libraryType === 'source'">二级文件夹<input v-model="folderName" maxlength="160" placeholder="选填，如 KOC原片-林凡清" /></label>
              <label>产品分类<select v-model="category"><option v-for="item in categoryOptions" :key="item">{{ item }}</option></select></label>
              <label>{{ asset.asset_scope === 'product_image' ? '图片类型' : '内容类型' }}<select v-model="contentType"><option v-for="item in contentOptions" :key="item">{{ item }}</option></select></label>
              <label>素材状态<select v-model="status"><option>待整理</option><option>测试中</option><option>稳定跑量</option><option>爆款素材</option><option>衰退素材</option></select></label>
              <label class="reference-url-field">竞对/参考信息<input v-model="referenceUrl" type="text" placeholder="可填写任意链接或文字说明（选填）" /></label>
              <label v-if="asset.asset_subtype === 'AI原创素材'" class="material-description-field">素材说明<textarea v-model="materialDescription" rows="4" maxlength="10000" placeholder="说明开头、卖点、表现形式和适合的投放场景" /></label>
            </div>
            <div v-if="asset.asset_subtype === 'AI原创素材' && performanceScreenshots.length" class="asset-evidence-edit"><span>效果/数据截图 <small>点击右上角可移除错误截图</small></span><div><figure v-for="(image, index) in performanceScreenshots" :key="image.object_key"><a :href="image.url" target="_blank" rel="noreferrer"><img :src="image.url" :alt="image.filename" /></a><figcaption>{{ image.filename }}</figcaption><button type="button" aria-label="移除数据截图" @click="performanceScreenshots.splice(index, 1)"><X :size="12" /></button></figure></div></div>
            <button class="primary-button save" :disabled="saving" @click="emit('save', { filename, category, content_type: contentType, status, library_type: libraryType, asset_subtype: assetSubtype, folder_name: folderName, tags, reference_url: referenceUrl, material_description: materialDescription, performance_screenshots: performanceScreenshots })">{{ saving ? '保存中…' : '保存素材信息' }}</button>
          </section>

          <div v-if="!trash && asset.media_type === 'video'" class="drawer-action-group drawer-push-actions">
            <span>素材推送</span>
            <button class="primary-button" @click="emit('qianchuan', asset)"><Send :size="17" />推送千川</button>
            <button class="primary-button" @click="emit('adq', asset)"><Send :size="17" />推送 ADQ</button>
            <button class="primary-button" @click="emit('channels', asset)"><Send :size="17" />推送视频号</button>
          </div>

          <div class="drawer-actions drawer-file-actions">
            <button v-if="trash && asset.can_delete" class="primary-button" @click="emit('restore', asset)"><RotateCcw :size="17" />恢复素材</button>
            <button v-if="trash && asset.can_purge" class="danger-button" @click="emit('purge', asset)"><Trash2 :size="17" />永久删除</button>
            <button v-if="!trash" class="secondary-button" @click="emit('favorite', asset)"><Heart :size="17" :fill="asset.favorite ? 'currentColor' : 'none'" />{{ asset.favorite ? '已收藏' : '收藏' }} · {{ asset.favorite_count || 0 }}</button>
            <button v-if="!trash && asset.media_type === 'video' && asset.favorite" class="jianying-import-button" @click="emit('jianying', asset)"><Clapperboard :size="17" />导入剪映</button>
            <a v-if="asset.download_url" class="secondary-button" :href="asset.download_url" target="_blank"><Download :size="17" />下载</a>
            <a v-if="asset.preview_url" class="secondary-button" :href="asset.preview_url" target="_blank"><ExternalLink :size="17" />原始文件</a>
            <button v-if="!trash && asset.can_delete" class="danger-link" @click="emit('delete', asset)"><Trash2 :size="17" />移入回收站</button>
          </div>

          <div v-if="trash" class="trash-detail-note">
            <Trash2 :size="18" />
            <div><strong>已由 {{ asset.deleted_by_name || '部门成员' }} 移入回收站</strong><span>{{ asset.purge_after ? `保留至 ${new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(asset.purge_after))}，之后由每周清理任务永久删除` : '等待定期清理' }}</span></div>
          </div>

          <div v-if="asset.reference_url || asset.reference_video_url" class="asset-reference-card">
            <div><span>创作参考</span><strong>竞对链接与参考视频</strong></div>
            <a v-if="asset.reference_url && referenceHref" :href="referenceHref" target="_blank" rel="noreferrer"><Link2 :size="16" /><span><strong>打开竞对/参考链接</strong><small>{{ asset.reference_url }}</small></span></a>
            <div v-else-if="asset.reference_url" class="reference-text"><Link2 :size="16" /><span><strong>竞对/参考信息</strong><small>{{ asset.reference_url }}</small></span></div>
            <a v-if="asset.reference_video_url" :href="asset.reference_video_url" target="_blank"><Video :size="16" /><span><strong>查看参考视频</strong><small>{{ asset.reference_video_name || '参考视频' }}</small></span></a>
          </div>

          <div v-if="asset.asset_subtype === 'AI原创素材' && (materialDescription || performanceScreenshots.length)" class="asset-ai-evidence-card">
            <div><FileImage :size="18" /><span><strong>AI 一创素材说明</strong><small>人工经验与真实投放表现归档</small></span></div>
            <p v-if="materialDescription">{{ materialDescription }}</p>
            <div v-if="performanceScreenshots.length" class="asset-evidence-gallery"><a v-for="image in performanceScreenshots" :key="image.object_key" :href="image.url" target="_blank" rel="noreferrer"><img :src="image.url" :alt="image.filename" /><span>{{ image.filename }}</span></a></div>
          </div>

          <div class="asset-source asset-metadata-card">
            <span v-if="asset.historical_gmv_yuan != null">历史回流成交额<strong>¥{{ asset.historical_gmv_yuan.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) }}</strong></span>
            <span>资产库<strong>{{ asset.asset_scope === 'product_image' ? '产品图片' : asset.library_type === 'remix' ? '混剪成片' : '视频素材' }}</strong></span>
            <span>{{ asset.asset_scope === 'product_image' ? '图片类型' : '素材分类' }}<strong>{{ asset.asset_scope === 'product_image' ? asset.content_type : asset.asset_subtype || '其他视频素材' }}</strong></span>
            <span v-if="asset.folder_name">二级文件夹<strong>{{ asset.folder_name }}</strong></span>
            <span>收录方式<strong>{{ asset.ingest_source === 'oa_upload' ? '同事上传' : 'OSS 扫描' }}</strong></span>
            <span>业务来源<strong>{{ asset.source || '未标注' }}</strong></span>
            <span>关联账号<strong>{{ asset.account_name || '未标注' }}</strong></span>
            <span>OSS 路径<strong>{{ asset.object_key }}</strong></span>
          </div>
        </aside>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.material-description-field { grid-column:1/-1; }
.material-description-field textarea { width:100%; min-height:92px; box-sizing:border-box; resize:vertical; border:1px solid #c9dde8; border-radius:12px; padding:11px 13px; color:#153b59; background:#fff; font:inherit; }
.asset-evidence-edit { display:grid; gap:9px; margin:12px 0 16px; }
.asset-evidence-edit > span { color:#4c7088; font-size:13px; font-weight:800; }
.asset-evidence-edit > span small { margin-left:7px; color:#8095a4; font-weight:500; }
.asset-evidence-edit > div,.asset-evidence-gallery { display:grid; grid-template-columns:repeat(auto-fill,minmax(100px,1fr)); gap:9px; }
.asset-evidence-edit figure { position:relative; min-width:0; margin:0; overflow:hidden; border:1px solid #d5e6ee; border-radius:10px; background:#f6fafc; }
.asset-evidence-edit img,.asset-evidence-gallery img { display:block; width:100%; aspect-ratio:4/3; object-fit:cover; }
.asset-evidence-edit figcaption,.asset-evidence-gallery span { display:block; overflow:hidden; padding:6px 8px; color:#567086; font-size:10px; text-overflow:ellipsis; white-space:nowrap; }
.asset-evidence-edit button { position:absolute; top:4px; right:4px; display:grid; place-items:center; width:22px; height:22px; border:0; border-radius:7px; color:#fff; background:rgba(15,47,66,.76); }
.asset-ai-evidence-card { display:grid; gap:12px; padding:17px; border:1px solid #cfe3ed; border-radius:17px; background:linear-gradient(145deg,#f8fcfe,#eef8fc); }
.asset-ai-evidence-card > div:first-child { display:flex; align-items:center; gap:9px; color:#087dbb; }
.asset-ai-evidence-card > div:first-child span { display:grid; gap:2px; }
.asset-ai-evidence-card > div:first-child small { color:#7a91a2; font-size:11px; font-weight:500; }
.asset-ai-evidence-card p { margin:0; padding:11px 13px; border-radius:11px; color:#31586f; background:#fff; white-space:pre-wrap; line-height:1.65; }
.asset-evidence-gallery a { overflow:hidden; border:1px solid #d5e6ee; border-radius:10px; color:inherit; text-decoration:none; background:#fff; }
</style>
