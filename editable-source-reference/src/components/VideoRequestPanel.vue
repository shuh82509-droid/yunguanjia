<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import {
  ArrowRight,
  Check,
  CirclePlay,
  ExternalLink,
  FileImage,
  FileVideo,
  ListFilter,
  Link2,
  List,
  MessageSquareText,
  PanelsTopLeft,
  Plus,
  RefreshCw,
  Search,
  Send,
  Trash2,
  Undo2,
  UploadCloud,
  UserRoundCheck,
  Video,
  X,
} from "lucide-vue-next";
import { api } from "../api";
import PaginationControls from "./PaginationControls.vue";
import { uploadFileMultipart } from "../utils/multipartUpload";
import { CLEANSER_PRODUCT_CATEGORIES } from "../utils/productCategories";
import type {
  OaPermissions,
  OaUser,
  VideoRequestAssignee,
  VideoRequestItem,
  VideoRequestPage,
} from "../types";

const props = defineProps<{
  user: OaUser;
  permissions: OaPermissions;
  focusId?: string;
}>();
const emit = defineEmits<{ changed: [] }>();

const productOptions = [
  "通用",
  "隐形水润面膜",
  "黑晶面膜",
  "燕窝面膜",
  "晶润眼膜",
  "深海次抛",
  "肌活蛋白喷雾",
  "微针",
  "美白针",
  "颈膜",
  "凝颜",
  ...CLEANSER_PRODUCT_CATEGORIES,
];
const statusOptions = [
  { value: "all", label: "全部进度" },
  { value: "production_pending", label: "待制作" },
  { value: "completed", label: "已完成" },
  { value: "returned", label: "已退回" },
];
const sourceSubtypeOptions = [
  "明星信息流原片",
  "达人/KOL原片",
  "达人/KOC原片",
  "实拍自产素材",
  "产品镜",
  "AI原创素材",
  "品牌创意广告",
  "品牌IP广告",
  "其他视频素材",
];
const contentTypeOptions = ["其他", "上脸展示", "数字人", "图文", "产品展示", "痛点", "测评", "口播", "剧情", "科普"];
type DeliveryDraft = {
  id: string;
  file: File;
  category: string;
  assetSubtype: string;
  contentType: string;
  folderName: string;
  tags: string;
  progress: number;
  stage: string;
  assetId?: number;
};
const eventLabels: Record<string, string> = {
  submitted: "提交需求",
  assigned: "分配制作人",
  started: "开始制作",
  delivered: "上传成片",
  revision_requested: "提出修改意见",
  accepted: "确认完成",
  returned: "退回需求",
};

const data = ref<VideoRequestPage>({
  items: [],
  total: 0,
  page: 1,
  page_size: 10,
  total_pages: 1,
  scope: "mine",
});
const scope = ref<"mine" | "assigned" | "all">(
  props.permissions.video_request_supervisor_viewer ? "all" : "mine",
);
const listMode = ref<"table" | "cards">("table");
const status = ref("all");
const query = ref("");
const page = ref(1);
const pageSize = ref<5 | 10 | 20 | 30>(10);
const loading = ref(false);
const error = ref("");
const creating = ref(false);
const createOpen = ref(true);
const createProduct = ref("通用");
const createDescription = ref("");
const createUrl = ref("");
const referenceVideos = ref<File[]>([]);
const referenceVideoUrls = ref<string[]>([]);
const referenceImages = ref<File[]>([]);
const referenceImageUrls = ref<string[]>([]);
const referenceProgress = ref(0);
const referenceStage = ref("");
const createProgress = ref(0);
const createStage = ref("");
const assignees = ref<VideoRequestAssignee[]>([]);
const assigneeQuery = ref("");
const assigneeSelected = ref<Record<string, string>>({});
const busyId = ref("");
const feedback = ref<Record<string, string>>({});
const deliveryDrafts = ref<Record<string, DeliveryDraft[]>>({});
const deliveryNotes = ref<Record<string, string>>({});
const returnReasons = ref<Record<string, string>>({});
const expanded = ref<Record<string, boolean>>({});
const referencePreviewBusy = ref<Record<string, boolean>>({});
const toast = ref("");
let queryTimer: number | undefined;
const referencePreviewTimers = new Map<string, number>();

const canSeeAll = computed(
  () =>
    props.permissions.operation_admin ||
    props.permissions.video_request_assigner ||
    props.permissions.video_request_supervisor_viewer,
);
const activeCount = computed(
  () =>
    data.value.items.filter((item) => !["accepted", "returned"].includes(item.status))
      .length,
);
const formatDate = (value?: string | null) =>
  value
    ? new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      }).format(new Date(value))
    : "—";
const formatMoney = (value?: number | null) =>
  value === null || value === undefined
    ? "待回流"
    : `¥${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`;
const displayPerson = (name?: string | null, number?: string | null) => {
  if (
    (number && number === props.user.number) ||
    (name && name === props.user.realName)
  )
    return "我";
  return name || "未记录";
};
const showToast = (message: string) => {
  toast.value = message;
  window.setTimeout(() => {
    toast.value = "";
  }, 3200);
};
const referenceHref = (value: string) =>
  value.match(/https?:\/\/[^\s]+/i)?.[0] || "";
const replaceRequestItem = (updated: VideoRequestItem) => {
  const index = data.value.items.findIndex((item) => item.id === updated.id);
  if (index >= 0) data.value.items.splice(index, 1, updated);
};
const pollReferencePreviews = async (requestId: string, attempt = 0) => {
  referencePreviewTimers.delete(requestId);
  try {
    const updated = await api.videoRequest(requestId);
    replaceRequestItem(updated);
    const waiting = updated.reference_videos.some((video) =>
      ["pending", "processing"].includes(video.preview_status || "pending"),
    );
    if (waiting && attempt < 450) {
      const timer = window.setTimeout(
        () => void pollReferencePreviews(requestId, attempt + 1),
        2000,
      );
      referencePreviewTimers.set(requestId, timer);
    }
  } catch {
    if (attempt < 15) {
      const timer = window.setTimeout(
        () => void pollReferencePreviews(requestId, attempt + 1),
        3000,
      );
      referencePreviewTimers.set(requestId, timer);
    }
  }
};
const ensureReferencePreviews = async (item: VideoRequestItem) => {
  if (!item.reference_videos.length) return;
  if (item.reference_videos.every((video) => video.preview_status === "ready")) return;
  if (referencePreviewBusy.value[item.id] || referencePreviewTimers.has(item.id)) return;
  referencePreviewBusy.value[item.id] = true;
  try {
    await api.videoRequestReferencePreviews(item.id);
    await pollReferencePreviews(item.id);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "参考视频预览生成失败";
  } finally {
    referencePreviewBusy.value[item.id] = false;
  }
};
const openTask = (item: VideoRequestItem) => {
  listMode.value = "cards";
  expanded.value[item.id] = true;
  void ensureReferencePreviews(item);
  window.setTimeout(() => {
    document
      .querySelector(`[data-request-id="${item.id}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "start" });
  });
};
const openReview = (item: VideoRequestItem) => {
  listMode.value = "cards";
  expanded.value[item.id] = true;
  void ensureReferencePreviews(item);
  window.setTimeout(() => {
    document
      .querySelector(`[data-review-request-id="${item.id}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
};
const toggleTask = (item: VideoRequestItem) => {
  expanded.value[item.id] = !expanded.value[item.id];
  if (expanded.value[item.id]) void ensureReferencePreviews(item);
};

const load = async () => {
  loading.value = true;
  error.value = "";
  try {
    data.value = await api.videoRequests(
      scope.value,
      status.value,
      query.value.trim(),
      page.value,
      pageSize.value,
    );
    if (props.focusId) {
      const focused = await api.videoRequest(props.focusId).catch(() => null);
      if (focused && !data.value.items.some((item) => item.id === focused.id))
        data.value.items.unshift(focused);
      if (focused) {
        expanded.value[focused.id] = true;
        void ensureReferencePreviews(focused);
      }
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "提需列表加载失败";
  } finally {
    loading.value = false;
  }
};

const loadAssignees = async () => {
  if (!props.permissions.video_request_assigner) return;
  const result = await api
    .videoRequestAssignees(assigneeQuery.value)
    .catch(() => ({ items: [], total: 0 }));
  assignees.value = result.items;
};

const createRequest = async () => {
  if (creating.value) return;
  if (
    !createDescription.value.trim() &&
    !createUrl.value.trim() &&
    !referenceVideos.value.length &&
    !referenceImages.value.length
  ) {
    error.value = "请至少提供竞对链接、参考视频、参考图片或语言描述中的一项。";
    return;
  }
  creating.value = true;
  createProgress.value = 5;
  createStage.value = "正在校验需求内容";
  error.value = "";
  try {
    const referenceVideoEntries: { object_key: string; filename: string }[] = [];
    const referenceImageEntries: { object_key: string; filename: string }[] = [];
    const attachmentTotal = referenceVideos.value.length + referenceImages.value.length;
    for (let index = 0; index < referenceVideos.value.length; index += 1) {
      const file = referenceVideos.value[index];
      referenceProgress.value = 0;
      const session = await uploadFileMultipart(
        file,
        { assetScope: "reference_video", category: "参考视频" },
        {
          onStage: (value) => {
            referenceStage.value = value;
            createStage.value = `参考视频 ${index + 1}/${referenceVideos.value.length}：${value}`;
          },
          onProgress: (value) => {
            referenceProgress.value = value;
            const completed = index * 100 + value;
            createProgress.value = Math.max(5, Math.round(completed / Math.max(attachmentTotal, 1) * 0.85));
          },
        },
      );
      referenceVideoEntries.push({ object_key: session.object_key, filename: file.name });
    }
    for (let index = 0; index < referenceImages.value.length; index += 1) {
      const file = referenceImages.value[index];
      const session = await uploadFileMultipart(
        file,
        { assetScope: "reference_video", category: "参考图片" },
        {
          onStage: (value) => { createStage.value = `参考图片 ${index + 1}/${referenceImages.value.length}：${value}`; },
          onProgress: (value) => {
            const completed = (referenceVideos.value.length + index) * 100 + value;
            createProgress.value = Math.max(5, Math.round(completed / Math.max(attachmentTotal, 1) * 0.85));
          },
        },
      );
      referenceImageEntries.push({ object_key: session.object_key, filename: file.name });
    }
    createProgress.value = 95;
    createStage.value = "正在建立任务并发送提醒";
    const item = await api.videoRequestCreate({
      product: createProduct.value,
      description: createDescription.value.trim(),
      reference_url: createUrl.value.trim(),
      reference_videos: referenceVideoEntries,
      reference_images: referenceImageEntries,
    });
    createDescription.value = "";
    createUrl.value = "";
    referenceVideoUrls.value.forEach((url) => URL.revokeObjectURL(url));
    referenceVideos.value = [];
    referenceVideoUrls.value = [];
    referenceImageUrls.value.forEach((url) => URL.revokeObjectURL(url));
    referenceImages.value = [];
    referenceImageUrls.value = [];
    referenceProgress.value = 0;
    referenceStage.value = "";
    createProgress.value = 100;
    createStage.value = "需求已提交";
    expanded.value[item.id] = true;
    page.value = 1;
    await load();
    emit("changed");
    showToast("提需已提交，视频中心主管会收到分配提醒");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "提交失败";
  } finally {
    creating.value = false;
    window.setTimeout(() => {
      createProgress.value = 0;
      createStage.value = "";
    }, 500);
  }
};

const chooseReferenceVideos = (files: FileList | null) => {
  const accepted: File[] = [];
  for (const file of Array.from(files || [])) {
    if (!file.type.startsWith("video/") || file.size <= 0 || file.size > 500 * 1024 ** 2) {
      error.value = `${file.name} 不是可用视频或超过 500MB`;
      continue;
    }
    if (referenceVideos.value.length + accepted.length >= 9) {
      error.value = "参考视频最多上传 9 条";
      break;
    }
    accepted.push(file);
  }
  referenceVideos.value = [...referenceVideos.value, ...accepted];
  referenceVideoUrls.value = [...referenceVideoUrls.value, ...accepted.map((file) => URL.createObjectURL(file))];
};

const removeReferenceVideo = (index: number) => {
  URL.revokeObjectURL(referenceVideoUrls.value[index]);
  referenceVideos.value = referenceVideos.value.filter((_, current) => current !== index);
  referenceVideoUrls.value = referenceVideoUrls.value.filter((_, current) => current !== index);
};

const chooseReferenceImages = (files: FileList | null) => {
  const incoming = Array.from(files || []);
  const accepted: File[] = [];
  for (const file of incoming) {
    if (!file.type.startsWith("image/") || file.size <= 0 || file.size > 20 * 1024 ** 2) {
      error.value = `${file.name} 不是可用图片或超过 20MB`;
      continue;
    }
    if (referenceImages.value.length + accepted.length >= 9) break;
    accepted.push(file);
  }
  referenceImages.value = [...referenceImages.value, ...accepted];
  referenceImageUrls.value = [...referenceImageUrls.value, ...accepted.map((file) => URL.createObjectURL(file))];
};

const removeReferenceImage = (index: number) => {
  URL.revokeObjectURL(referenceImageUrls.value[index]);
  referenceImages.value = referenceImages.value.filter((_, current) => current !== index);
  referenceImageUrls.value = referenceImageUrls.value.filter((_, current) => current !== index);
};

const assign = async (item: VideoRequestItem) => {
  const selectedKey = assigneeSelected.value[item.id];
  const selected = assignees.value.find(
    (candidate) => `${candidate.number}|${candidate.name}` === selectedKey,
  );
  if (!selected) {
    error.value = "请先选择制作人";
    return;
  }
  busyId.value = item.id;
  try {
    await api.videoRequestAssign(item.id, selected);
    await load();
    expanded.value[item.id] = false;
    showToast(`已分配给 ${selected.name}`);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "分配失败";
  } finally {
    busyId.value = "";
  }
};

const start = async (item: VideoRequestItem) => {
  busyId.value = item.id;
  try {
    await api.videoRequestStart(item.id);
    await load();
    showToast("已进入制作中");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "操作失败";
  } finally {
    busyId.value = "";
  }
};

const chooseDelivery = (requestId: string, files: FileList | null, defaultCategory = "通用") => {
  const current = deliveryDrafts.value[requestId] || [];
  const incoming = Array.from(files || []);
  const next = [...current];
  for (const file of incoming) {
    const looksLikeVideo = file.type.startsWith("video/") || /\.(mp4|mov|m4v|webm|avi)$/i.test(file.name);
    if (!looksLikeVideo || file.size <= 0 || file.size > 5 * 1024 ** 3) {
      error.value = `${file.name} 不是可用视频或超过 5GB`;
      continue;
    }
    if (next.length >= 10) break;
    const id = `${file.name}-${file.size}-${file.lastModified}`;
    if (next.some((draft) => draft.id === id)) continue;
    next.push({
      id,
      file,
      category: defaultCategory,
      assetSubtype: "AI原创素材",
      contentType: "其他",
      folderName: "",
      tags: "",
      progress: 0,
      stage: "等待上传",
    });
  }
  deliveryDrafts.value[requestId] = next;
};

const removeDelivery = (requestId: string, draftId: string) => {
  deliveryDrafts.value[requestId] = (deliveryDrafts.value[requestId] || []).filter((draft) => draft.id !== draftId);
};

const deliver = async (item: VideoRequestItem) => {
  const drafts = deliveryDrafts.value[item.id] || [];
  if (!drafts.length) {
    error.value = "请先选择本次要提交的成片";
    return;
  }
  busyId.value = item.id;
  error.value = "";
  try {
    const assetIds = await Promise.all(drafts.map(async (draft) => {
      if (draft.assetId) return draft.assetId;
      const session = await uploadFileMultipart(
        draft.file,
        { assetScope: "marketing_video", category: draft.category || item.product },
        {
          onStage: (value) => { draft.stage = value; },
          onProgress: (value) => { draft.progress = value; },
        },
      );
      draft.stage = "正在写入一创素材库";
      const customTags = draft.tags.split(/[，,]/).map((tag) => tag.trim()).filter(Boolean);
      const asset = await api.completeUpload({
        object_key: session.object_key,
        filename: draft.file.name,
        category: draft.category || item.product,
        content_type: draft.contentType,
        asset_scope: "marketing_video",
        library_type: "source",
        asset_subtype: draft.assetSubtype,
        folder_name: draft.folderName,
        tags: ["视频中心提需", `提需-${item.id.slice(0, 8)}`, ...customTags],
        reference_url: item.reference_url,
        reference_video_key: item.reference_video_key,
        reference_video_name: item.reference_video_name,
      });
      draft.assetId = asset.id;
      draft.progress = 100;
      draft.stage = "已进入一创素材库";
      return asset.id;
    }));
    await api.videoRequestDeliver(item.id, assetIds, deliveryNotes.value[item.id] || "");
    deliveryDrafts.value[item.id] = [];
    deliveryNotes.value[item.id] = "";
    await load();
    emit("changed");
    showToast(`${assetIds.length} 条成片已按各自分类进入一创素材库，提需同事已收到验收提醒`);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "成片提交失败";
  } finally {
    busyId.value = "";
  }
};

const returnRequest = async (item: VideoRequestItem) => {
  const reason = (returnReasons.value[item.id] || "").trim();
  if (reason.length < 2) {
    error.value = "请填写退回理由（至少 2 个字）";
    return;
  }
  busyId.value = item.id;
  try {
    await api.videoRequestReturn(item.id, reason);
    returnReasons.value[item.id] = "";
    expanded.value[item.id] = false;
    await load();
    emit("changed");
    showToast("需求已退回，提需同事已收到理由");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "退回失败";
  } finally {
    busyId.value = "";
  }
};

const accept = async (item: VideoRequestItem) => {
  busyId.value = item.id;
  try {
    await api.videoRequestAccept(item.id);
    await load();
    showToast("成片已确认完成");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "确认失败";
  } finally {
    busyId.value = "";
  }
};

const requestRevision = async (item: VideoRequestItem) => {
  const value = feedback.value[item.id]?.trim();
  if (!value) {
    error.value = "请先填写明确的修改意见";
    return;
  }
  busyId.value = item.id;
  try {
    await api.videoRequestRevision(item.id, value);
    feedback.value[item.id] = "";
    await load();
    showToast("修改意见已通知制作人和视频中心主管");
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : "反馈失败";
  } finally {
    busyId.value = "";
  }
};

watch([scope, status, pageSize], () => {
  page.value = 1;
  void load();
});
watch(query, () => {
  page.value = 1;
  if (queryTimer) window.clearTimeout(queryTimer);
  queryTimer = window.setTimeout(load, 250);
});
watch(
  () => props.focusId,
  () => {
    if (props.focusId) void load();
  },
);
watch(assigneeQuery, () => {
  window.setTimeout(loadAssignees, 180);
});
onMounted(() => {
  void load();
  void loadAssignees();
});
onBeforeUnmount(() => {
  if (queryTimer) window.clearTimeout(queryTimer);
  referencePreviewTimers.forEach((timer) => window.clearTimeout(timer));
  referencePreviewTimers.clear();
  referenceVideoUrls.value.forEach((url) => URL.revokeObjectURL(url));
  referenceImageUrls.value.forEach((url) => URL.revokeObjectURL(url));
});
</script>

<template>
  <section class="request-workspace">
    <header class="request-hero">
      <div>
        <span>VIDEO REQUEST WORKFLOW</span>
        <h1>视频中心提需</h1>
        <p>
          参考内容、制作分配、成片验收与投放数据集中流转，每一步都有负责人和记录。
        </p>
      </div>
      <div class="request-hero-stats">
        <strong>{{ data.total }}</strong
        ><span>当前口径记录</span><small>{{ activeCount }} 条正在推进</small>
      </div>
    </header>

    <div class="request-flow" aria-label="视频提需流程">
      <div>
        <b>01</b
        ><span
          ><strong>提交需求</strong><small>链接 / 参考视频或图片 / 描述</small></span
        >
      </div>
      <ArrowRight />
      <div>
        <b>02</b
        ><span><strong>主管分配</strong><small>仅授权主管可操作</small></span>
      </div>
      <ArrowRight />
      <div>
        <b>03</b
        ><span><strong>制作交付</strong><small>成片自动进入视频素材库</small></span>
      </div>
      <ArrowRight />
      <div>
        <b>04</b
        ><span
          ><strong>验收回流</strong><small>返修留版本，数据可追踪</small></span
        >
      </div>
    </div>

    <section class="request-create" :class="{ compact: !createOpen }">
      <button
        class="request-section-toggle"
        type="button"
        @click="createOpen = !createOpen"
      >
        <span
          ><Plus :size="19" /><strong>发起新提需</strong
          ><small>提交后视频中心主管会收到分配提醒</small></span
        ><b>{{ createOpen ? "收起" : "展开填写" }}</b>
      </button>
      <div v-if="createOpen" class="request-form">
        <label
          ><span>产品</span
          ><select v-model="createProduct">
            <option v-for="item in productOptions" :key="item">
              {{ item }}
            </option>
          </select></label
        >
        <label class="wide"
          ><span>竞对链接或分享文本（可选）</span>
          <div class="input-icon">
            <Link2 :size="17" /><input
              v-model="createUrl"
              placeholder="可直接粘贴抖音/小红书分享文案、短链或任意参考信息"
            /></div
        ></label>
        <label class="wide"
          ><span>语言描述</span
          ><textarea
            v-model="createDescription"
            rows="4"
            placeholder="说明目标、受众、核心卖点、画面方向、时长等；写清楚能减少来回沟通。"
          ></textarea>
        </label>
        <label class="reference-picker"
          ><span>参考视频（可选，最多 9 条）</span
          ><input
            type="file"
            accept="video/*"
            multiple
            @change="chooseReferenceVideos(($event.target as HTMLInputElement).files)"
          />
          <div>
            <FileVideo :size="20" /><span
              ><strong>{{ referenceVideos.length ? `已选择 ${referenceVideos.length} 条参考视频` : "选择参考视频" }}</strong
              ><small>原文件直传，单条不超过 500MB</small></span
            >
          </div></label
        >
        <label class="reference-picker reference-image-picker"
          ><span>参考图片（可选，最多 9 张）</span
          ><input type="file" accept="image/*" multiple @change="chooseReferenceImages(($event.target as HTMLInputElement).files)" />
          <div><FileImage :size="20" /><span><strong>{{ referenceImages.length ? `已选择 ${referenceImages.length} 张参考图` : "选择参考图片" }}</strong><small>支持 JPG、PNG、WEBP，单张不超过 20MB</small></span></div></label
        >
        <div v-if="referenceVideos.length" class="reference-video-preview-list">
          <figure v-for="(file, index) in referenceVideos" :key="`${file.name}-${file.lastModified}`"><video :src="referenceVideoUrls[index]" controls playsinline preload="metadata" /><figcaption><strong>{{ file.name }}</strong><small>{{ (file.size / 1024 ** 2).toFixed(1) }} MB</small></figcaption><button type="button" aria-label="移除参考视频" @click="removeReferenceVideo(index)"><X :size="13" /></button></figure>
        </div>
        <div v-if="referenceImages.length" class="reference-image-preview-list">
          <figure v-for="(file, index) in referenceImages" :key="`${file.name}-${file.lastModified}`"><img :src="referenceImageUrls[index]" :alt="file.name" /><figcaption>{{ file.name }}</figcaption><button type="button" aria-label="移除参考图片" @click="removeReferenceImage(index)"><X :size="13" /></button></figure>
        </div>
        <div v-if="creating" class="request-progress">
          <span><i :style="{ width: `${createProgress}%` }"></i></span
          ><b>{{ createProgress }}%</b><small>{{ createStage }}</small>
        </div>
        <button
          class="request-submit"
          :disabled="creating"
          @click="createRequest"
        >
          <Send :size="18" />{{ creating ? "正在提交…" : "提交给视频中心" }}
        </button>
      </div>
    </section>

    <div class="request-toolbar">
      <div class="scope-tabs">
        <button :class="{ active: scope === 'mine' }" @click="scope = 'mine'">
          我的提需</button
        ><button
          :class="{ active: scope === 'assigned' }"
          @click="scope = 'assigned'"
        >
          分配给我</button
        ><button
          v-if="canSeeAll"
          :class="{ active: scope === 'all' }"
          @click="scope = 'all'"
        >
          主管任务台
        </button>
      </div>
      <div class="view-tabs" aria-label="任务展示方式">
        <button :class="{ active: listMode === 'table' }" @click="listMode = 'table'">
          <List :size="16" />任务表
        </button>
        <button :class="{ active: listMode === 'cards' }" @click="listMode = 'cards'">
          <PanelsTopLeft :size="16" />处理卡片
        </button>
      </div>
      <label class="request-search"
        ><Search :size="17" /><input
          v-model="query"
          placeholder="搜索产品、需求或同事"
      /></label>
      <label class="request-progress-filter">
        <span><ListFilter :size="15" />任务进度</span>
        <select v-model="status" aria-label="按任务进度筛选">
          <option
            v-for="item in statusOptions"
            :key="item.value"
            :value="item.value"
          >
            {{ item.label }}
          </option>
        </select>
      </label>
      <select v-model="pageSize" aria-label="每页显示条数">
        <option :value="5">每页 5 条</option>
        <option :value="10">每页 10 条</option>
        <option :value="20">每页 20 条</option>
        <option :value="30">每页 30 条</option>
      </select>
      <button class="request-refresh" @click="load">
        <RefreshCw :size="16" />刷新
      </button>
    </div>

    <div v-if="error" class="request-error">
      <strong>暂时无法完成操作</strong><span>{{ error }}</span
      ><button @click="error = ''">知道了</button>
    </div>
    <div v-if="loading" class="request-loading">
      <i></i><span>正在读取协作进度…</span>
    </div>
    <div v-else-if="!data.items.length" class="request-empty">
      <Video :size="34" /><strong>当前没有匹配的提需</strong
      ><span>可以切换“我的提需 / 分配给我”，或发起一条新需求。</span>
    </div>
    <div v-else-if="listMode === 'table'" class="request-table-wrap">
      <table class="request-table">
        <thead>
          <tr>
            <th>提需 / 提需人</th>
            <th>产品与参考</th>
            <th>制作人</th>
            <th>任务进度</th>
            <th>更新时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in data.items" :key="item.id">
            <td>
              <strong>{{ item.description || "以参考内容为准" }}</strong>
              <small>{{ displayPerson(item.requester_name, item.requester_number) }} · {{ item.requester_department || "未记录部门" }}</small>
            </td>
            <td>
              <strong>{{ item.product }}</strong>
              <small>{{ item.reference_videos?.length ? `${item.reference_videos.length} 条参考视频` : (item.reference_images?.length ? `${item.reference_images.length} 张参考图` : (item.reference_url ? "已提供参考信息" : "仅语言描述")) }}</small>
            </td>
            <td>
              <strong>{{ item.assignee_name || "待主管分配" }}</strong>
              <small>第 {{ item.delivery_version || 0 }} 版成片</small>
            </td>
            <td class="task-progress-cell">
              <div><span><i :style="{ width: `${item.progress_percent}%` }"></i></span><b>{{ item.progress_percent }}%</b></div>
              <strong>{{ item.status_label }}</strong>
              <small>{{ item.progress_label }}</small>
            </td>
            <td><strong>{{ formatDate(item.updated_at) }}</strong><small>{{ item.latest_feedback || "暂无修改意见" }}</small></td>
            <td>
              <div class="request-table-actions">
                <button
                  v-if="item.status === 'delivered' && item.permissions.can_review && item.latest_assets?.length"
                  class="request-review-entry"
                  @click="openReview(item)"
                ><Check :size="15" />去验收</button>
                <button class="request-detail-toggle" @click="openTask(item)">查看与处理</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else class="request-list">
      <article
        v-for="item in data.items"
        :key="item.id"
        :data-request-id="item.id"
        class="request-card"
        :class="`status-${item.status}`"
      >
        <header>
          <div class="request-product">
            <span>{{ item.product }}</span
            ><strong>{{ item.description || "以参考内容为准" }}</strong
            ><small
              >{{ displayPerson(item.requester_name, item.requester_number) }} ·
              {{ formatDate(item.created_at) }}</small
            >
          </div>
          <div class="request-owner">
            <small>制作人</small
            ><strong>{{ item.assignee_name || "待主管分配" }}</strong>
          </div>
          <span class="request-status">{{ item.status_label }}</span>
          <div class="request-card-actions">
            <button
              v-if="item.status === 'delivered' && item.permissions.can_review && item.latest_assets?.length"
              class="request-review-entry"
              @click="openReview(item)"
            ><Check :size="15" />去验收</button>
            <button
              class="request-detail-toggle"
              @click="toggleTask(item)"
            >
              {{ expanded[item.id] ? "收起" : "查看与处理" }}
            </button>
          </div>
        </header>

        <div class="request-card-progress">
          <span><i :style="{ width: `${item.progress_percent}%` }"></i></span>
          <b>{{ item.progress_percent }}%</b>
          <small>{{ item.progress_label }}</small>
        </div>

        <div class="request-brief">
          <a
            v-if="item.reference_url && referenceHref(item.reference_url)"
            :href="referenceHref(item.reference_url)"
            target="_blank"
            rel="noreferrer"
            ><ExternalLink :size="15" />竞对链接</a
          >
          <span v-else-if="item.reference_url" class="reference-text"><Link2 :size="15" />{{ item.reference_url }}</span>
          <span v-if="item.reference_videos?.length" class="reference-video-count"><CirclePlay :size="15" />{{ item.reference_videos.length }} 条参考视频</span>
          <span v-if="item.latest_feedback" class="feedback-flag"
            ><MessageSquareText :size="15" />{{ item.latest_feedback }}</span
          >
          <span v-if="item.reference_images?.length" class="reference-image-count"><FileImage :size="15" />{{ item.reference_images.length }} 张参考图</span>
          <span v-if="!item.reference_url && !item.reference_videos?.length && !item.reference_images?.length"
            >仅语言描述</span
          >
        </div>

        <div v-if="expanded[item.id]" class="request-detail">
          <div class="request-detail-main">
            <section>
              <small>具体需求</small>
              <p>
                {{ item.description || "未填写文字说明，请查看参考内容。" }}
              </p>
              <div v-if="item.reference_videos?.length" class="request-reference-video-gallery">
                <article v-for="video in item.reference_videos" :key="video.object_key">
                  <video
                    v-if="video.preview_status === 'ready'"
                    :src="video.preview_url || video.url"
                    controls
                    playsinline
                    preload="metadata"
                  />
                  <div v-else class="request-reference-preview-state" :class="{ failed: video.preview_status === 'failed' }">
                    <RefreshCw v-if="video.preview_status !== 'failed'" :size="23" />
                    <CirclePlay v-else :size="25" />
                    <strong>{{ video.preview_status === "failed" ? "预览生成失败" : "正在生成浏览器兼容预览" }}</strong>
                    <small>{{ video.preview_status === "failed" ? (video.preview_error || "可下载原文件查看") : "原视频保持不变，只需处理一次" }}</small>
                    <button v-if="video.preview_status === 'failed'" type="button" @click="ensureReferencePreviews(item)">重试预览</button>
                  </div>
                  <div class="request-reference-video-meta">
                    <strong>{{ video.filename }}</strong>
                    <a :href="video.original_url || video.url" target="_blank" rel="noreferrer"><ExternalLink :size="13" />原文件</a>
                  </div>
                </article>
              </div>
              <div v-if="item.reference_images?.length" class="request-reference-gallery"><a v-for="image in item.reference_images" :key="image.object_key" :href="image.url" target="_blank" rel="noreferrer"><img :src="image.url" :alt="image.filename" /><span>{{ image.filename }}</span></a></div>
            </section>

            <section
              v-if="item.permissions.can_assign && !['accepted', 'returned'].includes(item.status)"
              class="assignment-box"
            >
              <small>主管分配（仅授权主管）</small>
              <div>
                <input
                  v-model="assigneeQuery"
                  placeholder="搜索制作人"
                  @focus="loadAssignees"
                /><select v-model="assigneeSelected[item.id]">
                  <option value="">选择制作人</option>
                  <option
                    v-for="candidate in assignees"
                    :key="`${candidate.number}|${candidate.name}`"
                    :value="`${candidate.number}|${candidate.name}`"
                  >
                    {{ candidate.name
                    }}{{
                      candidate.department ? ` · ${candidate.department}` : ""
                    }}
                  </option></select
                ><button :disabled="busyId === item.id" @click="assign(item)">
                  <UserRoundCheck :size="16" />确认分配
                </button>
              </div>
              <div class="return-request-row">
                <input v-model="returnReasons[item.id]" placeholder="无法制作或需求重复时，填写退回理由" />
                <button class="return-button" :disabled="busyId === item.id" @click="returnRequest(item)"><Undo2 :size="16" />退回需求</button>
              </div>
            </section>

            <section v-if="item.status === 'returned'" class="returned-box">
              <small>退回原因</small><strong>{{ item.return_reason }}</strong><span>{{ item.returned_by_name }} · {{ formatDate(item.returned_at) }}</span>
            </section>

            <section
              v-if="
                item.permissions.can_work &&
                ['assigned', 'revision_requested'].includes(item.status)
              "
              class="work-action"
            >
              <button :disabled="busyId === item.id" @click="start(item)">
                <CirclePlay :size="17" />{{
                  item.status === "revision_requested" ? "开始修改" : "开始制作"
                }}</button
              ><span>开始后提需人可以实时看到状态变化。</span>
            </section>

            <section
              v-if="
                item.permissions.can_work &&
                ['assigned', 'in_production', 'revision_requested'].includes(
                  item.status,
                )
              "
              class="delivery-box"
            >
              <small>上传本次成片（最多 10 条，可逐条选择品类与素材类型）</small>
              <div class="delivery-fields">
                <label
                  ><UploadCloud :size="18" /><input
                    type="file"
                    accept="video/*"
                    multiple
                    @change="
                      chooseDelivery(
                        item.id,
                        ($event.target as HTMLInputElement).files,
                        item.product,
                      )
                    "
                  /><span>{{
                    (deliveryDrafts[item.id]?.length ? `已选择 ${deliveryDrafts[item.id].length} 条` : "选择多条视频原文件")
                  }}</span></label
                ><input
                  v-model="deliveryNotes[item.id]"
                  placeholder="版本说明（可选）"
                /><button
                  :disabled="busyId === item.id || !deliveryDrafts[item.id]?.length"
                  @click="deliver(item)"
                >
                  上传 {{ deliveryDrafts[item.id]?.length || 0 }} 条并提醒验收
                </button>
              </div>
              <div v-if="deliveryDrafts[item.id]?.length" class="delivery-draft-list">
                <article v-for="draft in deliveryDrafts[item.id]" :key="draft.id" class="delivery-draft-card">
                  <header><FileVideo :size="17" /><strong>{{ draft.file.name }}</strong><span>{{ (draft.file.size / 1024 / 1024).toFixed(1) }} MB</span><button type="button" :disabled="busyId === item.id" @click="removeDelivery(item.id, draft.id)"><Trash2 :size="15" /></button></header>
                  <div class="delivery-classification">
                    <label><small>产品品类</small><select v-model="draft.category"><option v-for="category in productOptions" :key="category" :value="category">{{ category }}</option></select></label>
                    <label><small>素材类型</small><select v-model="draft.assetSubtype"><option v-for="subtype in sourceSubtypeOptions" :key="subtype" :value="subtype">{{ subtype }}</option></select></label>
                    <label><small>内容类型</small><select v-model="draft.contentType"><option v-for="type in contentTypeOptions" :key="type" :value="type">{{ type }}</option></select></label>
                    <label><small>文件夹（可选）</small><input v-model="draft.folderName" placeholder="例如：七夕裂变" /></label>
                    <label class="draft-tags"><small>标签（可选，逗号分隔）</small><input v-model="draft.tags" placeholder="卖点、达人、批次" /></label>
                  </div>
                  <div v-if="busyId === item.id || draft.progress" class="draft-progress"><span><i :style="{ width: `${draft.progress}%` }"></i></span><b>{{ draft.progress }}%</b><small>{{ draft.stage }}</small></div>
                </article>
              </div>
            </section>

            <section
              v-if="item.status === 'delivered' && item.latest_assets?.length"
              class="review-box"
              :data-review-request-id="item.id"
            >
              <small>本批 {{ item.latest_assets.length }} 条成片待验收</small>
              <div class="latest-delivery-grid">
                <article v-for="asset in item.latest_assets" :key="asset.id">
                  <video :src="asset.preview_url" controls playsinline preload="metadata" :poster="asset.cover_url || undefined"></video>
                  <strong>{{ asset.filename }}</strong><small>{{ asset.category }} · {{ asset.asset_subtype }} · {{ asset.content_type }}</small>
                  <a :href="asset.preview_url" target="_blank"><ExternalLink :size="13" />新窗口打开</a>
                </article>
              </div>
              <div v-if="item.permissions.can_review" class="review-actions">
                <button
                  class="accept"
                  :disabled="busyId === item.id"
                  @click="accept(item)"
                >
                  <Check :size="17" />确认满意</button
                ><input
                  v-model="feedback[item.id]"
                  placeholder="不满意请写清要改什么"
                /><button
                  class="revision"
                  :disabled="busyId === item.id"
                  @click="requestRevision(item)"
                >
                  反馈并重新制作
                </button>
              </div>
            </section>

            <section class="metrics-box">
              <div>
                <small>数据回流</small
                ><strong>{{
                  item.metrics.state === "available"
                    ? "已关联真实数据"
                    : "等待平台回流"
                }}</strong
                ><span>{{ item.metrics.message }}</span>
              </div>
              <div class="metric-grid">
                <span
                  ><small>千川成交额</small
                  ><strong>{{
                    formatMoney(item.metrics.qianchuan?.gmv_yuan)
                  }}</strong
                  ><em>{{
                    item.metrics.qianchuan
                      ? `${item.metrics.qianchuan.target_count} 个投放目标`
                      : "尚未关联"
                  }}</em></span
                >
                <span
                  ><small>ADQ</small
                  ><strong>{{
                    item.metrics.adq
                      ? `${item.metrics.adq.data_task_count}/${item.metrics.adq.task_count}`
                      : "待回流"
                  }}</strong
                  ><em>已回流任务 / 关联任务</em></span
                >
                <span
                  ><small>视频号播放</small
                  ><strong>{{
                    item.metrics.channels?.view_count ?? "待回流"
                  }}</strong
                  ><em>{{
                    item.metrics.channels
                      ? `${item.metrics.channels.data_task_count} 条已回流`
                      : "尚未关联"
                  }}</em></span
                >
              </div>
            </section>
          </div>

          <aside class="request-history">
            <small>版本与动态</small>
            <div v-for="event in item.events" :key="event.id">
              <i></i
              ><span
                ><strong>{{ eventLabels[event.action] || event.action }}</strong
                ><small
                  >{{ displayPerson(event.actor_name) }} ·
                  {{ formatDate(event.created_at) }}</small
                ></span
              >
            </div>
          </aside>
        </div>
      </article>
    </div>

    <footer v-if="data.total" class="request-footer">
      <span>共 {{ data.total }} 条</span
      ><PaginationControls
        :page="data.page"
        :total-pages="data.total_pages"
        @change="
          page = $event;
          load();
        "
      />
    </footer>
    <Transition name="request-toast"
      ><div v-if="toast" class="request-toast">{{ toast }}</div></Transition
    >
  </section>
</template>

<style scoped>
.request-workspace {
  display: grid;
  gap: 20px;
  color: #153b59;
}
.request-hero {
  display: flex;
  justify-content: space-between;
  align-items: end;
  padding: 30px 34px;
  border: 1px solid #c8e1ef;
  border-radius: 28px;
  background: linear-gradient(125deg, #fffdf7 0%, #f4fbff 62%, #e4f4fb 100%);
  box-shadow: 0 18px 48px rgba(32, 91, 123, 0.08);
}
.request-hero > div:first-child > span {
  font-size: 12px;
  font-weight: 900;
  letter-spacing: 0.22em;
  color: #1688c7;
}
.request-hero h1 {
  margin: 8px 0 4px;
  font-size: 38px;
  line-height: 1.1;
}
.request-hero p {
  margin: 0;
  color: #658298;
  font-size: 15px;
}
.request-hero-stats {
  min-width: 180px;
  padding: 18px 22px;
  border-radius: 20px;
  background: #0f78b6;
  color: white;
}
.request-hero-stats strong {
  display: block;
  font-size: 30px;
}
.request-hero-stats span,
.request-hero-stats small {
  display: block;
}
.request-hero-stats small {
  margin-top: 5px;
  opacity: 0.75;
}
.request-flow {
  display: grid;
  grid-template-columns: 1fr auto 1fr auto 1fr auto 1fr;
  align-items: center;
  padding: 18px 22px;
  border-radius: 22px;
  background: #fff;
  border: 1px solid #d7e7ef;
}
.request-flow > div {
  display: flex;
  gap: 12px;
  align-items: center;
}
.request-flow b {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 14px;
  background: #dff3fc;
  color: #087bbb;
}
.request-flow span {
  display: grid;
}
.request-flow strong {
  font-size: 15px;
}
.request-flow small {
  color: #7891a2;
}
.request-flow > svg {
  color: #9fb7c6;
}
.request-create {
  border: 1px solid #cfe3ee;
  border-radius: 24px;
  background: #fff;
  overflow: hidden;
}
.request-section-toggle {
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 20px 24px;
  border: 0;
  background: #eef8fc;
  color: #153b59;
}
.request-section-toggle > span {
  display: flex;
  align-items: center;
  gap: 10px;
}
.request-section-toggle small {
  color: #718b9c;
  font-weight: 500;
}
.request-section-toggle > b {
  color: #1688c7;
}
.request-form {
  display: grid;
  grid-template-columns: 220px 1fr 1fr;
  gap: 16px;
  padding: 24px;
}
.request-form label {
  display: grid;
  gap: 8px;
}
.request-form label > span,
.request-detail section > small,
.request-history > small,
.delivery-box > small {
  font-size: 13px;
  font-weight: 800;
  color: #4d7189;
}
.request-form .wide {
  grid-column: span 2;
}
.request-form input,
.request-form select,
.request-form textarea,
.assignment-box input,
.assignment-box select,
.delivery-fields input,
.review-actions input,
.request-toolbar input,
.request-toolbar select {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #c8dce8;
  border-radius: 13px;
  background: #fff;
  padding: 12px 14px;
  color: #153b59;
  font: inherit;
  outline: none;
}
.request-form input:focus,
.request-form select:focus,
.request-form textarea:focus,
.assignment-box input:focus,
.assignment-box select:focus,
.delivery-fields input:focus,
.review-actions input:focus {
  border-color: #1688c7;
  box-shadow: 0 0 0 3px rgba(22, 136, 199, 0.1);
}
.input-icon {
  position: relative;
}
.input-icon svg {
  position: absolute;
  left: 13px;
  top: 13px;
  color: #7895a7;
}
.input-icon input {
  padding-left: 39px;
}
.reference-picker {
  grid-column: span 2;
  position: relative;
}
.reference-picker > input {
  position: absolute;
  inset: 26px 0 0;
  opacity: 0;
  cursor: pointer;
}
.reference-picker > div {
  display: flex;
  gap: 11px;
  align-items: center;
  padding: 13px 15px;
  border: 1px dashed #92c7e1;
  border-radius: 14px;
  background: #f5fbfe;
}
.reference-picker > div > span {
  display: grid;
}
.reference-picker small {
  color: #7991a2;
}
.request-submit,
.assignment-box button,
.delivery-fields button,
.work-action button {
  align-self: end;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 0;
  border-radius: 13px;
  background: #1688c7;
  color: #fff;
  font-weight: 800;
  padding: 13px 17px;
  cursor: pointer;
}
.request-submit:disabled,
.assignment-box button:disabled,
.delivery-fields button:disabled,
.work-action button:disabled {
  opacity: 0.5;
  cursor: wait;
}
.request-progress {
  grid-column: 1/-2;
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 5px 10px;
  align-items: center;
}
.request-progress > span {
  height: 8px;
  border-radius: 999px;
  background: #e3eef4;
  overflow: hidden;
}
.request-progress i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, #1688c7, #57bce8);
}
.request-progress small {
  grid-column: 1/-1;
  color: #6d8798;
}
.request-toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 12px;
  border: 1px solid #d4e5ee;
  border-radius: 18px;
  background: #fff;
}
.scope-tabs {
  display: flex;
  gap: 4px;
  padding: 4px;
  border-radius: 13px;
  background: #edf5f9;
}
.scope-tabs button {
  border: 0;
  background: transparent;
  padding: 9px 14px;
  border-radius: 10px;
  color: #57758a;
  font-weight: 700;
}
.scope-tabs button.active {
  background: #1688c7;
  color: #fff;
}
.view-tabs {
  display: flex;
  gap: 4px;
  padding: 4px;
  border: 1px solid #d8e7ee;
  border-radius: 13px;
  background: #fff;
}
.view-tabs button {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  border-radius: 9px;
  padding: 8px 11px;
  background: transparent;
  color: #658194;
  font-weight: 800;
  white-space: nowrap;
}
.view-tabs button.active {
  background: #e5f4fb;
  color: #087dbc;
}
.request-search {
  margin-left: auto;
  position: relative;
  min-width: 280px;
}
.request-search svg {
  position: absolute;
  left: 13px;
  top: 12px;
  color: #7993a4;
}
.request-search input {
  padding-left: 38px;
}
.request-toolbar select {
  width: auto;
}
.request-progress-filter {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 43px;
  padding-left: 11px;
  border: 1px solid #c8dce8;
  border-radius: 12px;
  background: #fff;
}
.request-progress-filter > span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: #4f7187;
  font-size: 12px;
  font-weight: 800;
  white-space: nowrap;
}
.request-progress-filter > span svg { color: #1688c7; }
.request-progress-filter select {
  height: 41px;
  padding: 0 30px 0 8px;
  border: 0;
  border-radius: 0 11px 11px 0;
  color: #183f5a;
  background: #f6fafc;
  font-weight: 800;
}
.request-progress-filter:focus-within {
  border-color: #5eadd5;
  box-shadow: 0 0 0 3px rgba(22,136,199,.1);
}
.request-refresh {
  display: flex;
  gap: 6px;
  align-items: center;
  padding: 11px 14px;
  border: 1px solid #c8dce8;
  border-radius: 12px;
  background: #fff;
  color: #24536f;
  font-weight: 700;
}
.request-error {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 14px 18px;
  border: 1px solid #fac8c3;
  border-radius: 15px;
  background: #fff4f2;
  color: #9f342b;
}
.request-error button {
  margin-left: auto;
  border: 0;
  background: transparent;
  color: inherit;
  font-weight: 800;
}
.request-loading,
.request-empty {
  display: grid;
  place-items: center;
  gap: 8px;
  min-height: 190px;
  border: 1px dashed #bfd7e4;
  border-radius: 22px;
  background: #fbfdfd;
  color: #6c899a;
}
.request-loading i {
  width: 28px;
  height: 28px;
  border: 3px solid #d7e9f2;
  border-top-color: #1688c7;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
.request-list {
  display: grid;
  gap: 14px;
}
.request-table-wrap {
  overflow-x: auto;
  border: 1px solid #cfdee7;
  border-radius: 20px;
  background: #fff;
}
.request-table {
  width: 100%;
  min-width: 1050px;
  border-collapse: collapse;
  table-layout: fixed;
}
.request-table th {
  padding: 14px 16px;
  background: #eef7fb;
  color: #4d7087;
  font-size: 13px;
  text-align: left;
}
.request-table th:nth-child(1) { width: 22%; }
.request-table th:nth-child(2) { width: 15%; }
.request-table th:nth-child(3) { width: 13%; }
.request-table th:nth-child(4) { width: 25%; }
.request-table th:nth-child(5) { width: 14%; }
.request-table th:nth-child(6) { width: 11%; }
.request-table td {
  padding: 16px;
  border-top: 1px solid #e0ebf0;
  vertical-align: middle;
}
.request-table tbody tr:hover { background: #fbfdfe; }
.request-table td > strong,
.request-table td > small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
}
.request-table td > strong {
  color: #183f5a;
  font-size: 14px;
  white-space: nowrap;
}
.request-table td > small {
  margin-top: 5px;
  color: #7891a2;
  font-size: 12px;
  line-height: 1.45;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.task-progress-cell > div {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 9px;
  align-items: center;
  margin-bottom: 6px;
}
.task-progress-cell > div > span,
.request-card-progress > span {
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: #e2edf3;
}
.task-progress-cell i,
.request-card-progress i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #1289c7, #50b7df);
}
.task-progress-cell b {
  color: #087dbc;
  font-size: 13px;
}
.task-progress-cell > strong { color: #137daf !important; }
.request-card {
  position: relative;
  border: 1px solid #cfdee7;
  border-radius: 20px;
  background: #fff;
  overflow: hidden;
}
.request-card-progress {
  display: grid;
  grid-template-columns: minmax(120px, 1fr) auto minmax(220px, auto);
  gap: 10px;
  align-items: center;
  margin: 0 20px 12px 24px;
  color: #628095;
}
.request-card-progress b { color: #0c7db8; font-size: 13px; }
.request-card-progress small { text-align: right; }
.request-card > header {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 190px 110px auto;
  gap: 20px;
  align-items: center;
  padding: 18px 20px 14px 24px;
}
.request-product {
  display: grid;
  min-width: 0;
}
.request-product > span {
  font-size: 12px;
  font-weight: 900;
  color: #1385c4;
}
.request-product > strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 17px;
}
.request-product > small,
.request-owner small {
  color: #7890a0;
}
.request-owner {
  display: grid;
}
.request-status {
  justify-self: start;
  padding: 7px 11px;
  border-radius: 999px;
  background: #e7f5fc;
  color: #0e79b4;
  font-size: 13px;
  font-weight: 900;
}
.status-submitted .request-status {
  background: #fff1d8;
  color: #a66311;
}
.status-revision_requested .request-status {
  background: #ffebe8;
  color: #b23d34;
}
.status-accepted .request-status {
  background: #e8f7ef;
  color: #23825b;
}
.request-detail-toggle {
  border: 1px solid #bed9e7;
  border-radius: 11px;
  background: #fff;
  color: #1d658e;
  padding: 9px 12px;
  font-weight: 700;
}
.request-table-actions,
.request-card-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.request-card-actions {
  justify-content: flex-end;
}
.request-review-entry {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  border: 1px solid #0b8f66;
  border-radius: 11px;
  background: #0b9c70;
  color: #fff;
  padding: 9px 12px;
  font-weight: 800;
  white-space: nowrap;
  box-shadow: 0 6px 14px rgba(11, 156, 112, 0.16);
}
.request-review-entry:hover {
  background: #087e5b;
}
.request-brief {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  padding: 0 20px 16px 24px;
  color: #6a8495;
}
.request-brief a,
.feedback-flag,
.request-brief .reference-text {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 7px 10px;
  border-radius: 10px;
  background: #f0f7fa;
  color: #21698f;
  text-decoration: none;
}
.request-brief .reference-text {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.feedback-flag {
  background: #fff0ee;
  color: #a9433a;
}
.request-detail {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 245px;
  gap: 22px;
  padding: 22px 24px;
  border-top: 1px solid #dceaf1;
  background: #f8fbfc;
}
.request-detail-main {
  display: grid;
  gap: 14px;
}
.request-detail section {
  padding: 16px;
  border: 1px solid #d6e6ee;
  border-radius: 15px;
  background: #fff;
}
.request-detail section p {
  margin: 7px 0 0;
  line-height: 1.7;
  white-space: pre-wrap;
}
.assignment-box > div {
  display: grid;
  grid-template-columns: 180px 1fr auto;
  gap: 9px;
  margin-top: 10px;
}
.assignment-box button {
  align-self: stretch;
}
.return-request-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 9px;
  margin-top: 9px;
}
.assignment-box > .return-request-row { grid-template-columns: 1fr auto; }
.assignment-box .return-button {
  background: #fff3f2;
  color: #bd3f46;
  border: 1px solid #efc2c4;
}
.returned-box {
  display: grid;
  gap: 7px;
  padding: 14px;
  border: 1px solid #efc2c4;
  border-radius: 14px;
  background: #fff7f5;
}
.returned-box strong { color: #9e3039; }
.returned-box span { color: #7d8f9a; font-size: 13px; }
.work-action {
  display: flex;
  align-items: center;
  gap: 14px;
}
.work-action button {
  align-self: center;
}
.work-action span {
  color: #6c8798;
}
.delivery-fields {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  gap: 10px;
  margin-top: 10px;
}
.delivery-fields > label {
  position: relative;
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 12px 14px;
  border: 1px dashed #8fc2dc;
  border-radius: 13px;
  color: #226991;
  overflow: hidden;
}
.delivery-fields > label input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}
.delivery-fields > label span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.delivery-draft-list { display: grid; gap: 10px; margin-top: 12px; }
.delivery-draft-card { padding: 13px; border: 1px solid #d4e5ee; border-radius: 14px; background: #f8fcfe; }
.delivery-draft-card > header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; gap: 8px; align-items: center; color: #174765; }
.delivery-draft-card > header strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.delivery-draft-card > header span { color: #7891a1; font-size: 12px; }
.delivery-draft-card > header button { border: 0; background: transparent; color: #bd5961; cursor: pointer; }
.delivery-classification { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px; margin-top: 11px; }
.delivery-classification label { display: grid; gap: 5px; }
.delivery-classification small { color: #688596; font-weight: 700; }
.delivery-classification select,
.delivery-classification input { min-width: 0; border: 1px solid #c8dce8; border-radius: 10px; background: #fff; padding: 9px 10px; color: #153b59; }
.delivery-classification .draft-tags { grid-column: span 2; }
.draft-progress { display: grid; grid-template-columns: 1fr auto; gap: 5px 9px; margin-top: 10px; }
.draft-progress > span { height: 7px; border-radius: 99px; overflow: hidden; background: #e2eef4; }
.draft-progress i { display: block; height: 100%; background: linear-gradient(90deg, #1688c7, #57bce8); }
.draft-progress small { grid-column: 1/-1; color: #6d8798; }
.latest-delivery-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
.latest-delivery-grid article { display: grid; gap: 6px; min-width: 0; }
.latest-delivery-grid video { width: 100%; aspect-ratio: 9/16; object-fit: cover; border-radius: 12px; background: #102f42; }
.latest-delivery-grid strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.latest-delivery-grid small { color: #718b9c; line-height: 1.4; }
.latest-delivery-grid a { display: flex; align-items: center; gap: 4px; color: #167eb7; text-decoration: none; font-size: 12px; }
.latest-delivery {
  display: flex;
  justify-content: space-between;
  gap: 14px;
  margin-top: 9px;
}
.latest-delivery a {
  display: flex;
  gap: 6px;
  align-items: center;
  color: #167eb7;
  text-decoration: none;
  font-weight: 800;
}
.latest-delivery span {
  color: #7891a1;
}
.review-actions {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 9px;
  margin-top: 14px;
}
.review-actions button {
  border: 0;
  border-radius: 12px;
  padding: 11px 15px;
  font-weight: 800;
}
.review-actions .accept {
  display: flex;
  gap: 6px;
  background: #e4f6ed;
  color: #237a58;
}
.review-actions .revision {
  background: #fff0ee;
  color: #a73f36;
}
.metrics-box > div:first-child {
  display: grid;
  gap: 3px;
}
.metrics-box > div:first-child > span {
  color: #7891a2;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
  margin-top: 13px;
}
.metric-grid > span {
  display: grid;
  padding: 12px;
  border-radius: 12px;
  background: #eef7fb;
}
.metric-grid small,
.metric-grid em {
  font-style: normal;
  color: #718d9f;
  font-size: 12px;
}
.metric-grid strong {
  font-size: 19px;
}
.request-history {
  border-left: 1px solid #d3e2e9;
  padding-left: 20px;
}
.request-history > div {
  position: relative;
  display: flex;
  gap: 10px;
  padding: 12px 0;
}
.request-history i {
  width: 9px;
  height: 9px;
  margin-top: 5px;
  border: 2px solid #1688c7;
  border-radius: 50%;
  background: #fff;
}
.request-history > div:not(:last-child):after {
  content: "";
  position: absolute;
  left: 5px;
  top: 27px;
  bottom: -11px;
  width: 1px;
  background: #c7dbe5;
}
.request-history span {
  display: grid;
}
.request-history small {
  color: #7891a2;
}
.request-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 4px;
  color: #607e91;
}
.request-toast {
  position: fixed;
  right: 28px;
  bottom: 28px;
  z-index: 40;
  padding: 14px 18px;
  border-radius: 14px;
  background: #153b59;
  color: #fff;
  box-shadow: 0 14px 35px rgba(21, 59, 89, 0.28);
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}
@media (max-width: 1100px) {
  .request-flow {
    grid-template-columns: 1fr 1fr;
  }
  .request-flow > svg {
    display: none;
  }
  .request-form {
    grid-template-columns: 1fr 1fr;
  }
  .request-form .wide,
  .reference-picker {
    grid-column: span 1;
  }
  .request-submit {
    grid-column: 1/-1;
  }
  .request-card > header {
    grid-template-columns: 1fr auto;
  }
  .request-owner {
    display: none;
  }
  .request-detail {
    grid-template-columns: 1fr;
  }
  .request-history {
    border-left: 0;
    border-top: 1px solid #d3e2e9;
    padding: 16px 0 0;
  }
  .request-toolbar {
    flex-wrap: wrap;
  }
  .view-tabs { order: 2; }
  .request-search {
    margin-left: 0;
    flex: 1;
  }
}
@media (max-width: 720px) {
  .request-hero {
    align-items: start;
    padding: 24px;
    flex-direction: column;
    gap: 18px;
  }
  .request-hero h1 {
    font-size: 31px;
  }
  .request-hero-stats {
    width: 100%;
    box-sizing: border-box;
  }
  .request-flow {
    grid-template-columns: 1fr;
  }
  .request-form {
    grid-template-columns: 1fr;
  }
  .request-form .wide,
  .reference-picker {
    grid-column: auto;
  }
  .request-section-toggle small {
    display: none;
  }
  .scope-tabs {
    width: 100%;
  }
  .view-tabs { width: 100%; }
  .view-tabs button { flex: 1; justify-content: center; }
  .scope-tabs button {
    flex: 1;
  }
  .request-search {
    min-width: 100%;
  }
  .request-progress-filter {
    flex: 1;
  }
  .request-progress-filter select {
    flex: 1;
  }
  .request-card > header {
    grid-template-columns: 1fr auto;
    gap: 10px;
  }
  .request-card-progress {
    grid-template-columns: 1fr auto;
  }
  .request-card-progress small {
    grid-column: 1/-1;
    text-align: left;
  }
  .request-status {
    grid-column: 1;
  }
  .request-card-actions {
    grid-column: 2;
    grid-row: 1/3;
    align-self: center;
    flex-direction: column;
  }
  .assignment-box > div,
  .delivery-fields,
  .delivery-classification,
  .latest-delivery-grid,
  .review-actions,
  .metric-grid {
    grid-template-columns: 1fr;
  }
  .request-footer {
    align-items: start;
    flex-direction: column;
    gap: 10px;
  }
}
.request-card.status-submitted {
  border-color: #ead5ae;
}
.request-card.status-revision_requested {
  border-color: #edc5c1;
}
.request-card.status-accepted {
  border-color: #bfe2d1;
}
.reference-image-preview-list { grid-column: 1/-1; display:grid; grid-template-columns:repeat(auto-fill,minmax(104px,1fr)); gap:10px; }
.reference-video-preview-list { grid-column:1/-1; display:grid; grid-template-columns:repeat(auto-fill,minmax(132px,1fr)); gap:10px; }
.reference-video-preview-list figure { position:relative; min-width:0; margin:0; overflow:hidden; border:1px solid #d6e6ef; border-radius:13px; background:#f6fbfd; }
.reference-video-preview-list video { display:block; width:100%; aspect-ratio:9/16; max-height:220px; object-fit:cover; background:#102c3d; }
.reference-video-preview-list figcaption { display:grid; gap:2px; padding:7px 9px; }
.reference-video-preview-list figcaption strong { overflow:hidden; color:#274f68; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
.reference-video-preview-list figcaption small { color:#7891a2; font-size:10px; }
.reference-video-preview-list button { position:absolute; top:6px; right:6px; display:grid; place-items:center; width:25px; height:25px; border:0; border-radius:8px; color:#fff; background:rgba(16,48,68,.76); cursor:pointer; }
.reference-image-preview-list figure { position:relative; min-width:0; margin:0; overflow:hidden; border:1px solid #d6e6ef; border-radius:12px; background:#f6fbfd; }
.reference-image-preview-list img { display:block; width:100%; aspect-ratio:4/3; object-fit:cover; }
.reference-image-preview-list figcaption { overflow:hidden; padding:7px 9px; color:#567086; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
.reference-image-preview-list button { position:absolute; top:6px; right:6px; display:grid; place-items:center; width:24px; height:24px; border:0; border-radius:7px; color:#fff; background:rgba(16,48,68,.72); cursor:pointer; }
.reference-image-count { display:inline-flex; align-items:center; gap:5px; }
.reference-video-count { display:inline-flex; align-items:center; gap:5px; }
.request-reference-video-gallery { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:12px; margin-top:12px; }
.request-reference-video-gallery article { overflow:hidden; border:1px solid #d5e6ef; border-radius:12px; background:#f5fafc; }
.request-reference-video-gallery video { display:block; width:100%; aspect-ratio:9/16; max-height:310px; object-fit:cover; background:#102c3d; }
.request-reference-preview-state { display:grid; place-content:center; justify-items:center; gap:7px; width:100%; aspect-ratio:9/16; max-height:310px; padding:14px; color:#477087; text-align:center; background:linear-gradient(145deg,#eff8fc,#e4f1f7); }
.request-reference-preview-state > svg { animation:spin .9s linear infinite; color:#078dd0; }
.request-reference-preview-state.failed > svg { animation:none; color:#b76c65; }
.request-reference-preview-state strong { color:#244e66; font-size:12px; white-space:normal; }
.request-reference-preview-state small { max-width:150px; color:#718b9b; font-size:10px; line-height:1.5; }
.request-reference-preview-state button { padding:6px 10px; border:1px solid #bed8e6; border-radius:8px; color:#087fb8; font-size:11px; background:#fff; cursor:pointer; }
.request-reference-video-meta { display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 10px; }
.request-reference-video-gallery strong { overflow:hidden; color:#315a73; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
.request-reference-video-gallery a { display:inline-flex; flex:none; align-items:center; gap:3px; color:#0a82c2; font-size:11px; text-decoration:none; }
.request-reference-gallery { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:10px; margin-top:12px; }
.request-reference-gallery a { overflow:hidden; border:1px solid #dbe8ef; border-radius:10px; color:#315a73; text-decoration:none; background:#f7fbfd; }
.request-reference-gallery img { display:block; width:100%; aspect-ratio:4/3; object-fit:cover; }
.request-reference-gallery span { display:block; overflow:hidden; padding:7px 9px; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
</style>
