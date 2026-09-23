<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

defineProps<{ src: string; alt: string }>()

const host = ref<HTMLElement | null>(null)
const active = ref(false)
const ready = ref(false)
const failed = ref(false)
let observer: IntersectionObserver | null = null

const loadWhenVisible = () => {
  active.value = true
  observer?.disconnect()
  observer = null
}

const seekToCoverFrame = (event: Event) => {
  const video = event.currentTarget as HTMLVideoElement
  if (!Number.isFinite(video.duration) || video.duration <= 0) {
    ready.value = true
    return
  }

  try {
    video.currentTime = Math.min(1, Math.max(0.12, video.duration * 0.04))
  } catch {
    ready.value = true
  }
}

onMounted(() => {
  if (!('IntersectionObserver' in window) || !host.value) {
    loadWhenVisible()
    return
  }

  observer = new IntersectionObserver((entries) => {
    if (entries.some(entry => entry.isIntersecting)) loadWhenVisible()
  }, { rootMargin: '260px 0px' })
  observer.observe(host.value)
})

onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div ref="host" class="video-cover-shell" role="img" :aria-label="`${alt} 的视频封面`">
    <video
      v-if="active && !failed"
      class="video-cover"
      :class="{ ready }"
      :src="src"
      muted
      playsinline
      preload="metadata"
      tabindex="-1"
      @loadedmetadata="seekToCoverFrame"
      @loadeddata="ready = true"
      @seeked="ready = true"
      @error="failed = true"
    />
    <div v-if="!ready" class="video-cover-state" :class="{ failed }">
      <span>WIS</span>
      <small>{{ failed ? '封面读取失败' : '正在读取视频封面' }}</small>
    </div>
  </div>
</template>
