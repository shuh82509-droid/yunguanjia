<script setup lang="ts">
import { ref } from 'vue'
import { api, ApiError } from '../api'
import type { OaPermissions, OaUser } from '../types'

const emit = defineEmits<{ login: [user: OaUser, permissions: OaPermissions] }>()
const username = ref('')
const password = ref('')
const captcha = ref('')
const showCaptcha = ref(false)
const captchaBusy = ref(false)
const busy = ref(false)
const error = ref('')

const submit = async () => {
  busy.value = true
  error.value = ''
  try {
    const data = await api.login(username.value.trim(), password.value, captcha.value)
    password.value = ''
    emit('login', data.user, data.permissions)
  } catch (exception) {
    if (exception instanceof ApiError && exception.status === 428) showCaptcha.value = true
    error.value = exception instanceof Error ? exception.message : '登录失败'
  } finally {
    busy.value = false
  }
}

const sendCaptcha = async () => {
  captchaBusy.value = true
  error.value = ''
  try {
    const result = await api.captcha(username.value.trim())
    error.value = result.message
  } catch (exception) {
    error.value = exception instanceof Error ? exception.message : '验证码发送失败'
  } finally {
    captchaBusy.value = false
  }
}

const unifiedLogin = () => {
  const callback = `${window.location.pathname}${window.location.search}`
  window.location.assign(`/_auth/login?callbackUrl=${encodeURIComponent(callback)}`)
}
</script>

<template>
  <main class="login-page">
    <section class="login-brand">
      <div class="login-logo">W</div>
      <small>WIS MARKETING CONTENT HUB</small>
      <h1>品牌内容，<br />在这里持续生长。</h1>
      <p>凡岛统一账号安全访问<br />沉淀、检索、复用每一条好内容。</p>
      <div class="login-stats"><span><b>统一</b>素材归档</span><span><b>快速</b>内容检索</span><span><b>安全</b>成员访问</span></div>
    </section>
    <section class="login-panel">
      <form @submit.prevent="submit">
        <div class="mini-logo">W</div>
        <h2>欢迎回来</h2>
        <p>使用凡岛统一 OA 账号登录 WIS 素材中心</p>
        <label>OA 工号<input v-model="username" autofocus placeholder="例如 FD-0001" autocomplete="username" autocapitalize="characters" spellcheck="false" @blur="username = username.trim().toUpperCase()" /></label>
        <label>登录密码<input v-model="password" type="password" placeholder="请输入 OA 密码" autocomplete="current-password" /></label>
        <label v-if="showCaptcha">登录验证码<div class="login-captcha-row"><input v-model="captcha" inputmode="numeric" placeholder="请输入验证码" autocomplete="one-time-code" /><button type="button" :disabled="captchaBusy || !username.trim()" @click="sendCaptcha">{{ captchaBusy ? '发送中…' : '获取验证码' }}</button></div></label>
        <div v-if="error" class="login-error">{{ error }}</div>
        <button :disabled="busy || !username.trim() || !password || (showCaptcha && !captcha)">{{ busy ? '正在验证身份…' : '登录素材中心 →' }}</button>
        <button type="button" class="login-unified" @click="unifiedLogin">使用统一 OA / 飞书登录</button>
        <small>仅限 OA 状态正常的品牌营销部及已授权同事登录</small>
      </form>
    </section>
  </main>
</template>
