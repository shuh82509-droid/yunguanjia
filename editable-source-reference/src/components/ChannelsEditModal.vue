<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { Image as ImageIcon, LoaderCircle, Search, X } from 'lucide-vue-next'
import { api } from '../api'
import { uploadFileMultipart } from '../utils/multipartUpload'
import type { ChannelsProduct, ChannelsTask } from '../types'
const props = defineProps<{ task: ChannelsTask }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const title = ref(props.task.title)
const description = ref(props.task.description)
const product = ref<{ id: string; name: string } | null>(props.task.product_id ? { id: props.task.product_id, name: props.task.product_name } : null)
const query = ref('')
const products = ref<ChannelsProduct[]>([])
const productMessage = ref('')
const searching = ref(false)
const cover = ref<File | null>(null)
const preview = ref(props.task.cover_url || '')
const resetCover = ref(false)
const busy = ref(false)
const error = ref('')
const stage = ref('')
let objectUrl = ''
const chooseCover = (files: FileList | null) => {
  const file = files?.[0]
  if (!file) return
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || !file.size || file.size > 20 * 1024 ** 2) {
    error.value = '封面支持 JPG、PNG、WEBP，最大 20MB'; return
  }
  if (objectUrl) URL.revokeObjectURL(objectUrl)
  cover.value = file; objectUrl = URL.createObjectURL(file); preview.value = objectUrl; resetCover.value = false
}
const restore = () => { cover.value = null; preview.value = ''; resetCover.value = true }
const search = async () => {
  searching.value = true
  try {
    const result = await api.channelsProducts(props.task.account_id, query.value)
    products.value = result.items; productMessage.value = result.error || result.message || (result.items.length ? '' : '未找到商品')
  } catch (e) { productMessage.value = e instanceof Error ? e.message : '商品读取失败' }
  finally { searching.value = false }
}
const save = async (retry: boolean) => {
  if (busy.value) return
  if (title.value.trim().length < 6 || title.value.trim().length > 16) { error.value = '短标题需填写 6–16 个字'; return }
  busy.value = true; error.value = ''
  try {
    let coverObjectKey: string | undefined = resetCover.value ? '' : undefined
    if (cover.value) {
      stage.value = '正在保存封面…'
      coverObjectKey = (await uploadFileMultipart(cover.value, { assetScope: 'reference_video', category: '视频号封面' })).object_key
    }
    stage.value = retry ? '正在保存并重新排队…' : '正在保存…'
    await api.channelsEdit(props.task.id, { title: title.value.trim(), description: description.value,
      product_id: product.value?.id || '', product_name: product.value?.name || '',
      cover_object_key: coverObjectKey, cover_filename: cover.value?.name || '', retry })
    emit('saved')
  } catch (e) { error.value = e instanceof Error ? e.message : '保存失败' }
  finally { busy.value = false; stage.value = '' }
}
onBeforeUnmount(() => { if (objectUrl) URL.revokeObjectURL(objectUrl) })
</script>

<template>
  <Teleport to="body">
    <div class="qc-modal-layer" @click.self="!busy && emit('close')">
      <section class="qc-modal channels-edit-modal" role="dialog" aria-modal="true" aria-label="修改发布信息">
        <header><div><span>WECHAT CHANNELS</span><h2>修改发布信息</h2><p>{{ task.account_name }} · {{ task.asset_name }}</p></div><button class="icon-button" :disabled="busy" aria-label="关闭" @click="emit('close')"><X /></button></header>
        <div class="channels-edit-body">
          <label>短标题（6–16 个字）<input v-model="title" maxlength="16" :disabled="busy" /></label>
          <div class="channels-edit-cover"><img v-if="preview" :src="preview" alt="待发布封面预览" /><ImageIcon v-else :size="42" /><div><label>替换封面<input type="file" accept="image/jpeg,image/png,image/webp" :disabled="busy" @change="chooseCover(($event.target as HTMLInputElement).files)" /></label><button class="secondary-button" :disabled="busy" @click="restore">使用视频自动封面</button><small>{{ cover?.name || (resetCover ? '自动封面' : task.cover_filename || '自动封面') }}</small></div></div>
          <label>正文<textarea v-model="description" maxlength="1000" :disabled="busy" /></label>
          <div class="channels-product-status"><span>{{ product ? `挂车商品：${product.name} · ${product.id}` : '本条不挂车' }}</span><button v-if="product" :disabled="busy" @click="product = null">取消挂车</button></div>
          <form class="channels-product-search" @submit.prevent="search"><Search :size="16" /><input v-model="query" :disabled="busy" placeholder="搜索该视频号橱窗商品" /><button :disabled="searching || busy">{{ searching ? '读取中…' : '查询商品' }}</button></form>
          <small v-if="productMessage">{{ productMessage }}</small>
          <div v-if="products.length" class="channels-product-list"><button v-for="item in products" :key="item.id" :disabled="busy" :class="{ selected: product?.id === item.id }" @click="product = item"><span><strong>{{ item.name }}</strong><small>{{ item.id }}</small></span></button></div>
          <small>视频标注：{{ task.video_annotation_label }}。保存后可继续修改；“保存并重试”将使用以上信息重新排队。</small>
        </div>
        <div v-if="error" class="upload-error">{{ error }}</div><div v-if="stage" class="upload-stage">{{ stage }}</div>
        <footer><button class="secondary-button" :disabled="busy" @click="save(false)">仅保存</button><button class="primary-button" :disabled="busy" @click="save(true)"><LoaderCircle v-if="busy" class="spin" :size="16" />保存并重试</button></footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.channels-edit-modal { max-width: 720px; }
.channels-edit-body { display: grid; gap: 16px; padding: 8px 0; }
.channels-edit-body label { display: grid; gap: 7px; font-weight: 600; }
.channels-edit-body input,.channels-edit-body textarea { width: 100%; padding: 10px; border: 1px solid #bed8e8; border-radius: 9px; }
.channels-edit-body textarea { min-height: 82px; }
.channels-edit-cover { display:flex; align-items:center; gap: 18px; }
.channels-edit-cover img { width: 96px; height: 128px; object-fit: cover; background: #eef6fa; border-radius: 9px; }
.channels-edit-cover div { display:grid; gap: 10px; }
</style>
