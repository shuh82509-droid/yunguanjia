<script setup lang="ts">
import { ref, watch } from 'vue'
import { AlertTriangle, X } from 'lucide-vue-next'

const props = withDefaults(defineProps<{ open: boolean; count: number; busy?: boolean }>(), { busy: false })
const emit = defineEmits<{ close: []; confirm: [confirmText: string] }>()
const confirmText = ref('')
const expected = '永久清空回收站'
watch(() => props.open, open => { if (open) confirmText.value = '' })
</script>

<template>
  <Teleport to="body">
    <Transition name="modal-fade">
      <div v-if="open" class="trash-clear-layer" @mousedown.self="!busy && emit('close')">
        <section class="trash-clear-modal" role="alertdialog" aria-modal="true" aria-label="永久清空回收站">
          <header><span><AlertTriangle :size="24" /></span><div><small>PERMANENT DELETE</small><h2>永久清空回收站？</h2></div><button aria-label="关闭" :disabled="busy" @click="emit('close')"><X :size="19" /></button></header>
          <p>回收站中的 <strong>{{ count.toLocaleString('zh-CN') }}</strong> 条素材将从平台和 OSS 中永久删除，无法恢复；投放与审计记录会继续保留。</p>
          <label><span>请输入“{{ expected }}”确认</span><input v-model="confirmText" :disabled="busy" autocomplete="off" :placeholder="expected" /></label>
          <footer><button class="secondary-button" :disabled="busy" @click="emit('close')">取消</button><button class="danger-button" :disabled="confirmText !== expected || busy" @click="emit('confirm', confirmText)">{{ busy ? '正在永久删除…' : '永久清空' }}</button></footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.trash-clear-layer { position: fixed; inset: 0; z-index: 130; display: grid; place-items: center; padding: 24px; background: rgba(33, 33, 36, .5); backdrop-filter: blur(8px); }.trash-clear-modal { width: min(520px, 100%); display: grid; gap: 18px; padding: 24px; border: 1px solid #efd3cf; border-radius: 22px; color: #4a2928; background: #fffdfa; box-shadow: 0 30px 80px rgba(55, 24, 24, .26); }.trash-clear-modal header { display: grid; grid-template-columns: 48px minmax(0, 1fr) 36px; align-items: start; gap: 14px; }.trash-clear-modal header > span { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 14px; color: #b8473e; background: #fff0ed; }.trash-clear-modal header small { color: #bb4e45; font-size: 11px; font-weight: 800; letter-spacing: 1.8px; }.trash-clear-modal h2 { margin: 5px 0 0; font-size: 24px; }.trash-clear-modal header button { width: 36px; height: 36px; display: grid; place-items: center; border: 1px solid #eadbd8; border-radius: 11px; color: #785b58; background: #fff; }.trash-clear-modal p { margin: 0; padding: 14px; border-radius: 13px; color: #7e514c; font-size: 14px; line-height: 1.7; background: #fff4f1; }.trash-clear-modal label { display: grid; gap: 8px; color: #694844; font-size: 13px; font-weight: 700; }.trash-clear-modal input { height: 46px; padding: 0 13px; border: 1px solid #e4c7c3; border-radius: 12px; outline: 0; color: #4a2928; font: inherit; background: #fff; }.trash-clear-modal input:focus { border-color: #c75c52; box-shadow: 0 0 0 3px rgba(199, 92, 82, .12); }.trash-clear-modal footer { display: flex; justify-content: flex-end; gap: 10px; }.trash-clear-modal footer button { min-width: 110px; }
</style>
