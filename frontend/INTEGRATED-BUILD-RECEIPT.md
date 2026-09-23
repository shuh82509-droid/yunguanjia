# 云管家集成前端候选

`editable-source-reference/` 的 44 份 Vue/TypeScript 源码以修订后的锁文件完成 `npm ci`，再设置 `INTEGRATED_BASE_PATH=/yxb/wis-marketing-hub/modules/cloud-manager/`，依次通过 `vue-tsc -b` 和 `vite build`。最终构建复制到 `frontend/integrated-dist-verified/`，`index.html` SHA256 为 `fae682d57fd4826cbbab23c205a958673d36d2c1e81ba8cb72fc0fe5f026cde6`。

后端仅在 `HUB_INTEGRATED_MODE=1` 且 `CLOUD_INTEGRATED_INDEX_SHA256` 与上述实际文件相同时选择该目录；其它模式仍用原 `frontend/dist`。同级 `integrated-dist/` 是此前未修复二次登录界面的探索构建，不是发布目标。

这证明候选前端可由源码复建，不证明它与现役所有功能完全等价。原数据、OA 真会话、上传、审核、渠道推送与回流仍须迁移后逐项验收。
