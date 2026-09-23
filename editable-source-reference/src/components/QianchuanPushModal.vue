<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { Check, Link2, LoaderCircle, Plus, RefreshCw, Send, ShieldCheck, Sparkles, Star, Trash2, X } from 'lucide-vue-next'
import { api } from '../api'
import { openAuthorizationWindow } from '../authorization-window'
import type { Asset, PushPreference, PushScheme, QianchuanAccount, QianchuanPlan, QianchuanProductPlanMap, QianchuanProductPlanRule, QianchuanStatus, QianchuanTarget } from '../types'

const props = defineProps<{ assets: Asset[] }>()
const emit = defineEmits<{ close: []; queued: [count: number] }>()

const status = ref<QianchuanStatus | null>(null)
const accounts = ref<QianchuanAccount[]>([])
const preferences = ref<PushPreference[]>([])
const schemes = ref<PushScheme[]>([])
const schemeId = ref('')
const schemeName = ref('')
const schemeNotice = ref('')
const schemeSaving = ref(false)
const loadSchemes = async () => {
  try { schemes.value = (await api.pushSchemes()).items }
  catch (e) { schemeNotice.value = e instanceof Error ? e.message : '个人方案读取失败，可重试' }
}
const saveScheme = async () => {
  if (schemeSaving.value || bundleLoading.value || pushing.value || !schemeName.value.trim() || !pushTargetCount.value) return
  schemeSaving.value = true
  try {
    const saved = await api.savePushScheme({ name: schemeName.value.trim(), targets: allPushTargets.value })
    schemes.value = [saved, ...schemes.value]; schemeId.value = saved.id; schemeName.value = ''
    schemeNotice.value = '已保存到本人账号。使用时会重新核验计划，不会自动推送。'
  } catch (e) { schemeNotice.value = e instanceof Error ? e.message : '保存失败，当前选择已保留' }
  finally { schemeSaving.value = false }
}
const deleteScheme = async () => {
  const chosen = schemes.value.find(item => item.id === schemeId.value)
  if (!chosen || schemeSaving.value || bundleLoading.value || pushing.value) return
  schemeSaving.value = true
  try {
    await api.deletePushScheme(chosen.id, chosen.revision)
    schemes.value = schemes.value.filter(item => item.id !== chosen.id); schemeId.value = ''
    schemeNotice.value = '已删除个人方案，当前所选计划和推送任务不受影响。'
  } catch (e) { schemeNotice.value = e instanceof Error ? e.message : '删除失败' }
  finally { schemeSaving.value = false }
}
const applyScheme = async () => {
  const chosen = schemes.value.find(item => item.id === schemeId.value)
  if (!chosen || bundleLoading.value || pushing.value || schemeSaving.value) return
  bundleLoading.value = true
  const accepted: QianchuanTarget[] = []; const skipped: string[] = []
  const accountReads = new Map<string, Promise<Awaited<ReturnType<typeof api.qianchuanPlans>>>>()
  try {
    for (const target of chosen.targets) {
      const account = accounts.value.find(item => item.id === target.advertiser_id)
      if (!account) { skipped.push(`${target.advertiser_name}：账户当前不可用`); continue }
      try {
        if (!accountReads.has(target.advertiser_id)) accountReads.set(target.advertiser_id, api.qianchuanPlans(target.advertiser_id, '', 'all', false))
        const result = await accountReads.get(target.advertiser_id)!
        const plan = result.items.find(item => item.id === target.plan_id && item.plan_type === target.plan_type)
        if (result.cached || !plan?.can_attach_video) { skipped.push(`${target.plan_name}：未取得当前可推送状态`); continue }
        accepted.push({ ...target, advertiser_name: account.name, plan_name: plan.name })
      } catch { skipped.push(`${target.plan_name}：读取未完成，未加入`) }
    }
    const combined = [...targets.value, ...accepted].filter((item, index, all) => all.findIndex(other => other.advertiser_id === item.advertiser_id && other.plan_type === item.plan_type && other.plan_id === item.plan_id) === index)
    if (combined.length + currentSelectedTargets.value.filter(item => !combined.some(other => other.advertiser_id === item.advertiser_id && other.plan_id === item.plan_id)).length > 50) {
      schemeNotice.value = '合并后超过50个计划，当前选择未变。请先清空或减少已选计划。'; return
    }
    targets.value = combined
    schemeNotice.value = `方案核验通过 ${accepted.length} 个，未加入 ${skipped.length} 个。${skipped.join('；')} 请核对产品后再确认创建任务。`
  } finally { bundleLoading.value = false }
}

const productPlanMap = ref<QianchuanProductPlanMap | null>(null)
const plans = ref<QianchuanPlan[]>([])
const accountId = ref('')
const selectedProductKey = ref('')
const selectedPlanKeys = ref<string[]>([])
const accountKeyword = ref('')
const planKeyword = ref('')
const planTypeFilter = ref('')
const targets = ref<QianchuanTarget[]>([])
const loading = ref(true)
const loadingPlans = ref(false)
const loadingMorePlans = ref(false)
const bundleLoading = ref(false)
const bundleProgress = ref('')
const bundleSummary = ref('')
const bundleWarnings = ref<string[]>([])
const pushing = ref(false)
const error = ref('')
const authorizing = ref(false)
const authorizationNotice = ref('')
const planWarnings = ref<string[]>([])
const planCounts = ref({ total: 0, multiplication: 0, full_domain: 0, standard: 0, full: 0 })
const planSourceReadAt = ref('')
const plansCached = ref(false)
const plansComplete = ref(true)
let planRequestSequence = 0
let userTouchedPlans = false
const cancelledPlanRequest = new Error('cancelled plan request')

const selectedAccount = computed(() => accounts.value.find(item => item.id === accountId.value))
const preferenceRank = (item?: PushPreference) => [item?.pinned ? 1 : 0, item?.last_used_at ? new Date(item.last_used_at).getTime() : 0, item?.use_count || 0]
const comparePreference = (left?: PushPreference, right?: PushPreference) => {
  const a = preferenceRank(left); const b = preferenceRank(right)
  return b[0] - a[0] || b[1] - a[1] || b[2] - a[2]
}
const accountPreference = (id: string) => preferences.value.find(item => item.account_id === id && !item.target_id)
const targetPreference = (account: string, target: string) => preferences.value.find(item => item.account_id === account && item.target_id === target)
const filteredAccounts = computed(() => {
  const keyword = accountKeyword.value.trim().toLowerCase()
  const matches = keyword ? accounts.value.filter(item => `${item.name} ${item.id}`.toLowerCase().includes(keyword)) : accounts.value
  return [...matches].sort((a, b) => comparePreference(accountPreference(a.id), accountPreference(b.id)))
})
const filteredPlans = computed(() => {
  const keyword = planKeyword.value.trim().toLowerCase()
  return plans.value.filter(item => {
    const matchesType = !planTypeFilter.value || item.plan_type === planTypeFilter.value
    const matchesKeyword = !keyword || `${item.name} ${item.id} ${item.plan_type_label}`.toLowerCase().includes(keyword)
    return matchesType && matchesKeyword
  }).sort((a, b) => comparePreference(targetPreference(accountId.value, a.id), targetPreference(accountId.value, b.id)))
})
const selectableFilteredPlans = computed(() => filteredPlans.value.filter(item => item.can_attach_video))
const fullPlanCount = computed(() => plans.value.filter(item => item.is_full).length)
const allFilteredSelected = computed(() => selectableFilteredPlans.value.length > 0 && selectableFilteredPlans.value.every(item => selectedPlanKeys.value.includes(`${item.plan_type}:${item.id}`)))
const planFreshnessLabel = computed(() => {
  if (!planSourceReadAt.value) return ''
  const parsed = new Date(planSourceReadAt.value)
  if (Number.isNaN(parsed.getTime())) return planSourceReadAt.value
  return parsed.toLocaleString('zh-CN', { hour12: false })
})
const currentSelectedTargets = computed<QianchuanTarget[]>(() => {
  if (!selectedAccount.value) return []
  return plans.value
    .filter(plan => plan.can_attach_video && selectedPlanKeys.value.includes(`${plan.plan_type}:${plan.id}`))
    .map(plan => ({
      advertiser_id: selectedAccount.value!.id,
      advertiser_name: selectedAccount.value!.name,
      plan_id: plan.id,
      plan_name: plan.name,
      plan_type: plan.plan_type,
    }))
})
const allPushTargets = computed(() => {
  const combined = [...targets.value, ...currentSelectedTargets.value]
  return combined.filter((target, index) => combined.findIndex(item => item.advertiser_id === target.advertiser_id && item.plan_id === target.plan_id) === index).slice(0, 50)
})
const pushTargetCount = computed(() => allPushTargets.value.length)
const expectedTaskCount = computed(() => props.assets.length * pushTargetCount.value)
const assetSummary = computed(() => props.assets.length === 1 ? props.assets[0].filename : `已选择 ${props.assets.length} 条视频素材`)

const normalizedProductText = (value: string) => value.toLowerCase().replace(/[\s_\-—–()（）]+/g, '')
const productKeyForAsset = (asset: Asset, bundles = productPlanMap.value?.items || []) => {
  const haystack = normalizedProductText(`${asset.category || ''} ${asset.filename || ''}`)
  return bundles.find(bundle => bundle.aliases.some(alias => haystack.includes(normalizedProductText(alias))))?.key || ''
}
const detectedProductKeys = computed(() => [...new Set(props.assets.map(asset => productKeyForAsset(asset)).filter(Boolean))])
const mixedProductBatch = computed(() => detectedProductKeys.value.length > 1)
const selectedProductBundle = computed(() => productPlanMap.value?.items.find(item => item.key === selectedProductKey.value) || null)
const productDetectionLabel = computed(() => {
  if (!productPlanMap.value) return ''
  if (mixedProductBatch.value) {
    const labels = detectedProductKeys.value.map(key => productPlanMap.value!.items.find(item => item.key === key)?.label || key)
    return `本批次识别到多个产品：${labels.join('、')}，请分产品批量推送。`
  }
  if (selectedProductBundle.value) return `已按素材分类识别：${selectedProductBundle.value.label}`
  return '未从素材分类识别到产品，可手动选择产品后匹配。'
})

const replacePreference = (updated: PushPreference) => {
  preferences.value = [...preferences.value.filter(item => item.id !== updated.id), updated]
}

const toggleAccountPreference = async () => {
  if (!selectedAccount.value) return
  try {
    replacePreference(await api.togglePushPreference('qianchuan', {
      account_id: selectedAccount.value.id,
      account_name: selectedAccount.value.name,
    }))
  } catch (e) { error.value = e instanceof Error ? e.message : '常用账户设置失败' }
}

const togglePlanPreference = async (plan: QianchuanPlan) => {
  if (!selectedAccount.value) return
  try {
    replacePreference(await api.togglePushPreference('qianchuan', {
      account_id: selectedAccount.value.id,
      account_name: selectedAccount.value.name,
      target_id: plan.id,
      target_name: plan.name,
      target_type: plan.plan_type,
    }))
  } catch (e) { error.value = e instanceof Error ? e.message : '常用计划设置失败' }
}

const restoreRecentPlans = () => {
  if (!accountId.value || pushing.value || bundleLoading.value) return
  const recent = preferences.value
    .filter(item => item.account_id === accountId.value && item.target_id && item.last_used_at)
    .sort((a, b) => comparePreference(a, b))
  if (!recent.length) return
  const latest = Math.max(...recent.map(item => new Date(item.last_used_at!).getTime()))
  const recentIds = new Set(recent.filter(item => latest - new Date(item.last_used_at!).getTime() <= 5000).map(item => item.target_id))
  const keys = plans.value.filter(plan => plan.can_attach_video && recentIds.has(plan.id)).map(plan => `${plan.plan_type}:${plan.id}`)
  userTouchedPlans = true
  if (keys.length) selectedPlanKeys.value = Array.from(new Set([...selectedPlanKeys.value, ...keys])).slice(0, 50)
}

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    void loadSchemes()
    status.value = await api.qianchuanStatus()
    if (status.value.authorized) {
      const [accountResult, preferenceResult, productMapResult] = await Promise.all([
        api.qianchuanAccounts(),
        api.pushPreferences('qianchuan'),
        api.qianchuanProductPlanMap(),
      ])
      accounts.value = accountResult.items
      preferences.value = preferenceResult.items
      productPlanMap.value = productMapResult
      const detected = [...new Set(props.assets.map(asset => productKeyForAsset(asset, productMapResult.items)).filter(Boolean))]
      selectedProductKey.value = detected.length === 1 ? detected[0] : ''
      const preferred = [...accounts.value].sort((a, b) => comparePreference(accountPreference(a.id), accountPreference(b.id)))[0]
      accountId.value = preferred?.id || ''
    }
  } catch (e) {
    error.value = e instanceof Error ? e.message : '无法读取千川授权'
  } finally { loading.value = false }
}

const countPlans = (items: QianchuanPlan[]) => ({
  total: items.length,
  multiplication: items.filter(item => item.plan_type === 'multiplication').length,
  full_domain: items.filter(item => item.plan_type === 'full_domain').length,
  standard: items.filter(item => item.plan_type === 'standard').length,
  full: items.filter(item => item.is_full).length,
})

const mergePlans = (incoming: QianchuanPlan[]) => {
  const merged = new Map(plans.value.map(item => [`${item.plan_type}:${item.id}`, item]))
  incoming.forEach(item => merged.set(`${item.plan_type}:${item.id}`, item))
  plans.value = [...merged.values()]
  planCounts.value = countPlans(plans.value)
}

const applyPlanResult = (result: Awaited<ReturnType<typeof api.qianchuanPlans>>) => {
  mergePlans(result.items)
  planWarnings.value = [...new Set([...planWarnings.value, ...(result.warnings || [])])]
  planSourceReadAt.value = result.source_read_at || planSourceReadAt.value
  plansCached.value = plansCached.value || Boolean(result.cached)
  plansComplete.value = result.complete
}

const wait = (milliseconds: number) => new Promise(resolve => window.setTimeout(resolve, milliseconds))

const planMatchesBundleRule = (plan: QianchuanPlan, rule: QianchuanProductPlanRule) => {
  if (rule.match_mode === 'exact_plan') return plan.id === rule.plan_id
  if (rule.match_mode === 'all') return true
  const keyword = rule.keyword.trim().toLowerCase()
  return Boolean(keyword) && `${plan.name} ${plan.id}`.toLowerCase().includes(keyword)
}

const fetchBundleRulePlans = async (rule: QianchuanProductPlanRule) => {
  const query = rule.match_mode === 'exact_plan' ? rule.plan_id : rule.match_mode === 'keyword' ? rule.keyword : ''
  let lastError: unknown
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      return await api.qianchuanPlans(rule.advertiser_id, query, rule.scope, false)
    } catch (e) {
      lastError = e
      const statusCode = Number((e as { status?: number } | null)?.status || 0)
      if (attempt >= 2 || (statusCode > 0 && statusCode < 500)) break
      await wait(500 * (2 ** attempt))
    }
  }
  try {
    return await api.qianchuanPlans(rule.advertiser_id, query, rule.scope, false, true)
  } catch {
    throw lastError
  }
}

const applyProductBundle = async () => {
  if (mixedProductBatch.value) {
    error.value = '本批次包含多个产品。为避免素材进入错误计划，请先按产品分批选择素材。'
    return
  }
  const bundle = selectedProductBundle.value
  if (!bundle) {
    error.value = '请先选择次抛、喷雾、黑晶等产品'
    return
  }

  bundleLoading.value = true
  bundleSummary.value = ''
  bundleWarnings.value = []
  error.value = ''

  const additions: QianchuanTarget[] = []
  const warnings: string[] = []
  let blockedCount = 0
  for (const [index, rule] of bundle.rules.entries()) {
    bundleProgress.value = `正在匹配 ${index + 1}/${bundle.rules.length}：${rule.advertiser_name}`
    const account = accounts.value.find(item => item.id === rule.advertiser_id)
    if (!account) {
      warnings.push(`${rule.advertiser_name}（${rule.advertiser_id}）不在当前授权账户中`)
      continue
    }
    try {
      const result = await fetchBundleRulePlans(rule)
      const candidates = result.items.filter(plan => planMatchesBundleRule(plan, rule))
      const selectable = candidates.filter(plan => plan.can_attach_video && !plan.is_full)
      blockedCount += candidates.length - selectable.length
      if (!candidates.length) {
        warnings.push(`${account.name} 未找到${rule.plan_id ? `计划 ${rule.plan_id}` : `含“${rule.keyword}”的计划`}`)
        continue
      }
      if (!selectable.length) {
        warnings.push(`${account.name} 的匹配计划均已满或当前仅可查询`)
        continue
      }
      additions.push(...selectable.map(plan => ({
        advertiser_id: account.id,
        advertiser_name: account.name,
        plan_id: plan.id,
        plan_name: plan.name,
        plan_type: plan.plan_type,
      })))
    } catch (e) {
      warnings.push(`${account.name} 读取失败：${e instanceof Error ? e.message : '请稍后重试'}`)
    }
  }

  const unique = additions.filter((target, index) => additions.findIndex(item => item.advertiser_id === target.advertiser_id && item.plan_id === target.plan_id) === index)
  // Product matching is a safe replacement operation: remembered/manual plan
  // selections must not leak into a different product's one-click target list.
  selectedPlanKeys.value = []
  targets.value = unique.slice(0, 50)
  const matchedPlanCount = targets.value.length
  const matchedAccountCount = new Set(additions.map(item => item.advertiser_id)).size
  if (unique.length > 50) warnings.push(`一次最多 50 个计划，超出的 ${unique.length - 50} 个未加入`)
  bundleSummary.value = matchedPlanCount
    ? `已按“${bundle.label}”匹配 ${matchedAccountCount} 个账户、用 ${matchedPlanCount} 个计划替换当前目标清单；请在下方确认后再创建任务。${blockedCount ? `另有 ${blockedCount} 个已满或仅查询计划未加入。` : ''}`
    : `“${bundle.label}”本次没有可推送计划，已清空当前目标清单，请查看提示后重试。`
  bundleWarnings.value = [...new Set(warnings)]
  bundleProgress.value = ''
  bundleLoading.value = false
}

const fetchPlansWithRetry = async (
  advertiserId: string,
  scope: 'multiplication' | 'all',
  requestSequence: number,
  forceRefresh: boolean,
) => {
  let lastError: unknown
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (requestSequence !== planRequestSequence) throw cancelledPlanRequest
    try {
      // An automatic retry should reuse a result that the preceding request may
      // already have placed in the server cache.
      return await api.qianchuanPlans(advertiserId, '', scope, forceRefresh)
    } catch (e) {
      lastError = e
      const statusCode = Number((e as { status?: number } | null)?.status || 0)
      if (attempt >= 2 || (statusCode > 0 && statusCode < 500)) break
      await wait(700 * (2 ** attempt))
    }
  }
  if (requestSequence !== planRequestSequence) throw cancelledPlanRequest
  try {
    return await api.qianchuanPlans(advertiserId, '', scope, false, true)
  } catch {
    // Preserve the original live-read error if no cached result is available.
  }
  throw lastError
}

const loadPlansForAccount = async (value: string, reset: boolean, forceRefresh = false) => {
  const requestSequence = ++planRequestSequence
  if (reset) {
    selectedPlanKeys.value = []
    planKeyword.value = ''
    planTypeFilter.value = ''
    plans.value = []
    planCounts.value = { total: 0, multiplication: 0, full_domain: 0, standard: 0, full: 0 }
    planSourceReadAt.value = ''
    plansCached.value = false
  }
  planWarnings.value = []
  plansComplete.value = true
  if (forceRefresh) plansCached.value = false
  loadingPlans.value = !plans.value.length
  loadingMorePlans.value = Boolean(plans.value.length)
  error.value = ''

  if (!forceRefresh) {
    try {
      const multiplication = await fetchPlansWithRetry(value, 'multiplication', requestSequence, false)
      if (requestSequence !== planRequestSequence) return
      applyPlanResult(multiplication)
      if (reset && multiplication.counts.multiplication > 0) planTypeFilter.value = 'multiplication'
    } catch (e) {
      if (e === cancelledPlanRequest || requestSequence !== planRequestSequence) return
      planWarnings.value = [e instanceof Error ? `乘方计划优先读取未完成：${e.message}` : '乘方计划优先读取未完成']
      plansComplete.value = false
    } finally {
      if (requestSequence === planRequestSequence) loadingPlans.value = false
    }
  } else {
    loadingPlans.value = false
  }

  if (requestSequence !== planRequestSequence) return
  loadingMorePlans.value = true
  try {
    const allPlans = await fetchPlansWithRetry(value, 'all', requestSequence, forceRefresh)
    if (requestSequence !== planRequestSequence) return
    applyPlanResult(allPlans)
  } catch (e) {
    if (e === cancelledPlanRequest || requestSequence !== planRequestSequence) return
    const message = e instanceof Error ? e.message : '其他计划读取未完成'
    plansComplete.value = false
    if (plans.value.length) {
      plansCached.value = true
      planWarnings.value = [...new Set([...planWarnings.value, `其他计划后台读取未完成：${message}`])]
    } else {
      error.value = message
    }
  } finally {
    if (requestSequence === planRequestSequence) loadingMorePlans.value = false
  }
}

const clearTargets = () => {
  if (pushing.value || bundleLoading.value) return
  selectedPlanKeys.value = []
  targets.value = []
  userTouchedPlans = true
  bundleSummary.value = ''
  error.value = ''
}

const refreshPlans = () => {
  if (accountId.value) void loadPlansForAccount(accountId.value, false, true)
}

watch(accountId, (value, previousValue) => {
  if (previousValue && selectedPlanKeys.value.length) addSelectedTargets(previousValue)
  if (!value) {
    planRequestSequence += 1
    plans.value = []
    return
  }
  userTouchedPlans = false
  void loadPlansForAccount(value, true)
})

const togglePlan = (plan: QianchuanPlan) => {
  if (!plan.can_attach_video) return
  userTouchedPlans = true
  const key = `${plan.plan_type}:${plan.id}`
  if (selectedPlanKeys.value.includes(key)) {
    selectedPlanKeys.value = selectedPlanKeys.value.filter(item => item !== key)
    return
  }
  if (allPushTargets.value.length >= 50) {
    error.value = '一次最多选择 50 个投放计划'
    return
  }
  selectedPlanKeys.value = [...selectedPlanKeys.value, key]
}

const toggleAllFilteredPlans = () => {
  userTouchedPlans = true
  const keys = selectableFilteredPlans.value.map(plan => `${plan.plan_type}:${plan.id}`)
  if (allFilteredSelected.value) {
    selectedPlanKeys.value = selectedPlanKeys.value.filter(key => !keys.includes(key))
    return
  }
  const existingTargetCount = targets.value.length
  const remaining = Math.max(0, 50 - existingTargetCount)
  const merged = Array.from(new Set([...selectedPlanKeys.value, ...keys]))
  selectedPlanKeys.value = merged.slice(0, remaining)
  if (merged.length > remaining) error.value = `一次最多选择 50 个计划，已保留前 ${remaining} 个`
}

const addSelectedTargets = (advertiserId = accountId.value) => {
  const account = accounts.value.find(item => item.id === advertiserId)
  if (!account) return
  const selected = plans.value
    .filter(plan => plan.can_attach_video && selectedPlanKeys.value.includes(`${plan.plan_type}:${plan.id}`))
    .map(plan => ({ advertiser_id: account.id, advertiser_name: account.name, plan_id: plan.id, plan_name: plan.name, plan_type: plan.plan_type }))
  const combined = [...targets.value, ...selected]
  targets.value = combined.filter((target, index) => combined.findIndex(item => item.advertiser_id === target.advertiser_id && item.plan_id === target.plan_id) === index).slice(0, 50)
  selectedPlanKeys.value = []
}

const authorize = async () => {
  if (authorizing.value) return
  authorizing.value = true
  error.value = ''
  try {
    await openAuthorizationWindow(() => window.open('about:blank', '_blank'), api.qianchuanAuthorize)
    authorizationNotice.value = '请在独立窗口完成千川授权，再返回这里刷新。当前素材与选择会保留，不会自动推送。'
  } catch (e) { error.value = e instanceof Error ? e.message : '无法发起授权' }
  finally { authorizing.value = false }
}

const push = async () => {
  const pushTargets = allPushTargets.value
  if (!pushTargets.length) return
  pushing.value = true
  error.value = ''
  try {
    const result = await api.qianchuanPush(props.assets.map(asset => asset.id), pushTargets)
    emit('queued', result.task_ids.length)
  } catch (e) { error.value = e instanceof Error ? e.message : '推送失败' }
  finally { pushing.value = false }
}

onMounted(load)
</script>

<template>
  <Teleport to="body">
    <div class="qc-modal-layer">
      <section class="qc-modal" aria-label="推送到巨量千川">
        <header>
          <div><span>QIANCHUAN DELIVERY</span><h2>{{ assets.length > 1 ? '批量推送到巨量千川' : '推送到巨量千川' }}</h2><p>{{ assetSummary }}</p></div>
          <button class="icon-button" aria-label="关闭" :disabled="pushing || bundleLoading" @click="emit('close')"><X /></button>
        </header>

        <div class="qc-asset-batch">
          <div><strong>本次素材 {{ assets.length }}/10</strong><small>全部使用原视频，不压缩、不转码；每条素材独立记录结果</small></div>
          <span v-for="asset in assets" :key="asset.id" :title="asset.filename">{{ asset.filename }}</span>
        </div>

        <div v-if="loading" class="qc-loading"><LoaderCircle class="spin" />正在读取千川授权与账户…</div>
        <template v-else>
          <div class="qc-auth-card" :class="{ ready: status?.authorized }">
            <ShieldCheck :size="24" />
            <div><strong>{{ status?.message }}</strong><small>回调地址：{{ status?.redirect_uri }}</small></div>
            <button v-if="!status?.authorized && status?.can_authorize" :disabled="authorizing" @click="authorize"><Link2 :size="15" />{{ authorizing ? '正在打开授权…' : '前往授权' }}</button>
            <small v-else-if="!status?.authorized">请联系千川管理员更新授权</small>
          </div>
          <div v-if="authorizationNotice" class="qc-auth-card" role="status">
            <span>{{ authorizationNotice }}</span>
            <button :disabled="loading || authorizing" @click="load"><RefreshCw :size="15" />授权完成后刷新</button>
          </div>

          <template v-if="status?.authorized">
            <section v-if="productPlanMap" class="qc-product-bundle" :class="{ warning: mixedProductBatch }">
              <div class="qc-product-bundle-copy">
                <span><Sparkles :size="17" />按产品一键匹配计划</span>
                <strong>{{ productDetectionLabel }}</strong>
                <small>根据账户映射表匹配“产品—账户—计划”，只加入下方目标清单，不会自动提交推送。</small>
                <a :href="productPlanMap.source.url" target="_blank" rel="noreferrer">查看映射原文 · 修订 {{ productPlanMap.source.revision }}</a>
              </div>
              <div class="qc-product-bundle-action">
                <select v-model="selectedProductKey" :disabled="bundleLoading || mixedProductBatch">
                  <option value="">选择产品</option>
                  <option v-for="bundle in productPlanMap.items" :key="bundle.key" :value="bundle.key">{{ bundle.label }}（{{ bundle.rules.length }} 个账户规则）</option>
                </select>
                <button type="button" :disabled="bundleLoading || mixedProductBatch || !selectedProductBundle" @click="applyProductBundle">
                  <LoaderCircle v-if="bundleLoading" class="spin" :size="16" /><Sparkles v-else :size="16" />
                  {{ bundleLoading ? '匹配中…' : '一键匹配并替换目标' }}
                </button>
                <small v-if="bundleProgress">{{ bundleProgress }}</small>
              </div>
              <p v-if="bundleSummary" class="qc-product-bundle-result">{{ bundleSummary }}</p>
              <p v-if="bundleWarnings.length" class="qc-product-bundle-warnings">{{ bundleWarnings.join('；') }}</p>
            </section>

            <div class="qc-target-builder">
              <label>千川账户
                <input v-model="accountKeyword" class="qc-filter-input" placeholder="搜索账户名称或ID" />
                <div class="qc-common-row"><select v-model="accountId"><option value="">请选择账户（共 {{ accounts.length }} 个）</option><option v-for="account in filteredAccounts" :key="account.id" :value="account.id">{{ account.name }} · {{ account.id }}</option></select><button type="button" class="qc-common-toggle" :class="{ active: accountPreference(accountId)?.pinned }" :disabled="!accountId" :title="accountPreference(accountId)?.pinned ? '取消常用账户' : '设为常用账户'" @click.prevent="toggleAccountPreference"><Star :size="15" :fill="accountPreference(accountId)?.pinned ? 'currentColor' : 'none'" />常用</button></div>
              </label>
              <label>投放计划
                <div class="qc-plan-filters">
                  <input v-model="planKeyword" class="qc-filter-input" placeholder="搜索计划名称或ID" />
                  <select v-model="planTypeFilter" class="qc-plan-type-filter">
                    <option value="">全部类型</option>
                    <option value="multiplication">乘方计划（{{ planCounts.multiplication }}）</option>
                    <option value="full_domain">全域推广（{{ planCounts.full_domain }}）</option>
                    <option value="standard">普通计划（{{ planCounts.standard }}）</option>
                  </select>
                </div>
                <div class="qc-plan-select-list" :class="{ loading: loadingPlans && !plans.length }">
                  <div class="qc-plan-select-head">
                    <span>{{ loadingPlans ? '正在优先读取乘方计划…' : loadingMorePlans ? `已显示 ${selectableFilteredPlans.length} 个，其他计划补齐中…` : `可批量选择 ${selectableFilteredPlans.length} 个计划` }}</span>
                    <span class="qc-plan-select-actions">
                      <button type="button" :disabled="loadingPlans || loadingMorePlans || !accountId" @click.prevent="refreshPlans"><RefreshCw :size="13" :class="{ spin: loadingPlans || loadingMorePlans }" />刷新计划</button>
                      <button type="button" :disabled="!selectableFilteredPlans.length" @click.prevent="toggleAllFilteredPlans"><Check :size="14" />{{ allFilteredSelected ? '取消全选' : '全选筛选结果' }}</button>
                    </span>
                  </div>
                  <label v-for="plan in filteredPlans" :key="`${plan.plan_type}-${plan.id}`" class="qc-plan-check" :class="{ disabled: !plan.can_attach_video, selected: selectedPlanKeys.includes(`${plan.plan_type}:${plan.id}`), full: plan.is_full }" :title="plan.capacity_message || ''">
                    <input type="checkbox" :checked="selectedPlanKeys.includes(`${plan.plan_type}:${plan.id}`)" :disabled="!plan.can_attach_video" @change="togglePlan(plan)" />
                    <span><strong>{{ plan.name }} <em v-if="plan.is_full" class="qc-plan-full-badge">计划已满</em></strong><small>{{ plan.plan_type_label }} · {{ plan.id }} · {{ plan.status_label }}{{ plan.is_full ? ' · 素材数量已达上限' : plan.can_attach_video ? '' : ' · 仅查询' }}</small><small v-if="plan.is_full" class="qc-plan-full-message">千川已明确返回满额，请改选其他计划</small></span>
                    <button type="button" class="qc-plan-star" :class="{ active: targetPreference(accountId, plan.id)?.pinned }" :title="targetPreference(accountId, plan.id)?.pinned ? '取消常用计划' : '设为常用计划'" @click.stop.prevent="togglePlanPreference(plan)"><Star :size="14" :fill="targetPreference(accountId, plan.id)?.pinned ? 'currentColor' : 'none'" /></button>
                  </label>
                  <div v-if="!loadingPlans && !filteredPlans.length" class="qc-plan-empty"><p>{{ planKeyword || planTypeFilter ? '没有符合筛选条件的计划' : '本次没有读取到计划，可立即重新读取' }}</p><button v-if="!planKeyword && !planTypeFilter" type="button" :disabled="loadingMorePlans" @click.prevent="refreshPlans"><RefreshCw :size="13" />重新读取</button></div>
                </div>
              </label>
              <button class="secondary-button" :disabled="!currentSelectedTargets.length" @click="addSelectedTargets()"><Plus :size="16" />加入已选 {{ currentSelectedTargets.length || '' }} 个目标</button>
            </div>
            <p class="qc-api-note">已查询 {{ accounts.length }} 个真实千川投放账户；选择账户后优先读取乘方计划，乘方可选后再后台补齐全域与普通计划。当前账户共读取 {{ planCounts.total }} 个计划（乘方 {{ planCounts.multiplication }}，全域 {{ planCounts.full_domain }}，普通 {{ planCounts.standard }}）。<strong v-if="fullPlanCount" class="qc-full-summary">其中 {{ fullPlanCount }} 个计划已满，已明显标红。</strong><span v-if="loadingMorePlans">其他计划仍在后台读取，已显示的计划不会被清空。</span><span v-else-if="plansCached">当前使用上一次成功读取的计划，后台波动不会影响选择。</span><span v-else-if="!plansComplete">当前显示已成功读取的部分计划，可点击“刷新计划”补齐。</span><span v-if="planFreshnessLabel"> 数据读取于 {{ planFreshnessLabel }}{{ plansCached ? '（缓存保护）' : '' }}。</span></p>
            <p v-if="planWarnings.length" class="qc-api-warning">{{ planWarnings.join('；') }}</p>

            <div class="qc-target-selection-tools">
              <strong>我的推送方案</strong>
              <input v-model="schemeName" maxlength="80" class="qc-filter-input" placeholder="方案名称，如水润日常投放" />
              <button type="button" class="secondary-button" :disabled="schemeSaving || bundleLoading || pushing || !schemeName.trim() || !pushTargetCount" @click="saveScheme">保存当前选择为新方案</button>
              <select v-model="schemeId" aria-label="我的推送方案"><option value="">选择已保存方案</option><option v-for="scheme in schemes" :key="scheme.id" :value="scheme.id">{{ scheme.name }} · {{ scheme.targets.length }}个计划</option></select>
              <button type="button" class="secondary-button" :disabled="schemeSaving || bundleLoading || pushing || !schemeId" @click="applyScheme">核验并加入方案计划</button>
              <button type="button" class="secondary-button" :disabled="schemeSaving || bundleLoading || pushing || !schemeId" @click="deleteScheme">删除所选方案</button>
              <button type="button" class="secondary-button" :disabled="schemeSaving || bundleLoading || pushing" @click="loadSchemes">刷新方案</button>
              <p v-if="schemeNotice" role="status">{{ schemeNotice }}</p>
            </div>
            <div class="qc-target-selection-tools">
              <strong>本次已选 {{ pushTargetCount }} 个计划</strong>
              <button type="button" class="secondary-button" :disabled="pushing || bundleLoading || !pushTargetCount" @click="clearTargets">清空全部已选计划</button>
              <button type="button" class="secondary-button" :disabled="pushing || bundleLoading || loadingPlans || loadingMorePlans || !accountId" @click="restoreRecentPlans">主动选用本账户最近计划</button>
              <p>默认不勾选历史计划。常用标记只影响排序；主动复用后请核对产品与目标。</p>
            </div>
            <div class="qc-targets">
              <div v-for="(target, index) in targets" :key="`${target.advertiser_id}-${target.plan_id}`">
                <Check :size="16" /><span><strong>{{ target.advertiser_name }}</strong><small>{{ target.plan_name }}</small></span>
                <button @click="targets.splice(index, 1)"><Trash2 :size="15" /></button>
              </div>
              <p v-if="!targets.length">可直接勾选多个计划确认投放；切换账户前，已勾选计划会自动加入批量目标。</p>
            </div>
          </template>
        </template>

        <div v-if="error" class="upload-error">{{ error }}</div>
        <footer>
          <button class="secondary-button" :disabled="pushing || bundleLoading" @click="emit('close')">取消</button>
          <button class="primary-button" :disabled="pushing || bundleLoading || !pushTargetCount" @click="push"><LoaderCircle v-if="pushing" class="spin" :size="16" /><Send v-else :size="16" />{{ pushing ? '提交中…' : `确认创建 ${expectedTaskCount || ''} 个任务` }}</button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
