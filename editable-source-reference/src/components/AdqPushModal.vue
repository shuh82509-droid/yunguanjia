<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Building2, Check, CheckCircle2, Library, LoaderCircle, RefreshCw, Search, Send, ShieldCheck, Target, X } from 'lucide-vue-next'
import { api } from '../api'
import type { AdqAccountCatalog, AdqAdgroup, AdqStatus, AdqTarget, Asset } from '../types'

type PushMode = 'library' | 'direct'
type SharedConfig = {
  configured: boolean
  authorized: boolean
  source_account_id: string
  mdm_id: string
  max_video_mb: number
  business_units: { id: string; name: string }[]
  known_account_total: number
}

const props = defineProps<{ assets: Asset[] }>()
const emit = defineEmits<{ close: []; queued: [count: number] }>()
const config = ref<SharedConfig | null>(null)
const status = ref<AdqStatus | null>(null)
const accountCatalog = ref<AdqAccountCatalog | null>(null)
const mode = ref<PushMode>('library')
const loading = ref(true)
const loadingUnits = ref(false)
const pushing = ref(false)
const authorizing = ref(false)
const error = ref('')
const accountQuery = ref('')
const selectedAccountId = ref('')
const unitQuery = ref('')
const adgroups = ref<AdqAdgroup[]>([])
const selectedTargets = ref<AdqTarget[]>([])

const oversized = computed(() => {
  const limit = (config.value?.max_video_mb || 500) * 1024 * 1024
  return props.assets.filter(asset => !asset.size || asset.size > limit)
})
const libraryReady = computed(() => Boolean(config.value?.configured && config.value?.authorized && !oversized.value.length))
const directReady = computed(() => Boolean(
  config.value?.authorized
  && status.value?.user_authorization?.authorized
  && selectedTargets.value.length
  && !oversized.value.length,
))
const ready = computed(() => mode.value === 'library' ? libraryReady.value : directReady.value)
const accounts = computed(() => accountCatalog.value?.items || [])
const visibleAccounts = computed(() => {
  const term = accountQuery.value.trim().toLocaleLowerCase('zh-CN')
  if (!term) return accounts.value
  return accounts.value.filter(item => `${item.account_name} ${item.account_id} ${item.business_unit_name || ''}`.toLocaleLowerCase('zh-CN').includes(term))
})
const selectedAccount = computed(() => accounts.value.find(item => item.account_id === selectedAccountId.value) || null)
const selectedCountForAccount = computed(() => selectedTargets.value.filter(item => item.account_id === selectedAccountId.value).length)

const targetSelected = (item: AdqAdgroup) => selectedTargets.value.some(target => target.account_id === selectedAccountId.value && target.adgroup_id === item.adgroup_id)
const toggleTarget = (item: AdqAdgroup) => {
  if (!item.can_attach_video || !selectedAccount.value) return
  if (targetSelected(item)) {
    selectedTargets.value = selectedTargets.value.filter(target => !(target.account_id === selectedAccountId.value && target.adgroup_id === item.adgroup_id))
    return
  }
  selectedTargets.value = [...selectedTargets.value, {
    account_id: selectedAccount.value.account_id,
    account_name: selectedAccount.value.account_name,
    adgroup_id: item.adgroup_id,
    adgroup_name: item.adgroup_name,
    source_dynamic_creative_id: item.source_dynamic_creative_id,
  }]
}

const loadAdgroups = async () => {
  if (!selectedAccountId.value) { adgroups.value = []; return }
  loadingUnits.value = true
  error.value = ''
  try {
    const result = await api.adqAdgroups(selectedAccountId.value, unitQuery.value)
    adgroups.value = result.items
  } catch (cause) {
    adgroups.value = []
    error.value = cause instanceof Error ? cause.message : '无法读取该账户的营销单元'
  } finally { loadingUnits.value = false }
}

let unitSearchTimer: ReturnType<typeof setTimeout> | undefined
watch(unitQuery, () => {
  if (unitSearchTimer) clearTimeout(unitSearchTimer)
  unitSearchTimer = setTimeout(() => void loadAdgroups(), 280)
})
watch(selectedAccountId, () => { unitQuery.value = ''; void loadAdgroups() })

const push = async () => {
  if (!ready.value || pushing.value) return
  pushing.value = true
  error.value = ''
  try {
    const assetIds = props.assets.map(asset => asset.id)
    const result = mode.value === 'library'
      ? await api.adqSharedUpload(assetIds)
      : await api.adqPush(assetIds, selectedTargets.value)
    emit('queued', result.task_ids.length)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : mode.value === 'library' ? '无法上传到 ADQ 素材库' : '无法创建 ADQ 营销单元推送任务'
  } finally { pushing.value = false }
}

const authorizeUser = async () => {
  if (authorizing.value || !status.value?.can_authorize_user) return
  authorizing.value = true
  error.value = ''
  try {
    const result = await api.adqUserAuthorizationStart()
    window.location.assign(result.url)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法发起 ADQ 操作人实名认证'
    authorizing.value = false
  }
}

onMounted(async () => {
  try {
    const [shared, catalog, statusData] = await Promise.all([api.adqSharedConfig(), api.adqAccounts(), api.adqStatus()])
    config.value = shared
    accountCatalog.value = catalog
    status.value = statusData
    selectedAccountId.value = catalog.items[0]?.account_id || ''
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '无法读取 ADQ 授权与账户状态' }
  finally { loading.value = false }
})
</script>

<template>
  <Teleport to="body">
    <div class="qc-modal-layer" @click.self="!pushing && emit('close')">
      <section class="qc-modal adq-library-modal adq-push-modal" role="dialog" aria-modal="true" aria-label="腾讯 ADQ 推送">
        <header>
          <div><span>TENCENT ADQ DELIVERY</span><h2>推送到腾讯 ADQ</h2><p>保留素材库上传，也可把原视频加入已存在、具备视频创意模板的营销单元。</p></div>
          <button class="icon-button" aria-label="关闭" :disabled="pushing" @click="emit('close')"><X /></button>
        </header>

        <div v-if="loading" class="qc-loading"><LoaderCircle class="spin" />正在读取 ADQ 授权与账户…</div>
        <template v-else>
          <div class="adq-push-modes" role="tablist" aria-label="选择推送方式">
            <button :class="{ active: mode === 'library' }" role="tab" :aria-selected="mode === 'library'" @click="mode = 'library'"><Library :size="20" /><span><strong>上传素材库</strong><small>公司主体全账户共享</small></span><Check v-if="mode === 'library'" :size="18" /></button>
            <button :class="{ active: mode === 'direct' }" role="tab" :aria-selected="mode === 'direct'" @click="mode = 'direct'"><Target :size="20" /><span><strong>推送营销单元</strong><small>复用现有视频创意模板</small></span><Check v-if="mode === 'direct'" :size="18" /></button>
          </div>

          <section v-if="mode === 'library'" class="adq-library-route">
            <span><Library :size="22" /></span>
            <div><small>上传位置</small><strong>ADQ 统一素材源账户 {{ config?.source_account_id || '未配置' }}</strong><p>原视频逐字节上传，不压缩、不转码；后续按视频素材汇总回流消耗、播放、点击、成交与 ROI。</p></div>
            <CheckCircle2 v-if="libraryReady" class="adq-library-ready" :size="22" />
          </section>

          <section v-else class="adq-direct-route">
            <div class="adq-business-unit-note"><Building2 :size="20" /><div><strong>{{ config?.business_units?.map(item => item.name).join('、') || 'WIS 业务单元' }}</strong><span>已登记 {{ config?.known_account_total ?? accountCatalog?.configured_total ?? '—' }} 个账户；业务单元归属待腾讯目录接口确认，只显示本次真实可读账户。</span></div></div>
            <div class="adq-real-route" aria-label="腾讯 ADQ 真实直投路径">
              <span><b>1</b>目标账户素材库</span><i>→</i><span><b>2</b>新建视频创意</span><i>→</i><span><b>3</b>加入营销单元</span><i>→</i><span><b>4</b>平台回读确认</span>
            </div>
            <div class="adq-user-authorization" :class="{ ready: status?.user_authorization?.authorized }">
              <ShieldCheck :size="20" />
              <div><strong>{{ status?.user_authorization?.authorized ? '操作人实名认证可用' : '直投前需完成操作人实名认证' }}</strong><span>{{ status?.user_authorization?.message }}</span></div>
              <button v-if="!status?.user_authorization?.authorized && status?.can_authorize_user" :disabled="authorizing" @click="authorizeUser"><LoaderCircle v-if="authorizing" class="spin" :size="15" /><ShieldCheck v-else :size="15" />{{ authorizing ? '正在跳转…' : '完成操作人认证' }}</button>
              <small v-else-if="!status?.user_authorization?.authorized">请联系素材管理员完成认证</small>
            </div>
            <div class="adq-direct-controls">
              <label><span>广告账户</span><select v-model="selectedAccountId"><option v-for="account in visibleAccounts" :key="account.account_id" :value="account.account_id">{{ account.account_name }} · {{ account.account_id }}</option></select></label>
              <label><span>筛选账户</span><div class="adq-inline-search"><Search :size="15" /><input v-model="accountQuery" placeholder="名称或账户 ID" /></div></label>
              <label><span>搜索营销单元</span><div class="adq-inline-search"><Search :size="15" /><input v-model="unitQuery" placeholder="名称或营销单元 ID" /></div></label>
            </div>
            <div class="adq-account-readback"><ShieldCheck :size="17" /><span>{{ accountCatalog?.message || '正在使用已验证账户范围' }}</span><strong>{{ accounts.length }} 个可读账户</strong></div>
            <div v-if="loadingUnits" class="qc-loading compact"><LoaderCircle class="spin" />正在读取营销单元与视频模板…</div>
            <div v-else class="adq-adgroup-list">
              <button v-for="item in adgroups" :key="item.adgroup_id" :disabled="!item.can_attach_video" :class="{ selected: targetSelected(item) }" @click="toggleTarget(item)">
                <span><strong>{{ item.adgroup_name }}</strong><small>单元 {{ item.adgroup_id }} · 计划 {{ item.campaign_id || '待回读' }}</small></span><b>{{ item.can_attach_video ? (targetSelected(item) ? '已选择' : '可直投') : '缺少视频模板' }}</b><CheckCircle2 v-if="targetSelected(item)" :size="18" />
              </button>
              <div v-if="!adgroups.length" class="adq-adgroup-empty">当前账户没有读取到可显示的营销单元；可切换账户或刷新搜索。</div>
            </div>
            <div class="adq-direct-selection"><span>当前账户已选 {{ selectedCountForAccount }} 个</span><strong>合计 {{ selectedTargets.length }} 个营销单元</strong><button :disabled="loadingUnits" @click="loadAdgroups"><RefreshCw :size="14" />刷新当前账户</button></div>
          </section>

          <div class="qc-asset-batch">
            <div><strong>本次素材 {{ assets.length }}/10</strong><small>单条上限 {{ config?.max_video_mb || 500 }}MB</small></div>
            <span v-for="asset in assets" :key="asset.id" :class="{ danger: oversized.some(item => item.id === asset.id) }">{{ asset.filename }}</span>
          </div>

          <div v-if="!config?.authorized" class="notice compact"><ShieldCheck :size="18" /><strong>ADQ 投放授权暂不可用</strong><span>请恢复统一素材源/投放授权后再试。</span></div>
          <div v-else-if="mode === 'library' && !config?.configured" class="notice compact"><ShieldCheck :size="18" /><strong>素材库配置暂不可用</strong><span>请检查统一素材源账户与主体配置。</span></div>
          <div v-else-if="oversized.length" class="notice compact"><strong>有素材超过上传上限</strong><span>{{ oversized.map(item => item.filename).join('、') }}；系统不会压缩或转码。</span></div>
          <div v-else-if="mode === 'direct' && !status?.user_authorization?.authorized" class="notice compact"><ShieldCheck :size="18" /><strong>尚不能写入营销单元</strong><span>素材库上传不受影响；完成操作人实名认证后，可继续复用已经上传的原视频。</span></div>
          <div v-else-if="mode === 'direct' && !selectedTargets.length" class="notice compact"><Target :size="18" /><strong>请选择营销单元</strong><span>只可选择已经存在且具备视频创意模板的单元；不会新建计划或修改预算。</span></div>
          <div v-if="error" class="upload-error">{{ error }}</div>
        </template>

        <footer><button class="secondary-button" :disabled="pushing" @click="emit('close')">取消</button><button class="primary-button" :disabled="loading || pushing || !ready" @click="push"><LoaderCircle v-if="pushing" class="spin" :size="16" /><Send v-else :size="16" />{{ pushing ? '正在创建推送任务…' : mode === 'library' ? `确认上传 ${assets.length} 条` : `推送 ${assets.length} 条到 ${selectedTargets.length} 个单元` }}</button></footer>
      </section>
    </div>
  </Teleport>
</template>
