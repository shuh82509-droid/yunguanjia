<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { Bell, CheckCheck, ClipboardCheck } from "lucide-vue-next";
import { api } from "../api";
import type { UserNotification } from "../types";

const emit = defineEmits<{ openRequest: [requestId: string] }>();
const open = ref(false);
const items = ref<UserNotification[]>([]);
const unread = ref(0);
const loading = ref(false);
let timer: number | undefined;

const load = async () => {
  loading.value = true;
  try {
    const data = await api.notifications(20);
    items.value = data.items;
    unread.value = data.unread;
  } finally {
    loading.value = false;
  }
};
const toggle = () => {
  open.value = !open.value;
  if (open.value) void load();
};
const readAll = async () => {
  await api.notificationsReadAll();
  await load();
};
const select = async (item: UserNotification) => {
  if (!item.read_at) await api.notificationRead(item.id).catch(() => undefined);
  open.value = false;
  if (item.resource_type === "video_request" && item.resource_id)
    emit("openRequest", item.resource_id);
  void load();
};
const formatDate = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));

onMounted(() => {
  void load();
  timer = window.setInterval(load, 60000);
});
onBeforeUnmount(() => {
  if (timer) window.clearInterval(timer);
});
</script>

<template>
  <div class="notification-center">
    <button
      class="notification-trigger"
      aria-label="通知中心"
      :aria-expanded="open"
      @click="toggle"
    >
      <Bell :size="19" /><span v-if="unread">{{
        unread > 99 ? "99+" : unread
      }}</span>
    </button>
    <Transition name="notice-pop">
      <div v-if="open" class="notification-popover">
        <header>
          <span
            ><strong>通知中心</strong
            ><small>{{ unread ? `${unread} 条未读` : "暂无未读" }}</small></span
          ><button v-if="unread" @click="readAll">
            <CheckCheck :size="15" />全部已读
          </button>
        </header>
        <div v-if="loading && !items.length" class="notification-empty">
          正在读取通知…
        </div>
        <div v-else-if="!items.length" class="notification-empty">
          <Bell :size="25" /><span>还没有新通知</span>
        </div>
        <button
          v-for="item in items"
          :key="item.id"
          class="notification-item"
          :class="{ unread: !item.read_at }"
          @click="select(item)"
        >
          <span class="notification-icon"><ClipboardCheck :size="18" /></span
          ><span
            ><strong>{{ item.title }}</strong
            ><small>{{ item.message }}</small
            ><em>{{ formatDate(item.created_at) }} · {{ item.external_status === 'sent' ? '飞书已同步' : item.external_status === 'failed' ? '飞书待重试' : '站内已提醒' }}</em></span
          ><i v-if="!item.read_at"></i>
        </button>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.notification-center {
  position: relative;
}
.notification-trigger {
  position: relative;
  display: grid;
  place-items: center;
  width: 42px;
  height: 42px;
  border: 1px solid #cbdce6;
  border-radius: 14px;
  background: #fff;
  color: #1f5e82;
}
.notification-trigger > span {
  position: absolute;
  right: -5px;
  top: -6px;
  display: grid;
  place-items: center;
  min-width: 18px;
  height: 18px;
  padding: 0 4px;
  border: 2px solid #fff;
  border-radius: 999px;
  background: #e5574f;
  color: white;
  font-size: 10px;
  font-weight: 900;
}
.notification-popover {
  position: absolute;
  z-index: 80;
  right: 0;
  top: 51px;
  width: min(390px, calc(100vw - 32px));
  max-height: 510px;
  overflow: auto;
  border: 1px solid #cbdfe9;
  border-radius: 20px;
  background: #fff;
  box-shadow: 0 24px 60px rgba(22, 64, 89, 0.2);
}
.notification-popover header {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 17px;
  border-bottom: 1px solid #e0ebf1;
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(10px);
}
.notification-popover header > span {
  display: grid;
}
.notification-popover header small {
  color: #7891a1;
}
.notification-popover header button {
  display: flex;
  gap: 5px;
  align-items: center;
  border: 0;
  background: transparent;
  color: #1682bc;
  font-weight: 800;
}
.notification-item {
  position: relative;
  display: grid;
  grid-template-columns: 40px 1fr auto;
  gap: 10px;
  width: 100%;
  padding: 14px 16px;
  border: 0;
  border-bottom: 1px solid #edf2f5;
  background: #fff;
  text-align: left;
  color: #183d58;
}
.notification-item:hover,
.notification-item.unread {
  background: #f1f9fd;
}
.notification-icon {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 12px;
  background: #e4f4fb;
  color: #1684c0;
}
.notification-item > span:nth-child(2) {
  display: grid;
  gap: 3px;
}
.notification-item small {
  line-height: 1.45;
  color: #59788d;
}
.notification-item em {
  font-size: 11px;
  font-style: normal;
  color: #91a5b2;
}
.notification-item > i {
  width: 8px;
  height: 8px;
  margin-top: 7px;
  border-radius: 50%;
  background: #1688c7;
}
.notification-empty {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
  justify-content: center;
  min-height: 150px;
  color: #7b94a4;
}
.notice-pop-enter-active,
.notice-pop-leave-active {
  transition: 0.18s ease;
}
.notice-pop-enter-from,
.notice-pop-leave-to {
  opacity: 0;
  transform: translateY(-8px) scale(0.98);
}
</style>
