<script setup lang="ts">
import { ref, watch } from 'vue'
import { ShieldCheck, X } from 'lucide-vue-next'
import type { ReviewEligibleReviewer, ReviewWorkflowConfig } from '../types'
import ReviewerAssignmentPicker from './ReviewerAssignmentPicker.vue'

const props = defineProps<{ open: boolean; count: number; config: ReviewWorkflowConfig | null; busy: boolean }>()
const emit = defineEmits<{
  close: []
  submit: [payload: { note: string; assignment_mode: 'organization' | 'designated'; designated_reviewer_number: string; designated_reviewer_name: string }]
}>()

const assignmentMode = ref<'organization' | 'designated'>('organization')
const selectedReviewer = ref<ReviewEligibleReviewer | null>(null)
const note = ref('')

watch(() => props.open, open => {
  if (!open) return
  assignmentMode.value = 'organization'
  selectedReviewer.value = null
  note.value = ''
})

const submit = () => {
  if (assignmentMode.value === 'designated' && !selectedReviewer.value) return
  emit('submit', {
    note: note.value.trim(),
    assignment_mode: assignmentMode.value,
    designated_reviewer_number: selectedReviewer.value?.user_number || '',
    designated_reviewer_name: selectedReviewer.value?.user_name || '',
  })
}
</script>

<template>
  <div v-if="open" class="modal-backdrop" @click.self="!busy && emit('close')">
    <section class="review-batch-modal" role="dialog" aria-modal="true" aria-labelledby="review-batch-title">
      <header><span><small>WIS REVIEW WORKFLOW</small><strong id="review-batch-title">批量提审 {{ count }} 条素材</strong></span><button type="button" aria-label="关闭" :disabled="busy" @click="emit('close')"><X /></button></header>
      <ReviewerAssignmentPicker :mode="assignmentMode" :reviewers="config?.eligible_reviewers || []" :selected="selectedReviewer" @update:mode="assignmentMode = $event" @select="selectedReviewer = $event" />
      <label class="review-batch-note"><span>审核说明（选填）</span><textarea v-model="note" maxlength="1000" placeholder="这段说明会同步到本批次的每一条素材" /></label>
      <footer><button type="button" class="cancel" :disabled="busy" @click="emit('close')">取消</button><button type="button" class="submit" :disabled="busy || (assignmentMode === 'designated' && !selectedReviewer)" @click="submit"><ShieldCheck />{{ busy ? '提交中…' : `确认提审 ${count} 条` }}</button></footer>
    </section>
  </div>
</template>

<style scoped>
.modal-backdrop{position:fixed;inset:0;z-index:1300;display:grid;place-items:center;padding:20px;background:rgba(16,39,54,.45);backdrop-filter:blur(5px)}.review-batch-modal{display:grid;gap:16px;width:min(760px,calc(100vw - 32px));max-height:calc(100vh - 40px);overflow:auto;padding:22px;border:1px solid #d5e6ef;border-radius:22px;background:#fff;box-shadow:0 24px 70px rgba(16,49,70,.24)}.review-batch-modal>header{display:flex;align-items:flex-start;justify-content:space-between}.review-batch-modal>header>span{display:grid;gap:5px}.review-batch-modal>header small{color:#1085c5;font-size:11px;font-weight:900;letter-spacing:.13em}.review-batch-modal>header strong{color:#123b5a;font-size:24px}.review-batch-modal>header button{display:grid;place-items:center;width:38px;height:38px;border:1px solid #cfe1eb;border-radius:11px;color:#3d687f;background:#fff}.review-batch-modal>header svg{width:18px}.review-batch-note{display:grid;gap:6px;color:#365f75;font-size:12px;font-weight:800}.review-batch-note textarea{min-height:76px;resize:vertical;padding:11px;border:1px solid #cbdfe9;border-radius:11px;color:#173f5c;background:#fff}.review-batch-modal>footer{display:flex;justify-content:flex-end;gap:9px}.review-batch-modal>footer button{display:flex;align-items:center;justify-content:center;gap:6px;min-width:112px;height:42px;border-radius:11px;font-weight:800}.review-batch-modal>footer svg{width:16px}.cancel{color:#446b80;border:1px solid #cadde7;background:#fff}.submit{color:#fff;border:1px solid #0e88cd;background:#0e88cd}.review-batch-modal button:disabled{opacity:.5;cursor:not-allowed}
</style>
