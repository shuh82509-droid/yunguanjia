<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, Search, UserRound, Users } from 'lucide-vue-next'
import type { ReviewEligibleReviewer } from '../types'

const props = defineProps<{
  mode: 'organization' | 'designated'
  reviewers: ReviewEligibleReviewer[]
  selected: ReviewEligibleReviewer | null
}>()

const emit = defineEmits<{
  'update:mode': [value: 'organization' | 'designated']
  select: [value: ReviewEligibleReviewer | null]
}>()

const query = ref('')
const normalized = (value: string) => value.trim().toLocaleLowerCase('zh-CN')
const reviewerMeta = (reviewer: ReviewEligibleReviewer) => [
  reviewer.role_labels.join(' / '),
  reviewer.center,
  reviewer.group_name,
].filter(Boolean).join(' · ')
const filteredReviewers = computed(() => {
  const term = normalized(query.value)
  if (!term) return props.reviewers
  return props.reviewers.filter(reviewer => normalized([
    reviewer.user_name,
    reviewer.user_number,
    reviewer.center,
    reviewer.group_name,
    ...reviewer.role_labels,
  ].join(' ')).includes(term))
})

watch(() => props.mode, mode => {
  if (mode === 'organization') query.value = ''
})
</script>

<template>
  <section class="reviewer-assignment-picker">
    <header><span><UserRound /><strong>审核人分配</strong></span><small>指定一人后，由该审核人直接完成通过或驳回。</small></header>
    <div class="assignment-mode-buttons">
      <button type="button" :class="{ active: mode === 'organization' }" @click="emit('update:mode', 'organization'); emit('select', null)"><Users /><span><strong>按组织自动分配</strong><small>组长初审 → 主管复审</small></span><Check v-if="mode === 'organization'" /></button>
      <button type="button" :class="{ active: mode === 'designated' }" @click="emit('update:mode', 'designated')"><UserRound /><span><strong>指定一名审核人</strong><small>所选人员一次完成审核</small></span><Check v-if="mode === 'designated'" /></button>
    </div>
    <div v-if="mode === 'designated'" class="reviewer-search-panel">
      <label><Search /><input v-model="query" placeholder="搜索审核人姓名、工号、中心或小组" /></label>
      <div v-if="selected" class="selected-reviewer"><span><b>{{ selected.user_name }}</b><small>{{ reviewerMeta(selected) }}</small></span><button type="button" @click="emit('select', null); query = ''">重新选择</button></div>
      <div v-else class="reviewer-options">
        <button v-for="reviewer in filteredReviewers" :key="reviewer.user_number || `${reviewer.user_name}-${reviewer.center}`" type="button" @click="emit('select', reviewer); query = reviewer.user_name"><span>{{ reviewer.user_name.slice(0, 1) }}</span><b>{{ reviewer.user_name }}</b><small>{{ reviewerMeta(reviewer) }}</small></button>
        <p v-if="!filteredReviewers.length">没有匹配的审核人，请检查姓名或工号。</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.reviewer-assignment-picker{display:grid;gap:12px;padding:14px;border:1px solid #cfe2ee;border-radius:15px;background:#f8fcfe}.reviewer-assignment-picker header{display:flex;align-items:center;justify-content:space-between;gap:12px}.reviewer-assignment-picker header>span{display:flex;align-items:center;gap:7px;color:#173f5c}.reviewer-assignment-picker header svg{width:17px;color:#1487c9}.reviewer-assignment-picker header small{color:#728c9d}.assignment-mode-buttons{display:grid;grid-template-columns:1fr 1fr;gap:9px}.assignment-mode-buttons>button{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:9px;padding:12px;border:1px solid #d3e4ed;border-radius:12px;color:#41687e;background:#fff;text-align:left}.assignment-mode-buttons>button>svg{width:18px}.assignment-mode-buttons>button>span{display:grid;gap:3px}.assignment-mode-buttons>button small{color:#7a91a0}.assignment-mode-buttons>button.active{color:#0878ad;border-color:#55acd8;background:#edf8fe;box-shadow:0 0 0 2px rgba(17,139,203,.08)}.reviewer-search-panel{display:grid;gap:9px}.reviewer-search-panel>label{display:flex;align-items:center;gap:8px;padding:0 11px;border:1px solid #c9dee9;border-radius:10px;background:#fff}.reviewer-search-panel>label svg{width:16px;color:#6b8798}.reviewer-search-panel input{width:100%;height:40px;border:0;outline:0;background:transparent}.reviewer-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;max-height:224px;overflow:auto}.reviewer-options>button{display:grid;grid-template-columns:32px auto 1fr;align-items:center;gap:8px;padding:9px;border:1px solid #dce9ef;border-radius:10px;color:#173f5c;background:#fff;text-align:left}.reviewer-options>button>span{display:grid;place-items:center;width:30px;height:30px;border-radius:50%;color:#0878ad;background:#e3f4fc;font-weight:800}.reviewer-options small{overflow:hidden;color:#78909f;text-overflow:ellipsis;white-space:nowrap}.reviewer-options p{grid-column:1/-1;margin:0;padding:18px;color:#78909f;text-align:center}.selected-reviewer{display:flex;align-items:center;gap:10px;padding:11px 12px;border:1px solid #8bcdb4;border-radius:11px;color:#08785a;background:#ecf9f4}.selected-reviewer>span{display:grid;flex:1;gap:2px}.selected-reviewer small{color:#5c8074}.selected-reviewer button{padding:7px 10px;border:1px solid #98d2bc;border-radius:8px;color:#08785a;background:#fff}@media(max-width:760px){.assignment-mode-buttons,.reviewer-options{grid-template-columns:1fr}.reviewer-assignment-picker header{align-items:flex-start;flex-direction:column}}
</style>
