<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { FolderPlus, Tags, X } from 'lucide-vue-next'

const props = withDefaults(defineProps<{
  open: boolean
  mode: 'tags' | 'folder'
  count: number
  folders?: string[]
  busy?: boolean
}>(), { folders: () => [], busy: false })
const emit = defineEmits<{
  close: []
  submit: [payload: { tags?: string[]; tagMode?: 'add' | 'remove' | 'replace'; folderName?: string }]
}>()

const tagText = ref('')
const tagMode = ref<'add' | 'remove' | 'replace'>('add')
const folderName = ref('')
const tags = computed(() => Array.from(new Set(tagText.value
  .split(/[,，;；\n]+/)
  .map(item => item.trim())
  .filter(Boolean)))
  .slice(0, 30))
const canSubmit = computed(() => props.mode === 'tags' ? tags.value.length > 0 : folderName.value.trim().length <= 160)

watch(() => props.open, open => {
  if (!open) return
  tagText.value = ''
  tagMode.value = 'add'
  folderName.value = ''
})

const submit = () => {
  if (!canSubmit.value || props.busy) return
  if (props.mode === 'tags') emit('submit', { tags: tags.value, tagMode: tagMode.value })
  else emit('submit', { folderName: folderName.value.trim() })
}
</script>

<template>
  <Teleport to="body">
    <Transition name="modal-fade">
      <div v-if="open" class="organize-layer" @mousedown.self="!busy && emit('close')">
        <section class="organize-modal" role="dialog" aria-modal="true" :aria-label="mode === 'tags' ? '批量打标签' : '整理到二级文件夹'">
          <header>
            <span class="organize-icon"><Tags v-if="mode === 'tags'" :size="22" /><FolderPlus v-else :size="22" /></span>
            <div><small>{{ mode === 'tags' ? 'BATCH TAGGING' : 'SECONDARY FOLDER' }}</small><h2>{{ mode === 'tags' ? `给 ${count} 条素材批量打标签` : `整理 ${count} 条视频素材` }}</h2><p>{{ mode === 'tags' ? '一次添加、移除或替换营销标签，原始文件不会发生变化。' : '建立团队可见的二级文件夹，同一达人或项目的素材更容易集中查找。' }}</p></div>
            <button aria-label="关闭" :disabled="busy" @click="emit('close')"><X :size="19" /></button>
          </header>

          <template v-if="mode === 'tags'">
            <label class="organize-field"><span>处理方式</span><select v-model="tagMode" :disabled="busy"><option value="add">添加到现有标签</option><option value="remove">从现有标签移除</option><option value="replace">替换全部标签</option></select></label>
            <label class="organize-field"><span>营销标签</span><textarea v-model="tagText" :disabled="busy" rows="3" placeholder="输入标签，支持逗号或换行分隔；最多 30 个"></textarea><small>已识别 {{ tags.length }} 个标签</small></label>
          </template>
          <template v-else>
            <label class="organize-field"><span>二级文件夹名称</span><input v-model="folderName" list="asset-folder-options" maxlength="160" :disabled="busy" placeholder="例如：KOC原片-林凡清（留空可移出文件夹）" /><small>输入新名称即可创建；留空保存会移出当前文件夹。</small></label>
            <datalist id="asset-folder-options"><option v-for="folder in folders" :key="folder" :value="folder" /></datalist>
          </template>

          <footer><button class="secondary-button" :disabled="busy" @click="emit('close')">取消</button><button class="primary-button" :disabled="!canSubmit || busy" @click="submit">{{ busy ? '保存中…' : mode === 'tags' ? '保存标签' : '保存文件夹' }}</button></footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.organize-layer { position: fixed; inset: 0; z-index: 120; display: grid; place-items: center; padding: 24px; background: rgba(20, 50, 72, .42); backdrop-filter: blur(8px); }
.organize-modal { width: min(560px, 100%); display: grid; gap: 18px; padding: 24px; border: 1px solid #d7e7ef; border-radius: 22px; color: #17364d; background: #fffdf8; box-shadow: 0 30px 80px rgba(16, 58, 84, .22); }
.organize-modal header { display: grid; grid-template-columns: 48px minmax(0, 1fr) 36px; align-items: start; gap: 14px; }
.organize-icon { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 14px; color: #147fbd; background: #e9f6fd; }
.organize-modal header small { color: #278ec5; font-size: 11px; font-weight: 800; letter-spacing: 1.8px; }
.organize-modal h2 { margin: 5px 0 4px; font-size: 23px; line-height: 1.25; }.organize-modal p { margin: 0; color: #718797; font-size: 14px; line-height: 1.6; }
.organize-modal header > button { width: 36px; height: 36px; display: grid; place-items: center; border: 1px solid #dbe8ef; border-radius: 11px; color: #58768a; background: #fff; }
.organize-field { display: grid; gap: 8px; color: #355a73; font-size: 13px; font-weight: 700; }.organize-field input, .organize-field select, .organize-field textarea { width: 100%; border: 1px solid #cfe1eb; border-radius: 12px; outline: 0; color: #17364d; font: inherit; font-weight: 500; background: #f8fcfe; }.organize-field input, .organize-field select { height: 46px; padding: 0 13px; }.organize-field textarea { resize: vertical; min-height: 92px; padding: 12px 13px; line-height: 1.6; }.organize-field input:focus, .organize-field select:focus, .organize-field textarea:focus { border-color: #2698d0; box-shadow: 0 0 0 3px rgba(38, 152, 208, .12); }.organize-field small { color: #8aa0af; font-size: 12px; font-weight: 500; }
.organize-modal footer { display: flex; justify-content: flex-end; gap: 10px; padding-top: 3px; }.organize-modal footer button { min-width: 108px; }
@media (max-width: 560px) { .organize-layer { padding: 12px; }.organize-modal { padding: 19px; }.organize-modal header { grid-template-columns: 42px minmax(0, 1fr) 34px; }.organize-icon { width: 42px; height: 42px; }.organize-modal h2 { font-size: 20px; } }
</style>
