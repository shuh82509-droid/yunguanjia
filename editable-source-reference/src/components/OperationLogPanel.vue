<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ChevronLeft, ChevronRight, ClipboardList, RefreshCw, Search } from 'lucide-vue-next'
import { api } from '../api'
import type { OperationLog } from '../types'

const rows = ref<OperationLog[]>([])
const modules = ref<string[]>([])
const total = ref(0)
const page = ref(1)
const pages = ref(1)
const q = ref('')
const module = ref('')
const result = ref('')
const loading = ref(true)
const error = ref('')
let timer: ReturnType<typeof setTimeout>
const load = async () => {
  loading.value = true; error.value = ''
  try {
    const data = await api.operationLogs(q.value, module.value, result.value, page.value, 20)
    rows.value = data.items; modules.value = data.modules; total.value = data.total; pages.value = data.total_pages
  } catch (e) { error.value = e instanceof Error ? e.message : '无法读取操作日志' }
  finally { loading.value = false }
}
watch([q, module, result], () => { page.value = 1; clearTimeout(timer); timer = setTimeout(load, 250) })
onMounted(load)
const dateTime = (value: string) => new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value))
</script>

<template>
  <section class="operation-panel">
    <div class="access-admin-hero"><div><span>OPERATION AUDIT</span><h2>操作日志</h2><p>查看同事在素材、推送、同步和权限模块中的关键操作，不记录密码、令牌或 Cookie。</p></div><div class="access-admin-stat"><ClipboardList /><strong>{{ total }}</strong><span>已记录操作</span></div></div>
    <div class="operation-toolbar"><label><Search :size="16" /><input v-model="q" placeholder="搜索姓名、工号、路径或目标 ID" /></label><select v-model="module"><option value="">全部模块</option><option v-for="item in modules" :key="item">{{ item }}</option></select><select v-model="result"><option value="">全部结果</option><option value="success">成功</option><option value="failed">失败</option></select><button @click="load"><RefreshCw :size="15" />刷新</button></div>
    <div v-if="error" class="access-message error">{{ error }}</div>
    <div class="operation-table">
      <div class="operation-row operation-head"><span>时间 / 操作人</span><span>模块</span><span>操作</span><span>结果</span><span>资源</span></div>
      <div v-if="loading" class="access-empty">正在读取使用情况…</div>
      <div v-for="row in rows" v-else :key="row.id" class="operation-row"><span><strong>{{ row.actor_name || row.actor_number }}</strong><small>{{ dateTime(row.created_at) }} · {{ row.department || '部门待读取' }}</small></span><span>{{ row.module }}</span><span><strong>{{ row.action }}</strong><small>{{ row.path }}</small></span><span><b :class="row.result">{{ row.result === 'success' ? '成功' : '失败' }}</b><small>HTTP {{ row.status_code }}</small></span><span><small>{{ row.resource_type }}</small><strong>{{ row.resource_id }}</strong></span></div>
      <div v-if="!loading && !rows.length" class="access-empty">没有匹配的操作记录</div>
      <footer class="operation-pagination"><span>共 {{ total }} 条</span><div><button :disabled="page <= 1" @click="page--; load()"><ChevronLeft :size="14" />上一页</button><strong>{{ page }} / {{ pages }}</strong><button :disabled="page >= pages" @click="page++; load()">下一页<ChevronRight :size="14" /></button></div></footer>
    </div>
  </section>
</template>
