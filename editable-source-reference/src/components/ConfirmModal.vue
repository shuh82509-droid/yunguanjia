<script setup lang="ts">
import { AlertTriangle, LoaderCircle, Trash2, X } from 'lucide-vue-next'

withDefaults(defineProps<{
  open: boolean
  title: string
  description: string
  confirmLabel: string
  danger?: boolean
  busy?: boolean
}>(), { danger: false, busy: false })

const emit = defineEmits<{ close: []; confirm: [] }>()
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer">
      <div v-if="open" class="confirm-layer" role="presentation" @click.self="emit('close')">
        <section class="confirm-modal" role="alertdialog" aria-modal="true" :aria-label="title">
          <button class="icon-button confirm-close" aria-label="关闭" :disabled="busy" @click="emit('close')"><X :size="19" /></button>
          <span class="confirm-icon" :class="{ danger }"><AlertTriangle v-if="danger" :size="24" /><Trash2 v-else :size="24" /></span>
          <h2>{{ title }}</h2>
          <p>{{ description }}</p>
          <footer>
            <button class="secondary-button" :disabled="busy" @click="emit('close')">取消</button>
            <button :class="danger ? 'danger-button' : 'primary-button'" :disabled="busy" @click="emit('confirm')">
              <LoaderCircle v-if="busy" class="spin" :size="16" />{{ busy ? '处理中…' : confirmLabel }}
            </button>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
