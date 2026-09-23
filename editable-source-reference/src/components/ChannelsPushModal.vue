<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Check, Image as ImageIcon, LoaderCircle, PackageSearch, Search, Send, X } from 'lucide-vue-next'
import { api } from '../api'
import { uploadFileMultipart } from '../utils/multipartUpload'
import { publishDescription, seedDrafts, textLength } from '../utils/channelsCopy'
import type { Asset, ChannelsAccount, ChannelsProduct, ChannelsVideoAnnotationCode, ChannelsVideoAnnotationInput } from '../types'

const props = defineProps<{ assets: Asset[] }>()
const emit = defineEmits<{ close: []; queued: [count: number] }>()
const accounts = ref<ChannelsAccount[]>([])
const selected = ref<string[]>([])
const assetTitles = ref<Record<number, string>>({})
const fullTitles = ref<Record<number, string>>({})
const assetBodies = ref<Record<number, string>>({})
watch(() => props.assets, assets => seedDrafts(assets, fullTitles.value, assetTitles.value, assetBodies.value), { immediate: true, deep: true })
const annotationOptions: { value: ChannelsVideoAnnotationCode; label: string }[] = [
  { value: 'none', label: '无需标注' },
  { value: 'ai_generated', label: '含AI生成内容' },
  { value: 'fictional', label: '内容为虚构剧情，仅供娱乐' },
  { value: 'personal_opinion', label: '个人观点，仅供参考' },
  { value: 'marketing_ad', label: '内容包含营销广告' },
  { value: 'self_shot', label: '内容为自行拍摄' },
  { value: 'repost', label: '内容为转载' },
]
const defaultAnnotation = (asset: Asset): ChannelsVideoAnnotationCode => {
  const text = [asset.filename, asset.asset_subtype, asset.content_type, asset.material_description, ...(asset.tags || [])].join(' ')
  return /aigc|数字人|人工智能生成|ai生成|ai原创|ai混剪|(^|[^a-z])ai([^a-z]|$)/i.test(text) ? 'ai_generated' : 'none'
}
const assetAnnotations = ref<Record<number, ChannelsVideoAnnotationCode>>(Object.fromEntries(props.assets.map(asset => [asset.id, defaultAnnotation(asset)])))
const annotationShootingTimes = ref<Record<number, string>>(Object.fromEntries(props.assets.map(asset => [asset.id, ''])))
const annotationShootingLocations = ref<Record<number, string>>(Object.fromEntries(props.assets.map(asset => [asset.id, ''])))
const annotationRepostSources = ref<Record<number, string>>(Object.fromEntries(props.assets.map(asset => [asset.id, ''])))
const description = ref('')
const tagsText = ref('')
const products = ref<ChannelsProduct[]>([])
const productQuery = ref('')
const selectedProduct = ref<ChannelsProduct | null>(null)
const selectedProductId = computed(() => selectedProduct.value?.id || '')
const publishMode = ref<'product' | 'plain'>('product')
const loading = ref(true)
const productLoading = ref(false)
const productState = ref<'idle' | 'loading' | 'refreshing' | 'ready' | 'error'>('idle')
const productMessage = ref('')
const productReadAt = ref('')
const productCached = ref(false)
const productStale = ref(false)
const productComplete = ref(false)
const productCachedTotal = ref(0)
const productAttempt = ref(0)
const productMaxAttempts = ref(3)
const pushing = ref(false)
const error = ref('')
const productError = ref('')
const coverFiles = ref<Record<number, File | undefined>>({})
const coverUrls = ref<Record<number, string>>({})
const coverStage = ref('')
const expected = computed(() => props.assets.length * selected.value.length)
const productStatusText = computed(() => {
  if (productState.value === 'refreshing') return productMessage.value || `已先显示缓存中的 ${productCachedTotal.value || products.value.length} 个商品，后台正在补齐完整列表${productAttempt.value ? `（第 ${productAttempt.value}/${productMaxAttempts.value} 次）` : ''}`
  if (productState.value === 'loading') return productMessage.value || `后台正在读取视频号商品${productAttempt.value ? `（第 ${productAttempt.value}/${productMaxAttempts.value} 次）` : ''}`
  if (productState.value === 'ready' && productReadAt.value) return `${productComplete.value ? `完整列表 ${productCachedTotal.value} 个商品` : '列表完整性待核验'} · 最近更新 ${new Date(productReadAt.value).toLocaleString('zh-CN', { hour12: false })}`
  return productMessage.value
})
let productPollTimer = 0
let productRequestVersion = 0
let productPollCount = 0
const appliedProductQuery = ref('')

const clearProductPoll = () => {
  window.clearTimeout(productPollTimer)
  productPollTimer = 0
}

const toggle = (id: string) => {
  if (pushing.value) return
  selected.value = selected.value.includes(id)
    ? selected.value.filter(value => value !== id)
    : [...selected.value, id]
}

const chooseCover = (assetId: number, files: FileList | null) => {
  const file = files?.[0]
  if (!file) return
  if (!file.type.startsWith('image/') || file.size <= 0 || file.size > 20 * 1024 ** 2) {
    error.value = '视频封面仅支持不超过 20MB 的 JPG、PNG 或 WEBP 图片'
    return
  }
  if (coverUrls.value[assetId]) URL.revokeObjectURL(coverUrls.value[assetId])
  coverFiles.value = { ...coverFiles.value, [assetId]: file }
  coverUrls.value = { ...coverUrls.value, [assetId]: URL.createObjectURL(file) }
}

const clearCover = (assetId: number) => {
  if (coverUrls.value[assetId]) URL.revokeObjectURL(coverUrls.value[assetId])
  const nextFiles = { ...coverFiles.value }; delete nextFiles[assetId]
  const nextUrls = { ...coverUrls.value }; delete nextUrls[assetId]
  coverFiles.value = nextFiles; coverUrls.value = nextUrls
}

const loadProducts = async (force = false, polling = false) => {
  const accountId = selected.value[0]
  if (!polling) {
    clearProductPoll()
    productPollCount = 0
    appliedProductQuery.value = productQuery.value.trim()
  }
  if (!accountId) {
    productState.value = 'idle'
    productLoading.value = false
    return
  }
  const requestVersion = ++productRequestVersion
  if (!products.value.length) productLoading.value = true
  productError.value = ''
  try {
    const result = await api.channelsProducts(accountId, appliedProductQuery.value, force)
    if (requestVersion !== productRequestVersion || accountId !== selected.value[0]) return
    products.value = result.items
    productState.value = result.state
    productLoading.value = result.state === 'loading' || result.state === 'refreshing'
    productMessage.value = result.message
    productReadAt.value = result.read_at
    productCached.value = result.cached
    productStale.value = result.stale
    productComplete.value = result.complete
    productCachedTotal.value = result.cached_total
    productAttempt.value = result.attempt
    productMaxAttempts.value = result.max_attempts
    productError.value = result.error
    if (result.state === 'loading' || result.state === 'refreshing') {
      productPollCount += 1
      if (productPollCount <= 180) {
        productPollTimer = window.setTimeout(() => void loadProducts(false, true), Math.max(result.retry_after_ms || 2000, 1000))
      } else {
        productState.value = products.value.length ? 'ready' : 'error'
        productLoading.value = false
        productError.value = '后台读取时间较长，请稍后点击刷新；已读取到的缓存商品仍可使用'
      }
    }
  } catch (e) {
    if (requestVersion !== productRequestVersion) return
    productState.value = products.value.length ? 'ready' : 'error'
    productError.value = e instanceof Error ? e.message : '无法读取该视频号的橱窗商品'
  } finally {
    if (requestVersion === productRequestVersion && !['loading', 'refreshing'].includes(productState.value)) productLoading.value = false
  }
}

const push = async () => {
  if (pushing.value) return
  if (!selected.value.length) return
  if (publishMode.value === 'product' && !selectedProduct.value) {
    error.value = '请选择本批视频要挂车的商品'
    return
  }
  const invalidTitle = props.assets.find(asset => {
    const length = textLength(assetTitles.value[asset.id] || '')
    return length < 6 || length > 16
  })
  if (invalidTitle) {
    error.value = `“${invalidTitle.filename}”的短标题需填写 6–16 个字`
    return
  }
  const invalidCopy = props.assets.find(asset => !fullTitles.value[asset.id]?.trim() || textLength(fullTitles.value[asset.id] || '') > 200 || textLength(publishDescription(fullTitles.value[asset.id] || '', assetBodies.value[asset.id] || '', description.value)) > 1000)
  if (invalidCopy) { error.value = `“${invalidCopy.filename}”需填写完整标题（最多 200 字）；标题加正文总计不能超过 1000 字`; return }
  const annotations: ChannelsVideoAnnotationInput[] = props.assets.map(asset => ({
    asset_id: asset.id,
    annotation: assetAnnotations.value[asset.id] || defaultAnnotation(asset),
    shooting_time: annotationShootingTimes.value[asset.id]?.trim() || '',
    shooting_location: annotationShootingLocations.value[asset.id]?.trim() || '',
    repost_source: annotationRepostSources.value[asset.id]?.trim() || '',
  }))
  const incompleteSelfShot = annotations.find(item => item.annotation === 'self_shot' && (!item.shooting_time || !item.shooting_location))
  if (incompleteSelfShot) {
    error.value = '选择“内容为自行拍摄”时，必须填写这条视频的拍摄时间和地点'
    return
  }
  pushing.value = true
  error.value = ''
  try {
    const tags = tagsText.value.split(/[，,\s]+/).map(value => value.trim()).filter(Boolean).slice(0, 10)
    const covers: { asset_id: number; object_key: string; filename: string }[] = []
    const selectedCovers = props.assets.filter(asset => coverFiles.value[asset.id])
    for (let index = 0; index < selectedCovers.length; index += 1) {
      const asset = selectedCovers[index]
      const file = coverFiles.value[asset.id]!
      coverStage.value = `正在上传封面 ${index + 1}/${selectedCovers.length}`
      const session = await uploadFileMultipart(file, { assetScope: 'reference_video', category: '视频号封面' })
      covers.push({ asset_id: asset.id, object_key: session.object_key, filename: file.name })
    }
    const result = await api.channelsPush(
      props.assets.map(asset => asset.id),
      selected.value,
      '',
      description.value,
      tags,
      publishMode.value === 'product' ? selectedProduct.value : null,
      covers,
      props.assets.map(asset => ({ asset_id: asset.id, title: assetTitles.value[asset.id]?.trim() || '' })),
      annotations,
      props.assets.map(asset => ({ asset_id: asset.id, description: publishDescription(fullTitles.value[asset.id] || '', assetBodies.value[asset.id] || '', description.value) })),
    )
    if (!result.task_ids.length) {
      error.value = result.message || '这些素材已有发布或待核验记录，已阻止重复上传'
      return
    }
    emit('queued', result.task_ids.length)
  } catch (e) {
    error.value = e instanceof Error ? e.message : '视频号推送失败'
  } finally {
    coverStage.value = ''
    pushing.value = false
  }
}

watch(() => selected.value[0], () => {
  clearProductPoll()
  productRequestVersion += 1
  products.value = []
  selectedProduct.value = null
  productQuery.value = ''
  appliedProductQuery.value = ''
  productError.value = ''
  productMessage.value = ''
  productReadAt.value = ''
  productCached.value = false
  productStale.value = false
  productComplete.value = false
  productCachedTotal.value = 0
  productState.value = 'idle'
  void loadProducts()
})
onBeforeUnmount(() => { clearProductPoll(); Object.values(coverUrls.value).forEach(url => URL.revokeObjectURL(url)) })
onMounted(async () => {
  try {
    accounts.value = (await api.channelsAccounts()).items
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法读取我的视频号'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <Teleport to="body">
    <div class="qc-modal-layer" @click.self="!pushing && emit('close')">
      <section class="qc-modal channels-push-modal">
        <header>
          <div><span>WECHAT CHANNELS DELIVERY</span><h2>{{ assets.length > 1 ? '批量推送到视频号主页' : '推送到视频号主页' }}</h2><p>已选择 {{ assets.length }}/10 条原视频</p></div>
          <button class="icon-button" aria-label="关闭" :disabled="pushing" @click="emit('close')"><X /></button>
        </header>
        <div class="qc-asset-batch"><div><strong>本次素材 {{ assets.length }}/10</strong><small>每条视频、每个视频号独立生成任务；原文件不压缩、不转码</small></div><span v-for="asset in assets" :key="asset.id">{{ asset.filename }}</span></div>
        <section class="channels-cover-picker">
          <header><div><ImageIcon :size="18" /><span><strong>逐条设置发布信息</strong><small>封面只应用到对应视频，主页封面居中适配为 3:4；AI/数字人素材默认标注为“含AI生成内容”</small></span></div></header>
          <div class="channels-cover-grid">
            <article v-for="asset in assets" :key="`cover-${asset.id}`">
              <div class="channels-cover-preview"><img v-if="coverUrls[asset.id]" :src="coverUrls[asset.id]" :alt="`${asset.filename} 的自定义封面`" /><img v-else-if="asset.cover_url" :src="asset.cover_url" :alt="`${asset.filename} 当前封面`" /><ImageIcon v-else :size="24" /></div>
              <div>
                <strong :title="asset.filename">{{ asset.filename }}</strong>
                <label class="channels-video-title"><span>完整标题 · 仅这条视频</span><textarea v-model="fullTitles[asset.id]" rows="2" placeholder="填写完整标题，不用压缩到 16 字" :disabled="pushing" /><small>作为这条发布文案首行，不会被统一正文覆盖 · {{ textLength(fullTitles[asset.id] || '') }}/200</small></label>
                <label class="channels-video-title"><span>这条视频的正文（选填）</span><textarea v-model="assetBodies[asset.id]" rows="2" :disabled="pushing" placeholder="只追加到这条视频，其他素材不变" /></label>
                <label class="channels-video-title"><span>平台短标题（辅助字段，6–16 字）</span><input v-model="assetTitles[asset.id]" required placeholder="独立短标题，不作为完整标题的长度限制" :disabled="pushing" /><small>现有发布接口仍按 6–16 字校验；完整标题填在上方 · {{ textLength(assetTitles[asset.id] || '') }}/16</small></label>
                <label class="channels-video-annotation"><span>视频标注</span><select v-model="assetAnnotations[asset.id]" :disabled="pushing"><option v-for="option in annotationOptions" :key="option.value" :value="option.value">{{ option.label }}</option></select></label>
                <div v-if="assetAnnotations[asset.id] === 'self_shot'" class="channels-annotation-details"><label><span>拍摄时间</span><input v-model="annotationShootingTimes[asset.id]" type="datetime-local" :disabled="pushing" /></label><label><span>拍摄地点</span><input v-model="annotationShootingLocations[asset.id]" maxlength="255" placeholder="填写拍摄地点" :disabled="pushing" /></label></div>
                <label v-if="assetAnnotations[asset.id] === 'repost'" class="channels-video-annotation"><span>转载来源（选填）</span><input v-model="annotationRepostSources[asset.id]" maxlength="500" placeholder="填写原作者或内容来源" :disabled="pushing" /></label>
                <small>{{ coverFiles[asset.id] ? `${coverFiles[asset.id]?.name} · 仅应用这条视频` : '平台自动取帧' }}</small>
                <span><label><input type="file" accept="image/jpeg,image/png,image/webp" :disabled="pushing" @change="chooseCover(asset.id, ($event.target as HTMLInputElement).files)" />{{ coverFiles[asset.id] ? '更换封面' : '上传封面' }}</label><button v-if="coverFiles[asset.id]" type="button" :disabled="pushing" @click="clearCover(asset.id)">恢复自动</button></span>
              </div>
            </article>
          </div>
        </section>
        <div v-if="loading" class="qc-loading"><LoaderCircle class="spin" />正在读取我的视频号…</div>
        <template v-else>
          <div v-if="!accounts.length" class="notice compact"><strong>还没有可用的视频号授权</strong><span>请先到左侧“视频号主页推送”扫码授权自己的账号。</span></div>
          <div v-else class="channels-push-form">
            <label class="channels-account-field">选择视频号
              <div class="channels-account-choices">
                <button v-for="account in accounts" :key="account.id" type="button" :class="{ selected: selected.includes(account.id) }" @click="toggle(account.id)"><Check v-if="selected.includes(account.id)" :size="15" /><span>{{ account.nickname }}</span></button>
              </div>
            </label>
            <section class="channels-product-picker">
              <div class="channels-product-head">
                <div><PackageSearch :size="18" /><span><strong>关联商品</strong><small>读取所选视频号橱窗，发布时由平台校验并绑定</small></span></div>
                <form class="channels-product-search" @submit.prevent="loadProducts()"><Search :size="15" /><input v-model="productQuery" :disabled="!selected.length" placeholder="搜索商品名称或商品 ID" /><button type="submit" :disabled="!selected.length">查询</button><button type="button" :disabled="!selected.length || productLoading" @click="loadProducts(true)">刷新</button></form>
              </div>
              <div class="channels-account-choices"><button type="button" :class="{ selected: publishMode === 'product' }" :disabled="pushing" @click="publishMode = 'product'">批量挂车发布</button><button type="button" :class="{ selected: publishMode === 'plain' }" :disabled="pushing" @click="publishMode = 'plain'">不挂车发布</button></div>
              <div v-if="publishMode === 'product' && selectedProduct" class="channels-product-status"><Check :size="14" /><span>本批 {{ assets.length }} 条视频挂车：{{ selectedProduct.name }} · {{ selectedProduct.id }}</span><button type="button" :disabled="pushing" @click="selectedProduct = null">清除选择</button></div>
              <div v-if="productLoading && !products.length" class="channels-product-loading"><LoaderCircle class="spin" :size="16" /><span>{{ productStatusText }}</span></div>
              <div v-else-if="productError && !products.length" class="channels-product-error"><span>{{ productError }}</span><button type="button" @click="loadProducts(true)">重新读取</button></div>
              <div v-else-if="selected.length && products.length" class="channels-product-list">
                <button v-for="product in products" :key="product.id" type="button" :disabled="pushing" :class="{ selected: selectedProductId === product.id }" @click="selectedProduct = product; publishMode = 'product'">
                  <img v-if="product.image_url" :src="product.image_url" alt="" />
                  <span class="channels-product-image-fallback" v-else><PackageSearch :size="18" /></span>
                  <span><strong>{{ product.name }}</strong><small>商品 ID {{ product.id }}<template v-if="product.price_yuan != null"> · ¥{{ product.price_yuan }}</template></small></span>
                  <i class="channels-radio"><Check v-if="selectedProductId === product.id" :size="12" /></i>
                </button>
              </div>
              <p v-else-if="selected.length" class="channels-product-empty">当前条件未查到商品，可换商品名称或 ID 重试。</p>
              <p v-else class="channels-product-empty">请先选择视频号，再读取该账号的橱窗商品。</p>
              <div v-if="selected.length && (productStatusText || productError) && products.length" class="channels-product-status" :class="{ warning: !!productError || productStale }"><LoaderCircle v-if="productLoading" class="spin" :size="13" /><span>{{ productError || productStatusText }}</span><button v-if="productError" type="button" @click="loadProducts(true)">重新刷新</button></div>
              <small v-if="selected.length > 1" class="channels-product-multi">同时推送多个视频号时，系统会在每个账号内分别核验所选商品；任一账号没有该商品时会给出单独失败原因。</small>
            </section>
            <label>话题标签<input v-model="tagsText" :disabled="pushing" placeholder="多个标签用逗号或空格分隔，最多 10 个" /></label>
            <label class="channels-description-field">统一追加正文（可选，不覆盖标题）<textarea v-model="description" :disabled="pushing" maxlength="1000" placeholder="只追加到每条文案末尾；不修改任何一条标题" /></label>
            <details class="channels-copy-preview" open><summary>发布前逐条核对 · {{ assets.length }} 条</summary><article v-for="asset in assets" :key="`copy-${asset.id}`"><strong>{{ asset.filename }}</strong><p>{{ publishDescription(fullTitles[asset.id] || '', assetBodies[asset.id] || '', description) }}</p><small>短标题：{{ assetTitles[asset.id] }} · 封面：{{ coverFiles[asset.id]?.name || '平台自动取帧' }} · 商品：{{ publishMode === 'product' ? (selectedProduct?.name || '尚未选择') : '不挂车' }}</small></article></details>
          </div>
        </template>
        <div v-if="error" class="upload-error">{{ error }}</div>
        <div v-if="coverStage" class="upload-stage"><i></i>{{ coverStage }}</div>
        <footer><button class="secondary-button" :disabled="pushing" @click="emit('close')">取消</button><button class="primary-button" :disabled="pushing || !selected.length || (publishMode === 'product' && !selectedProduct)" @click="push"><LoaderCircle v-if="pushing" class="spin" :size="16" /><Send v-else :size="16" />{{ pushing ? '正在创建任务…' : `确认${publishMode === 'product' ? '挂车' : ''}发布 ${expected || ''} 条` }}</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.channels-cover-preview { aspect-ratio: 3/4; }
.channels-push-modal,.channels-push-modal *{box-sizing:border-box}
.channels-push-form{grid-template-columns:repeat(2,minmax(0,1fr))}
.channels-push-form>*{min-width:0;max-width:100%}
.channels-copy-preview{grid-column:1/-1}
.channels-video-title textarea,.channels-video-title input{min-width:0;max-width:100%}
@media(max-width:640px){.channels-push-modal{padding:20px 16px}.channels-push-form{grid-template-columns:minmax(0,1fr)}.channels-cover-grid{grid-template-columns:minmax(0,1fr)!important}.channels-cover-grid article{min-width:0}.channels-cover-grid article>div{min-width:0}.channels-push-modal input,.channels-push-modal textarea{font-size:16px!important}}
.channels-video-title textarea{box-sizing:border-box;width:100%;min-width:0;min-height:64px;padding:8px;border:1px solid #cbdde7;border-radius:8px;background:#fff;color:#173b57;font:inherit;resize:vertical}
.channels-copy-preview{border:1px solid #cbdde7;border-radius:10px;padding:12px;min-width:0}.channels-copy-preview summary{cursor:pointer;font-weight:600}.channels-copy-preview article{padding:12px 0;overflow-wrap:anywhere}.channels-copy-preview article+article{border-top:1px solid #cbdde7}.channels-copy-preview p{white-space:pre-wrap;line-height:1.6;margin:8px 0}.channels-copy-preview small{line-height:1.6;color:#526a7a}
</style>
