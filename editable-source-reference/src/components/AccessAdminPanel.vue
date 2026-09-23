<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { CheckCircle2, Clock3, Search, ShieldCheck, UserMinus, UserPlus, X } from 'lucide-vue-next'
import { api } from '../api'
import type { OaAccessAudit, OaAccessGrant } from '../types'

const grants = ref<OaAccessGrant[]>([])
const audits = ref<OaAccessAudit[]>([])
const departmentPolicy = ref('品牌营销')
const query = ref('')
const department = ref('')
const loading = ref(true)
const saving = ref(false)
const revoking = ref('')
const confirmRevoke = ref('')
const error = ref('')
const notice = ref('')

const normalizedQuery = computed(() => query.value.trim().toLocaleLowerCase('zh-CN'))
const visibleGrants = computed(() => {
  if (!normalizedQuery.value) return grants.value
  return grants.value.filter(item => [item.real_name, item.user_number, item.department]
    .some(value => value.toLocaleLowerCase('zh-CN').includes(normalizedQuery.value)))
})
const exactGrant = computed(() => grants.value.find(item => item.real_name === query.value.trim()))
const activeCount = computed(() => grants.value.filter(item => item.active).length)

const formatTime = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN') : '尚未登录核验'

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    const data = await api.accessGrants()
    grants.value = data.items
    audits.value = data.audits
    departmentPolicy.value = data.department_policy
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法读取登录白名单'
  } finally {
    loading.value = false
  }
}

const grantAccess = async () => {
  const realName = query.value.replace(/\s+/g, '')
  if (realName.length < 2 || saving.value) {
    error.value = '请输入同事完整的 OA 真实姓名'
    return
  }
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    await api.accessGrantCreate(realName, department.value.trim())
    notice.value = `已为 ${realName} 开通登录权限`
    query.value = realName
    department.value = ''
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '开通权限失败'
  } finally {
    saving.value = false
  }
}

const revokeAccess = async (grant: OaAccessGrant) => {
  revoking.value = grant.identifier
  error.value = ''
  notice.value = ''
  try {
    await api.accessGrantRevoke(grant.identifier)
    notice.value = `已取消 ${grant.real_name || grant.user_number} 的额外登录权限`
    confirmRevoke.value = ''
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '取消权限失败'
  } finally {
    revoking.value = ''
  }
}

onMounted(load)
</script>

<template>
  <section class="access-admin-panel">
    <header class="access-admin-hero">
      <div>
        <span>OA ACCESS CONTROL</span>
        <h2>登录权限白名单</h2>
        <p>品牌营销部保持默认可登录；这里仅管理其他部门的个人例外权限。</p>
      </div>
      <div class="access-admin-stat"><ShieldCheck /><strong>{{ activeCount }}</strong><span>名额外授权成员</span></div>
    </header>

    <div class="access-policy-note">
      <CheckCircle2 :size="18" />
      <div><strong>当前默认部门：{{ departmentPolicy }}</strong><span>超级管理员为 FD-026222。额外授权按 OA 真实姓名精确匹配，首次登录后自动回填工号和部门。</span></div>
    </div>

    <section class="access-grant-box">
      <div class="access-search-row">
        <label><Search :size="18" /><input v-model="query" maxlength="120" placeholder="输入同事完整 OA 姓名，搜索或授权" /></label>
        <input v-model="department" maxlength="255" placeholder="部门备注（选填）" />
        <button class="primary-button" :disabled="saving || query.trim().length < 2 || Boolean(exactGrant?.active)" @click="grantAccess">
          <UserPlus :size="16" />{{ saving ? '正在开通…' : exactGrant?.active ? '已经授权' : exactGrant ? '恢复权限' : '开通权限' }}
        </button>
      </div>
      <small>请使用 OA 中显示的完整真实姓名。系统不会保存密码，也不会扩大整个部门权限。</small>
    </section>

    <div v-if="notice" class="access-message success"><CheckCircle2 :size="17" />{{ notice }}</div>
    <div v-if="error" class="access-message error"><X :size="17" />{{ error }}<button @click="load">重新读取</button></div>

    <section class="access-list-section">
      <div class="access-section-heading"><div><span>授权成员</span><strong>{{ visibleGrants.length }} 条</strong></div><small>可搜索姓名、工号或部门</small></div>
      <div v-if="loading" class="access-empty">正在读取白名单…</div>
      <div v-else-if="visibleGrants.length" class="access-grant-list">
        <article v-for="grant in visibleGrants" :key="grant.identifier" :class="{ revoked: !grant.active }">
          <div class="access-person-avatar">{{ (grant.real_name || grant.user_number).slice(0, 1) }}</div>
          <div class="access-person-main">
            <div><strong>{{ grant.real_name || '待首次登录回填姓名' }}</strong><span :class="grant.active ? 'active' : 'revoked'">{{ grant.active ? '可登录' : '已取消' }}</span></div>
            <p>{{ grant.user_number || '工号待首次登录回填' }} · {{ grant.department || '部门待首次登录回填' }}</p>
            <small>由 {{ grant.granted_by_name || '系统' }} 于 {{ formatTime(grant.granted_at) }} 授权</small>
          </div>
          <div class="access-row-actions">
            <span><Clock3 :size="13" />{{ formatTime(grant.last_verified_at) }}</span>
            <button v-if="grant.active && confirmRevoke !== grant.identifier" class="access-revoke-button" @click="confirmRevoke = grant.identifier"><UserMinus :size="14" />取消权限</button>
            <div v-else-if="grant.active" class="access-inline-confirm"><span>确认取消？</span><button @click="confirmRevoke = ''">保留</button><button :disabled="revoking === grant.identifier" @click="revokeAccess(grant)">{{ revoking === grant.identifier ? '处理中…' : '确认' }}</button></div>
            <button v-else class="access-restore-button" @click="query = grant.real_name; grantAccess()"><UserPlus :size="14" />重新授权</button>
          </div>
        </article>
      </div>
      <div v-else class="access-empty">没有符合条件的授权记录。输入完整姓名即可开通。</div>
    </section>

    <details class="access-audit-section">
      <summary>最近权限操作记录（{{ audits.length }}）</summary>
      <div v-for="item in audits" :key="item.id"><span>{{ item.action === 'grant' ? '开通' : '取消' }}</span><strong>{{ item.real_name }}</strong><small>{{ item.actor_name }} · {{ formatTime(item.created_at) }}</small></div>
    </details>
  </section>
</template>
