<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AlertTriangle, CheckCircle2, Flame, LoaderCircle, QrCode, RefreshCw, WalletCards, X } from 'lucide-vue-next'
import { api } from '../api'
import type { ChannelsAccount, ChannelsPromotionAuth, ChannelsPromotionConfiguration, ChannelsPromotionOrder, ChannelsPromotionPaymentSession, ChannelsPromotionQuoteInput, ChannelsPromotionQuoteResult, ChannelsPromotionTarget, ChannelsTask } from '../types'

const props = withDefaults(defineProps<{ task?: ChannelsTask | null; account: ChannelsAccount; authOnly?: boolean }>(), {
  task: null,
  authOnly: false,
})
const emit = defineEmits<{ close: []; changed: [] }>()

const promotionAuth = ref<ChannelsPromotionAuth>({ ...props.account.promotion })
const target = ref<ChannelsPromotionTarget>('net_deal_roi')
const budget = ref(3000)
const duration = ref(24)
const fundingType = ref<ChannelsPromotionConfiguration['funding_type']>('wecoin')
const bidMode = ref<ChannelsPromotionConfiguration['bid_mode']>('volume')
const bidValue = ref<number | null>(null)
const startMode = ref<ChannelsPromotionConfiguration['start_mode']>('immediate')
const scheduledAt = ref('')
const billingMethod = ref<ChannelsPromotionConfiguration['billing_method']>('prepaid')
const promotionMode = ref<ChannelsPromotionConfiguration['promotion_mode']>('smart')
const portraitMode = ref<ChannelsPromotionConfiguration['portrait_mode']>('none')
const voucherMode = ref<ChannelsPromotionConfiguration['voucher_mode']>('none')
const orderName = ref(`${(props.task?.title || props.account.nickname).slice(0, 90)}-加热`)
const quote = ref<ChannelsPromotionQuoteResult | null>(null)
const confirmed = ref(false)
const busy = ref<'auth' | 'quote' | 'submit' | ''>('')
const error = ref('')
const success = ref<ChannelsPromotionOrder | null>(null)
const orders = ref<ChannelsPromotionOrder[]>([])
const sessionId = ref('')
const authMessage = ref('')
const captureSrc = ref('')
const captureRevision = ref(0)
const idempotencyKey = ref('')
const paymentOrder = ref<ChannelsPromotionOrder | null>(null)
const paymentSession = ref<ChannelsPromotionPaymentSession | null>(null)
const paymentMessage = ref('')
const paymentSyncing = ref(false)
let authTimer: ReturnType<typeof setInterval> | undefined
let authPolling = false
let paymentTimer: ReturnType<typeof setInterval> | undefined
let paymentPollCount = 0

const targetLabels: Record<ChannelsPromotionTarget, string> = {
  product_click: '商品点击数',
  product_pay: '商品成交数',
  net_product_pay: '商品净成交数',
  deal_roi: '成交 ROI',
  net_deal_roi: '商品净成交 ROI',
  smart: '智能加热',
  play: '播放数',
  like: '总点赞数',
  follow: '关注数',
  click: '商品点击数',
  heart: '爱心赞数',
}
const commerceTargets: { value: ChannelsPromotionTarget; label: string; note: string }[] = [
  { value: 'product_click', label: '商品点击数', note: '提升商品点击用户数' },
  { value: 'product_pay', label: '商品成交数', note: '提升商品成交订单数' },
  { value: 'net_product_pay', label: '商品净成交数', note: '剔除退款后的成交次数' },
  { value: 'deal_roi', label: '成交 ROI', note: '优化成交金额与投入比' },
  { value: 'net_deal_roi', label: '商品净成交 ROI', note: '按净成交金额优化' },
]
const unavailableTargets = [
  '智能加热', '总点赞数', '关注数', '播放数', '直播预约', '组件点击', '成功添加通讯录',
  '游戏付费 ROI', '游戏付费次数', '短剧付费 ROI', '短剧广告变现人数', '短剧广告变现 ROI',
]
const targetLabel = (value: ChannelsPromotionTarget) => targetLabels[value] || value
const statusLabel = (value: string) => ({ success: '已支付', submitting: '提交中', pending_payment: '待支付', payment_processing: '支付确认中', failed: '失败', manual_review: '需人工核对', cancelled: '已取消' } as Record<string, string>)[value] || value
const balanceCopy = computed(() => promotionAuth.value.balance == null ? '余额待读取' : `${promotionAuth.value.balance.toLocaleString('zh-CN')} 微信豆`)
const isRoiTarget = computed(() => target.value === 'deal_roi' || target.value === 'net_deal_roi')
const budgetOptions = computed(() => isRoiTarget.value ? [3000, 5000] : [1000, 2000])
const minimumBudget = computed(() => budgetOptions.value[0])
const budgetValid = computed(() => Number.isInteger(Number(budget.value)) && Number(budget.value) >= minimumBudget.value && Number(budget.value) <= 30_000_000)
const settingsIssue = computed(() => {
  if (fundingType.value !== 'wecoin') return '当前企业账户直投链路只完成微信豆校验；现金和自动选择需腾讯返回对应资金账户能力后才能提交。'
  if (billingMethod.value === 'realtime') return '实时扣费只对腾讯已开通的现金账户开放；微信豆账户需使用预先扣费。'
  if (bidMode.value === 'cost_control' && (!bidValue.value || bidValue.value <= 0)) return '控成本加热需要填写目标出价。'
  if (startMode.value === 'scheduled' && !scheduledAt.value) return '请选择定时加热的开始时间。'
  if (promotionMode.value === 'targeted') return '定向加热还需补充性别、年龄、地域或相似账号等人群条件，当前不能以空人群提交。'
  if (portraitMode.value === 'authorized') return '使用他人肖像需上传腾讯要求的肖像授权证明后才能提交。'
  if (voucherMode.value === 'max') return '最大面额优惠需先读取当前账户的可用优惠券；当前账户尚未返回可用券。'
  return ''
})
const dateTime = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))

const configurationPayload = (): ChannelsPromotionConfiguration => ({
  funding_type: fundingType.value,
  bid_mode: bidMode.value,
  bid_value: bidMode.value === 'cost_control' ? Number(bidValue.value) : null,
  start_mode: startMode.value,
  scheduled_at: startMode.value === 'scheduled' && scheduledAt.value ? new Date(scheduledAt.value).toISOString() : null,
  billing_method: billingMethod.value,
  promotion_mode: promotionMode.value,
  portrait_mode: portraitMode.value,
  voucher_mode: voucherMode.value,
})

const quotePayload = (): ChannelsPromotionQuoteInput => ({
  promotion_target: target.value,
  budget_wecoin: Number(budget.value),
  duration_hours: Number(duration.value),
  ...configurationPayload(),
})

const chooseBudget = (value: number) => { budget.value = value }
const normalizeBudget = () => {
  const normalized = Number(String(budget.value).replace(/[^0-9]/g, ''))
  budget.value = Number.isFinite(normalized) ? normalized : minimumBudget.value
}

const loadOrders = async () => {
  if (!props.task) return
  const data = await api.channelsPromotionOrders(props.task.id)
  orders.value = data.items
}

const refreshAuth = async () => {
  promotionAuth.value = await api.channelsPromotionAuthStatus(props.account.id)
}

const paintCapture = (revision: number) => {
  if (!sessionId.value || revision <= captureRevision.value) return
  const src = api.channelsPromotionCaptureUrl(sessionId.value, revision)
  const preload = new Image()
  preload.onload = () => {
    if (!sessionId.value) return
    captureSrc.value = src
    captureRevision.value = revision
  }
  preload.src = src
}

const pollAuth = async () => {
  if (!sessionId.value || authPolling) return
  authPolling = true
  try {
    const state = await api.channelsPromotionAuthSession(sessionId.value)
    authMessage.value = state.message
    if (state.capture_ready) paintCapture(Number(state.capture_revision || 0))
    if (['authorized', 'failed', 'expired'].includes(state.status)) {
      if (authTimer) clearInterval(authTimer)
      busy.value = ''
      if (state.status === 'authorized') {
        await refreshAuth()
        sessionId.value = ''
        captureSrc.value = ''
        captureRevision.value = 0
        emit('changed')
      } else {
        error.value = state.message
      }
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '加热授权状态读取失败'
  } finally {
    authPolling = false
  }
}

const startAuth = async () => {
  if (authTimer) clearInterval(authTimer)
  busy.value = 'auth'; error.value = ''; captureSrc.value = ''; captureRevision.value = 0
  try {
    const state = await api.channelsPromotionAuthStart(props.account.id)
    sessionId.value = state.id
    authMessage.value = state.message
    await pollAuth()
    if (sessionId.value) authTimer = setInterval(() => void pollAuth(), 1200)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法发起加热授权'
    busy.value = ''
  }
}

const invalidateQuote = () => {
  quote.value = null
  confirmed.value = false
  success.value = null
  idempotencyKey.value = ''
}
watch([target, budget, duration, fundingType, bidMode, bidValue, startMode, scheduledAt, billingMethod, promotionMode, portraitMode, voucherMode], invalidateQuote)
watch(target, () => { budget.value = budgetOptions.value[0] })

const requestQuote = async () => {
  if (!props.task) return
  busy.value = 'quote'; error.value = ''; success.value = null
  try {
    quote.value = await api.channelsPromotionQuote(props.task.id, quotePayload())
    await refreshAuth()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '询价未完成'
    await refreshAuth().catch(() => undefined)
  } finally { busy.value = '' }
}

const submit = async () => {
  if (!props.task || !quote.value || !confirmed.value || !orderName.value.trim()) return
  busy.value = 'submit'; error.value = ''
  if (!idempotencyKey.value) idempotencyKey.value = crypto.randomUUID()
  try {
    const created = await api.channelsPromotionCreate(props.task.id, {
      ...quotePayload(),
      order_name: orderName.value.trim(),
      confirmed: true,
      idempotency_key: idempotencyKey.value,
    })
    success.value = created
    confirmed.value = false
    await Promise.all([loadOrders(), refreshAuth()])
    emit('changed')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '加热计划提交未完成'
    await loadOrders().catch(() => undefined)
  } finally { busy.value = '' }
}

const stopPaymentPolling = () => {
  if (paymentTimer) clearInterval(paymentTimer)
  paymentTimer = undefined
  paymentPollCount = 0
}

const closePayment = () => {
  stopPaymentPolling()
  paymentOrder.value = null
  paymentSession.value = null
  paymentMessage.value = ''
  void loadOrders().catch(() => undefined)
}

const syncPayment = async (order = paymentOrder.value) => {
  if (!props.task || !order || paymentSyncing.value) return null
  paymentSyncing.value = true
  try {
    const synced = await api.channelsPromotionSync(props.task.id, order.id)
    success.value = synced
    const index = orders.value.findIndex(item => item.id === synced.id)
    if (index >= 0) orders.value.splice(index, 1, synced)
    else orders.value.unshift(synced)
    if (paymentOrder.value?.id === synced.id) paymentOrder.value = synced
    if (synced.status === 'success') {
      paymentMessage.value = `腾讯已确认真实支付成功，扣除 ${(synced.cost_wecoin || 0).toLocaleString('zh-CN')} 微信豆。`
      stopPaymentPolling()
      await refreshAuth().catch(() => undefined)
      emit('changed')
    } else {
      paymentMessage.value = synced.platform_message || synced.error_message || statusLabel(synced.status)
    }
    return synced
  } catch (cause) {
    paymentMessage.value = cause instanceof Error ? cause.message : '支付状态核验失败'
    return null
  } finally {
    paymentSyncing.value = false
  }
}

const pollPaymentSession = async () => {
  if (!props.task || !paymentOrder.value || !paymentSession.value || paymentSyncing.value) return
  paymentSyncing.value = true
  try {
    const state = await api.channelsPromotionPaymentSessionGet(props.task.id, paymentOrder.value.id, paymentSession.value.id)
    paymentSession.value = state
    if (state.status === 'succeeded') {
      paymentMessage.value = '腾讯已确认真实支付成功，正在刷新订单…'
      stopPaymentPolling()
      await loadOrders()
      const paid = orders.value.find(item => item.id === paymentOrder.value?.id)
      if (paid) {
        paymentOrder.value = paid
        success.value = paid
      }
      await refreshAuth().catch(() => undefined)
      emit('changed')
    } else if (['failed', 'cancelled', 'expired', 'uncertain'].includes(state.status)) {
      stopPaymentPolling()
      paymentMessage.value = state.status === 'expired'
        ? '支付页已过期；再次打开前系统会先核验腾讯订单。'
        : state.status === 'uncertain'
          ? '支付请求结果不确定，请只核验订单，不要重复付款。'
          : '本次支付会话已结束，订单仍以腾讯回读结果为准。'
    } else {
      paymentMessage.value = state.status === 'confirming'
        ? '支付页已反馈状态变化，正在等待腾讯订单确认…'
        : '支付页有效，系统正在持续核验腾讯订单。'
    }
  } catch (cause) {
    paymentMessage.value = cause instanceof Error ? cause.message : '支付状态核验失败'
  } finally {
    paymentSyncing.value = false
  }
}

const startPaymentPolling = () => {
  stopPaymentPolling()
  paymentPollCount = 0
  paymentTimer = setInterval(() => {
    paymentPollCount += 1
    void pollPaymentSession()
    if (paymentPollCount >= 100) stopPaymentPolling()
  }, 3000)
}

const openPaymentPage = () => {
  if (!paymentSession.value?.launch_url) return
  window.open(paymentSession.value.launch_url, '_blank', 'noopener,noreferrer')
}

const resumePayment = async (order: ChannelsPromotionOrder) => {
  if (!props.task) return
  busy.value = 'submit'; error.value = ''
  const paymentWindow = window.open('about:blank', '_blank')
  if (paymentWindow) paymentWindow.opener = null
  try {
    const session = await api.channelsPromotionPaymentSessionCreate(props.task.id, order.id, crypto.randomUUID())
    paymentOrder.value = order
    paymentSession.value = session
    paymentMessage.value = '腾讯支付页已打开；系统只在腾讯订单回读成功后显示“已支付”。'
    if (paymentWindow && session.launch_url) paymentWindow.location.href = session.launch_url
    else if (paymentWindow) paymentWindow.close()
    startPaymentPolling()
  } catch (cause) {
    if (paymentWindow) paymentWindow.close()
    error.value = cause instanceof Error ? cause.message : '无法打开腾讯支付组件'
  } finally { busy.value = '' }
}

onMounted(() => {
  if (props.task) void loadOrders().catch(cause => { error.value = cause instanceof Error ? cause.message : '历史订单读取失败' })
})
onBeforeUnmount(() => {
  if (authTimer) clearInterval(authTimer)
  stopPaymentPolling()
})
</script>

<template>
  <Teleport to="body">
    <div class="channels-promotion-backdrop" @click.self="emit('close')">
      <section class="channels-promotion-modal" role="dialog" aria-modal="true" :aria-label="authOnly ? '授权视频号加热账户' : '视频号加热'">
        <header>
          <div><span>WECHAT CHANNELS PROMOTION</span><h2><Flame :size="24" />{{ authOnly ? '加热账户授权' : '视频号加热' }}</h2><p>{{ authOnly ? `${account.nickname} · 发布与加热使用独立授权` : `${task?.account_name} · ${task?.title}` }}</p></div>
          <button aria-label="关闭" @click="emit('close')"><X :size="22" /></button>
        </header>

        <div v-if="task" class="channels-promotion-proof"><CheckCircle2 :size="18" /><div><strong>已确认公开视频</strong><span>加热标识 {{ task.platform_export_id }} · {{ task.platform_export_source || '平台回读' }}</span></div></div>

        <div v-if="promotionAuth.status === 'enterprise_required'" class="channels-promotion-result error">
          <AlertTriangle />
          <div><strong>当前不是企业加热账户</strong><span>{{ promotionAuth.message }}。发布视频号与加热账户可以不同，但加热账户必须选择企业账户。</span></div>
        </div>

        <div v-if="!promotionAuth.authorized" class="channels-promotion-auth">
          <div><QrCode :size="28" /><strong>需要独立企业加热授权</strong><p>发布视频号与加热账户可以不同。扫码后请在手机端选择任意企业账户；Apple 或 Android 个人账户不能用于企业加热。授权约 72 小时有效。</p></div>
          <img v-if="captureSrc" :src="captureSrc" alt="视频号加热授权二维码" />
          <div v-if="sessionId" class="channels-promotion-auth-status"><LoaderCircle v-if="busy === 'auth'" class="spin" /><span>{{ authMessage }}</span></div>
          <button class="primary-button" :disabled="busy === 'auth'" @click="startAuth"><QrCode :size="16" />{{ busy === 'auth' ? '等待扫码…' : '扫码授权加热账户' }}</button>
        </div>

        <template v-else>
          <div class="channels-promotion-account-pair">
            <div><small>发布视频号</small><strong>{{ task?.account_name || account.nickname }}</strong><span>用于发布内容</span></div>
            <div><small>企业加热账户</small><strong>{{ promotionAuth.nickname || '未命名企业账户' }}</strong><span>{{ promotionAuth.account_type || '企业账户' }} · 用于询价和支付</span></div>
          </div>
          <div class="channels-promotion-balance"><WalletCards :size="20" /><div><small>企业账户余额</small><strong>{{ promotionAuth.nickname || '未命名企业账户' }}</strong></div><span>{{ balanceCopy }}</span></div>
          <div v-if="authOnly" class="channels-promotion-auth-ready"><CheckCircle2 :size="22" /><div><strong>企业加热授权可用</strong><span>现在可以给这个发布视频号下的已公开视频加热；两个账号无需同名。</span></div></div>
          <template v-else>
          <div class="channels-promotion-config">
            <section class="channels-promotion-field channels-promotion-target-field">
              <div class="channels-promotion-field-title"><strong>优先提升目标</strong><span>按企业商品视频当前可用目标补齐</span></div>
              <div class="channels-promotion-targets">
                <button v-for="option in commerceTargets" :key="option.value" type="button" :class="{ active: target === option.value }" @click="target = option.value">
                  <span>{{ option.label }}</span><small>{{ option.note }}</small>
                </button>
              </div>
              <details class="channels-promotion-unavailable">
                <summary>查看平台暂不可用目标（{{ unavailableTargets.length }}）</summary>
                <div><span v-for="option in unavailableTargets" :key="option">{{ option }}</span></div>
              </details>
            </section>

            <section class="channels-promotion-field">
              <div class="channels-promotion-field-title"><strong>支付微信豆</strong><span>ROI 目标使用平台对应的 3000 / 5000 预设</span></div>
              <div class="channels-promotion-budget-row">
                <button v-for="option in budgetOptions" :key="option" type="button" :class="{ active: Number(budget) === option }" @click="chooseBudget(option)">{{ option.toLocaleString('zh-CN') }}</button>
                <label class="channels-promotion-custom-budget" :class="{ invalid: !budgetValid }"><span>自定义</span><input v-model.number="budget" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="8" aria-label="自定义预算微信豆" @blur="normalizeBudget" /><small>微信豆</small></label>
              </div>
              <p v-if="!budgetValid" class="channels-promotion-validation">预算不得低于 {{ minimumBudget.toLocaleString('zh-CN') }} 微信豆，且只能填写整数。</p>
            </section>

            <section class="channels-promotion-field channels-promotion-settings">
              <div class="channels-promotion-field-title"><strong>加热配置</strong><span>每项均可切换；可提交能力按腾讯账户和必要材料实时校验</span></div>
              <div class="channels-promotion-setting-grid">
                <div><small>扣费资金类型</small><button type="button" :class="{ active: fundingType === 'wecoin' }" @click="fundingType = 'wecoin'">微信豆</button><button type="button" :class="{ active: fundingType === 'cash' }" @click="fundingType = 'cash'">现金</button><button type="button" :class="{ active: fundingType === 'auto' }" @click="fundingType = 'auto'">自动选择</button></div>
                <div><small>出价方式</small><button type="button" :class="{ active: bidMode === 'volume' }" @click="bidMode = 'volume'">放量加热</button><button type="button" :class="{ active: bidMode === 'cost_control' }" @click="bidMode = 'cost_control'">控成本加热</button><label v-if="bidMode === 'cost_control'" class="setting-value"><input v-model.number="bidValue" type="number" min="0.01" step="0.01" /><span>{{ isRoiTarget ? '目标 ROI' : '微信豆' }}</span></label></div>
                <div><small>定时加热</small><button type="button" :class="{ active: startMode === 'immediate' }" @click="startMode = 'immediate'">不定时</button><button type="button" :class="{ active: startMode === 'scheduled' }" @click="startMode = 'scheduled'">自定义时间</button><input v-if="startMode === 'scheduled'" v-model="scheduledAt" class="setting-datetime" type="datetime-local" /></div>
                <div><small>扣费方式</small><button type="button" :class="{ active: billingMethod === 'prepaid' }" @click="billingMethod = 'prepaid'">预先扣费</button><button type="button" :class="{ active: billingMethod === 'realtime' }" @click="billingMethod = 'realtime'">实时扣费</button></div>
                <div><small>加热方式</small><button type="button" :class="{ active: promotionMode === 'smart' }" @click="promotionMode = 'smart'">智能加热</button><button type="button" :class="{ active: promotionMode === 'targeted' }" @click="promotionMode = 'targeted'">定向加热</button></div>
                <div><small>使用他人肖像</small><button type="button" :class="{ active: portraitMode === 'none' }" @click="portraitMode = 'none'">未使用</button><button type="button" :class="{ active: portraitMode === 'authorized' }" @click="portraitMode = 'authorized'">使用</button></div>
                <div><small>使用优惠</small><button type="button" :class="{ active: voucherMode === 'none' }" @click="voucherMode = 'none'">不使用优惠</button><button type="button" :class="{ active: voucherMode === 'max' }" @click="voucherMode = 'max'">最大面额优先</button></div>
              </div>
              <p v-if="settingsIssue" class="channels-promotion-setting-note warning">{{ settingsIssue }}</p>
              <p v-else class="channels-promotion-setting-note ready">当前组合已通过本地校验，询价时还会由腾讯校验素材与账户能力。</p>
            </section>

            <section class="channels-promotion-field channels-promotion-final-fields">
              <label><span>加热时长</span><select v-model.number="duration"><option :value="24">24 小时</option><option :value="12">12 小时</option><option :value="8">8 小时</option><option :value="6">6 小时</option></select></label>
              <label><span>计划名称</span><input v-model="orderName" maxlength="120" /></label>
            </section>
          </div>
          <button class="channels-promotion-quote" :disabled="busy !== '' || !budgetValid || !!settingsIssue" @click="requestQuote"><RefreshCw :size="16" :class="{ spin: busy === 'quote' }" />{{ busy === 'quote' ? '正在询价…' : '询价并检查余额' }}</button>

          <div v-if="quote" class="channels-promotion-confirm">
            <div><small>本次目标</small><strong>{{ targetLabel(target) }} · {{ duration }} 小时</strong></div>
            <div><small>预计扣除</small><strong>{{ quote.need_pay.toLocaleString('zh-CN') }} 微信豆</strong></div>
            <div><small>当前余额</small><strong>{{ quote.balance == null ? '平台未返回' : `${quote.balance.toLocaleString('zh-CN')} 微信豆` }}</strong></div>
            <label><input v-model="confirmed" type="checkbox" /><span>我确认先创建腾讯待支付计划，再由我在支付组件中完成支付；只有腾讯回读确认后系统才显示“已支付”。</span></label>
            <button class="primary-button danger-confirm" :disabled="!confirmed || busy !== '' || !orderName.trim()" @click="submit"><Flame :size="16" />{{ busy === 'submit' ? '正在创建计划…' : '创建待支付计划' }}</button>
          </div>
          </template>
        </template>

        <div v-if="paymentOrder" class="channels-promotion-payment">
          <div class="channels-promotion-payment-head"><div><strong>腾讯微信豆支付</strong><span>订单 {{ paymentOrder.order_name }} · 未回读成功前不会记为已支付</span></div><button type="button" aria-label="关闭支付" @click="closePayment"><X :size="18" /></button></div>
          <div class="channels-promotion-payment-launch"><QrCode :size="34" /><div><strong>在腾讯官方页面扫码支付</strong><span>支付凭证约 5 分钟有效，关闭页面后仍可回来核验结果。</span></div><button type="button" :disabled="!paymentSession?.launch_url" @click="openPaymentPage">打开腾讯支付页</button></div>
          <div class="channels-promotion-payment-foot"><span>{{ paymentMessage }}</span><button type="button" :disabled="paymentSyncing" @click="pollPaymentSession"><RefreshCw :size="14" :class="{ spin: paymentSyncing }" />{{ paymentSyncing ? '核验中…' : '我已支付，立即核验' }}</button></div>
        </div>

        <div v-if="success?.status === 'success'" class="channels-promotion-result success"><CheckCircle2 /><div><strong>腾讯已确认真实支付</strong><span>本地订单 {{ success.id }} · 腾讯计划 {{ success.promotion_id }} · 实际支付 {{ success.cost_wecoin }} 微信豆</span></div></div>
        <div v-else-if="success?.status === 'pending_payment' || success?.status === 'payment_processing'" class="channels-promotion-result pending"><WalletCards /><div><strong>{{ statusLabel(success.status) }}</strong><span>{{ success.error_message || '腾讯计划已创建，请在支付组件中完成支付。' }}</span></div></div>
        <div v-if="error" class="channels-promotion-result error"><AlertTriangle /><div><strong>本次操作未完成</strong><span>{{ error }}</span></div></div>

        <details v-if="!authOnly && orders.length" class="channels-promotion-history">
          <summary>查看历史加热订单（{{ orders.length }}）</summary>
          <article v-for="order in orders" :key="order.id">
            <div><strong>{{ order.order_name }}</strong><span>{{ dateTime(order.created_at) }} · {{ targetLabel(order.promotion_target) }} · {{ order.duration_hours }} 小时</span></div>
            <b :class="order.status">{{ statusLabel(order.status) }}</b>
            <small>{{ order.status === 'success' ? `${order.cost_wecoin} 微信豆（腾讯已确认）` : order.error_message }}</small>
            <div v-if="['pending_payment', 'payment_processing', 'manual_review'].includes(order.status)" class="channels-promotion-history-actions"><button type="button" @click="resumePayment(order)">继续支付</button><button type="button" @click="syncPayment(order)">核验支付</button></div>
          </article>
        </details>
      </section>
    </div>
  </Teleport>
</template>
