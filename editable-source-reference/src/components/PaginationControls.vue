<script setup lang="ts">
import { ref, watch } from 'vue'
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'

const props = defineProps<{ page: number; totalPages: number }>()
const emit = defineEmits<{ change: [page: number] }>()
const jumpPage = ref('')

const clampPage = (value: number) => Math.min(Math.max(1, value), Math.max(1, props.totalPages))
const goToPage = (value: number) => emit('change', clampPage(value))
const submitJump = () => {
  const value = Number(jumpPage.value)
  if (!Number.isFinite(value)) return
  goToPage(Math.trunc(value))
  jumpPage.value = ''
}

watch(() => props.page, () => { jumpPage.value = '' })
</script>

<template>
  <div class="pagination-controls" :class="{ 'is-single-page': totalPages <= 1 }">
    <button v-if="totalPages > 1" class="pagination-nav" type="button" :disabled="page <= 1" aria-label="上一页" @click="goToPage(page - 1)">
      <ChevronLeft :size="14" />上一页
    </button>
    <strong aria-live="polite">{{ totalPages <= 1 ? '共 1 页' : `第 ${page} / ${Math.max(1, totalPages)} 页` }}</strong>
    <div v-if="totalPages > 1" class="pagination-jump">
      <span>跳至</span>
      <input v-model="jumpPage" type="number" inputmode="numeric" min="1" :max="Math.max(1, totalPages)" aria-label="输入要跳转的页码" @keydown.enter.prevent="submitJump" />
      <span>页</span>
      <button type="button" :disabled="!jumpPage" @click="submitJump">跳转</button>
    </div>
    <button v-if="totalPages > 1" class="pagination-nav" type="button" :disabled="page >= totalPages" aria-label="下一页" @click="goToPage(page + 1)">
      下一页<ChevronRight :size="14" />
    </button>
  </div>
</template>

<style scoped>
.pagination-controls{
  display:grid !important;
  grid-template-columns:max-content max-content max-content max-content !important;
  align-items:center;
  gap:8px;
  width:max-content !important;
  max-width:100%;
}
.pagination-controls.is-single-page{grid-template-columns:max-content !important}
.pagination-controls>strong{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  min-width:108px !important;
  min-height:36px;
  box-sizing:border-box;
  padding:0 12px;
  border:1px solid #bcd9e8;
  border-radius:10px;
  color:#114f73 !important;
  background:#eef8fd !important;
  font-size:13px !important;
  font-weight:800 !important;
  font-variant-numeric:tabular-nums;
  white-space:nowrap;
}
.pagination-nav{
  display:inline-flex !important;
  align-items:center;
  justify-content:center;
  gap:4px;
  min-width:82px;
  min-height:36px !important;
  box-sizing:border-box;
  padding:0 12px !important;
  border:1px solid #bfd7e5 !important;
  border-radius:10px !important;
  color:#17688f !important;
  background:#fff !important;
  box-shadow:none !important;
  font-size:13px;
  font-weight:750;
  cursor:pointer;
}
.pagination-nav:hover:not(:disabled){border-color:#5ca7cd !important;color:#0d5b82 !important;background:#edf8fd !important}
.pagination-nav:focus-visible,.pagination-jump button:focus-visible,.pagination-jump input:focus-visible{outline:3px solid rgba(13,137,211,.2);outline-offset:2px}
.pagination-nav:disabled{
  color:#9aabb5 !important;
  border-color:#dbe5ea !important;
  background:#f3f6f8 !important;
  opacity:1 !important;
  cursor:not-allowed;
}
.pagination-jump{
  display:flex !important;
  align-items:center;
  gap:5px !important;
  min-height:36px;
  box-sizing:border-box;
  padding:0 4px 0 10px;
  border:1px solid #cfdee7;
  border-radius:10px;
  color:#617b8c;
  background:#fff !important;
  font-size:12px;
  white-space:nowrap;
}
.pagination-jump input{
  width:48px;
  height:28px;
  box-sizing:border-box;
  padding:0 5px;
  border:0 !important;
  border-radius:7px;
  color:#173f58 !important;
  background:#f7fbfd !important;
  text-align:center;
  font-weight:800;
  appearance:textfield;
}
.pagination-jump input::-webkit-inner-spin-button,.pagination-jump input::-webkit-outer-spin-button{appearance:none;margin:0}
.pagination-jump button{
  min-width:48px;
  height:28px !important;
  padding:0 9px !important;
  border:1px solid #b9d8e8 !important;
  border-radius:7px !important;
  color:#17688f !important;
  background:#edf8fd !important;
  box-shadow:none !important;
  font-weight:750;
}
.pagination-jump button:hover:not(:disabled){color:#fff !important;border-color:#0d89d3 !important;background:#0d89d3 !important}
.pagination-jump button:disabled{color:#99aab4 !important;border-color:#dce6eb !important;background:#f2f5f7 !important;opacity:1 !important}
@media(max-width:760px){
  .pagination-controls{grid-template-columns:max-content max-content !important;justify-content:start}
  .pagination-jump{grid-column:1/-1}
}
</style>
