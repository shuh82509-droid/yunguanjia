<script setup lang="ts">
const props = withDefaults(defineProps<{ startDate: string; endDate: string; label?: string }>(), { label: '推送日期' })
const emit = defineEmits<{ 'update:startDate': [value: string]; 'update:endDate': [value: string] }>()

const localDateValue = (offsetDays = 0) => {
  const value = new Date()
  value.setDate(value.getDate() + offsetDays)
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`
}
const preset = (offsetDays?: number) => {
  const value = offsetDays === undefined ? '' : localDateValue(offsetDays)
  emit('update:startDate', value)
  emit('update:endDate', value)
}
</script>

<template>
  <div class="date-range-filter" :aria-label="`按${label}筛选`">
    <span>{{ label }}</span>
    <input :value="props.startDate" type="date" :aria-label="`${label}开始`" @input="emit('update:startDate', ($event.target as HTMLInputElement).value)" />
    <i>—</i>
    <input :value="props.endDate" type="date" :aria-label="`${label}结束`" @input="emit('update:endDate', ($event.target as HTMLInputElement).value)" />
    <button type="button" @click="preset(0)">今日</button>
    <button type="button" @click="preset(-1)">昨日</button>
    <button v-if="props.startDate || props.endDate" type="button" @click="preset()">全部</button>
  </div>
</template>

<style scoped>
.date-range-filter { flex:1 1 100%; display:flex; align-items:center; justify-content:flex-end; gap:6px; color:#5f7b8e; }
.date-range-filter span { font-size:12px; font-weight:800; white-space:nowrap; }
.date-range-filter i { font-style:normal; color:#9cafbb; }
.date-range-filter input { width:132px; height:36px; box-sizing:border-box; padding:0 8px; border:1px solid #cfe2ed; border-radius:9px; color:#31566e; background:#fff; }
.date-range-filter button { height:34px; padding:0 9px; border:1px solid #cfe2ed; border-radius:9px; color:#167dad; background:#fff; }
@media (max-width:860px) { .date-range-filter { min-width:650px; justify-content:flex-start; } }
</style>
