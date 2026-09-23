<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { CheckCircle2, Clipboard, Download, FolderSync, Laptop, Link2, RefreshCw, Unplug, X } from 'lucide-vue-next'
import { api, ApiError } from '../api'
import type { JianyingDevice, JianyingPairing } from '../types'

const emit = defineEmits<{ close: [] }>()

const devices = ref<JianyingDevice[]>([])
const pairing = ref<JianyingPairing | null>(null)
const loading = ref(true)
const pairingBusy = ref(false)
const copied = ref(false)
const error = ref('')
let timer: number | undefined

const helperVersion = '1.3.12'
const installerUrl = `${import.meta.env.BASE_URL}downloads/WIS-Jianying-Helper-v${helperVersion}.exe`
const formattedCode = computed(() => pairing.value?.code.replace(/(.{5})(?=.)/, '$1-') || '')

const loadDevices = async () => {
  const result = await api.jianyingDevices()
  devices.value = result.items
}

const pollPairing = async () => {
  if (!pairing.value) return
  try {
    const result = await api.jianyingPairingStatus(pairing.value.id)
    if (result.status === 'claimed') {
      window.clearInterval(timer)
      timer = undefined
      pairing.value = null
      await loadDevices()
    } else if (result.status === 'expired' || result.status === 'replaced') {
      window.clearInterval(timer)
      timer = undefined
      error.value = '配对码已失效，请重新生成。'
    }
  } catch {
    // A short network fluctuation must not interrupt the local pairing window.
  }
}

const beginPairing = async () => {
  pairingBusy.value = true
  error.value = ''
  copied.value = false
  try {
    pairing.value = await api.jianyingPairingCreate()
    window.clearInterval(timer)
    timer = window.setInterval(pollPairing, 2000)
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '暂时无法生成配对码'
  } finally {
    pairingBusy.value = false
  }
}

const launchHelper = () => {
  if (pairing.value) window.location.assign(pairing.value.scheme_url)
}

const copyCode = async () => {
  if (!pairing.value) return
  await navigator.clipboard.writeText(pairing.value.code)
  copied.value = true
  window.setTimeout(() => { copied.value = false }, 1800)
}

const revoke = async (device: JianyingDevice) => {
  error.value = ''
  try {
    await api.jianyingDeviceRevoke(device.id)
    await loadDevices()
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '解除连接失败'
  }
}

onMounted(async () => {
  try {
    await loadDevices()
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : '无法读取剪映助手状态'
  } finally {
    loading.value = false
  }
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <Teleport to="body">
    <div class="jianying-layer" @mousedown.self="emit('close')">
      <section class="jianying-panel" role="dialog" aria-modal="true" aria-label="WIS 剪映互传">
        <header class="jianying-panel-head">
          <div><span>WIS DESKTOP BRIDGE</span><h2>剪映互传</h2><p>剪映导出的成片自动进入团队素材库；个人收藏可一键送回剪映。</p></div>
          <button type="button" class="icon-button" aria-label="关闭" @click="emit('close')"><X /></button>
        </header>

        <div class="jianying-flow" aria-label="剪映互传流程">
          <article><i>1</i><div><strong>安装助手</strong><span>每台电脑只需一次</span></div></article>
          <article><i>2</i><div><strong>连接 OA 身份</strong><span>收藏和上传归属本人</span></div></article>
          <article><i>3</i><div><strong>设置剪映导出目录</strong><span>导出完成后自动上传</span></div></article>
        </div>

        <div class="jianying-columns">
          <section class="jianying-setup-card">
            <div class="jianying-card-title"><Download /><div><strong>安装 WIS 剪映助手</strong><span>Windows 版 {{ helperVersion }} · 不修改剪映草稿</span></div></div>
            <a class="primary-button jianying-download" :href="installerUrl" :download="`WIS-Jianying-Helper-v${helperVersion}.exe`"><Download :size="17" />下载 v{{ helperVersion }}</a>
            <p>下载后双击运行；检测到旧版时，确认关闭旧版即可自动升级。系统会保留 OA 配对信息和本地目录。</p>

            <div class="jianying-pairing-block">
              <div class="jianying-card-title"><Link2 /><div><strong>连接当前 OA 账号</strong><span>配对码 10 分钟内有效且只能使用一次</span></div></div>
              <button v-if="!pairing" type="button" class="secondary-button" :disabled="pairingBusy" @click="beginPairing"><RefreshCw :class="{ spin: pairingBusy }" :size="17" />{{ pairingBusy ? '生成中…' : '生成配对码' }}</button>
              <template v-else>
                <div class="jianying-code-row"><code>{{ formattedCode }}</code><button type="button" @click="copyCode"><CheckCircle2 v-if="copied" :size="16" /><Clipboard v-else :size="16" />{{ copied ? '已复制' : '复制' }}</button></div>
                <button type="button" class="primary-button jianying-launch" @click="launchHelper"><Laptop :size="17" />打开助手并自动配对</button>
                <small>如果浏览器没有唤起助手，请在助手中手动输入上方配对码。</small>
              </template>
            </div>
          </section>

          <section class="jianying-device-card">
            <div class="jianying-card-title"><FolderSync /><div><strong>已连接电脑</strong><span>设备令牌可随时解除</span></div></div>
            <div v-if="loading" class="jianying-device-empty">正在读取连接状态…</div>
            <div v-else-if="devices.length" class="jianying-device-list">
              <article v-for="device in devices" :key="device.id">
                <span class="jianying-device-icon"><Laptop /></span>
                <div><strong>{{ device.device_name }}</strong><span>{{ device.last_seen_at ? `最近在线 ${new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(device.last_seen_at))}` : '等待首次连接' }}</span></div>
                <button type="button" title="解除连接" @click="revoke(device)"><Unplug :size="16" />解除</button>
              </article>
            </div>
            <div v-else class="jianying-device-empty"><Laptop /><strong>还没有连接电脑</strong><span>安装助手后，用左侧配对码连接。</span></div>

            <aside class="jianying-folder-note"><FolderSync :size="19" /><div><strong>推荐剪映导出位置</strong><code>视频\WIS剪映导出</code><span>每 10 秒自动扫描；也可在助手中点击“扫描目录并上传”补传。</span></div></aside>
          </section>
        </div>

        <p v-if="error" class="jianying-inline-error">{{ error }}</p>
        <footer><span>原视频上传，不压缩、不转码；失败会保留在本机等待重试。</span><button type="button" class="secondary-button" @click="emit('close')">完成</button></footer>
      </section>
    </div>
  </Teleport>
</template>
