<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Search, ShieldCheck, Trash2, UserPlus } from 'lucide-vue-next'
import { api } from '../api'
import type { AdminGrant } from '../types'

const rows = ref<AdminGrant[]>([])
const q = ref('')
const name = ref('')
const department = ref('')
const loading = ref(true)
const busy = ref('')
const error = ref('')
const success = ref('')

const load = async () => {
  loading.value = true
  error.value = ''
  try { rows.value = (await api.adminGrants(q.value)).items }
  catch (e) { error.value = e instanceof Error ? e.message : '无法读取管理员列表' }
  finally { loading.value = false }
}
const grant = async () => {
  if (!name.value.trim()) return
  busy.value = 'create'
  error.value = ''
  success.value = ''
  try {
    const grantedName = name.value.trim()
    await api.adminGrantCreate(grantedName, department.value)
    success.value = `已为 ${grantedName} 开通管理员权限：可查看操作日志和全员投放数据回流，请让对方刷新页面。`
    name.value = ''; department.value = ''
    await load()
  } catch (e) { error.value = e instanceof Error ? e.message : '授权失败' }
  finally { busy.value = '' }
}
const revoke = async (row: AdminGrant) => {
  busy.value = row.identifier
  success.value = ''
  try { await api.adminGrantRevoke(row.identifier); await load() }
  catch (e) { error.value = e instanceof Error ? e.message : '取消授权失败' }
  finally { busy.value = '' }
}
onMounted(load)
</script>

<template>
  <section class="access-admin-panel">
    <div class="access-admin-hero"><div><span>ADMIN ROLES</span><h2>管理员权限</h2><p>管理员可查看操作日志及全员千川、ADQ、视频号结果与数据回流；登录权限和管理员设置仅舒豪、吴为可见。</p></div><div class="access-admin-stat"><ShieldCheck /><strong>{{ rows.filter(row => row.active).length }}</strong><span>当前管理员</span></div></div>
    <div class="access-policy-note"><ShieldCheck /><div><strong>权限负责人受保护</strong><span>舒豪、吴为不可被其他管理员取消；所有授权变更都会写入操作日志。</span></div></div>
    <div class="access-grant-box">
      <div class="access-search-row"><label><Search /><input v-model="name" placeholder="输入 OA 真实姓名" /></label><input v-model="department" placeholder="部门备注（可选）" /><button class="primary-button" :disabled="busy === 'create' || !name.trim()" @click="grant"><UserPlus :size="15" />开通数据管理员</button></div>
      <small>开通后可查看操作日志和全员投放数据；只能操作本人任务，不会获得登录白名单或管理员配置权限。</small>
    </div>
    <div v-if="success" class="access-message success">{{ success }}<button @click="success = ''">关闭</button></div>
    <div v-if="error" class="access-message error">{{ error }}<button @click="error = ''">关闭</button></div>
    <section class="access-list-section">
      <div class="access-section-heading"><div><span>管理员列表</span><strong>{{ rows.length }}</strong></div><label class="admin-inline-search"><Search :size="14" /><input v-model="q" placeholder="搜索姓名、工号、部门" @keyup.enter="load" /><button @click="load">搜索</button></label></div>
      <div v-if="loading" class="access-empty">正在读取管理员权限…</div>
      <div v-else class="access-grant-list">
        <article v-for="row in rows" :key="row.identifier" :class="{ revoked: !row.active }"><span class="access-person-avatar">{{ row.real_name.slice(0, 1) }}</span><div class="access-person-main"><div><strong>{{ row.real_name }}</strong><span :class="row.active ? 'active' : 'revoked'">{{ row.protected ? '权限负责人' : row.active ? '数据管理员' : '已取消' }}</span></div><p>{{ row.department || '部门待登录后自动补全' }}<template v-if="row.user_number"> · {{ row.user_number }}</template></p><small>由 {{ row.granted_by_name || '系统' }} 授权</small></div><div class="access-row-actions"><button v-if="row.active && !row.protected" class="access-revoke-button" :disabled="busy === row.identifier" @click="revoke(row)"><Trash2 :size="14" />取消</button><span v-else-if="row.protected"><ShieldCheck :size="14" />受保护</span></div></article>
        <div v-if="!rows.length" class="access-empty">没有匹配的管理员</div>
      </div>
    </section>
  </section>
</template>
