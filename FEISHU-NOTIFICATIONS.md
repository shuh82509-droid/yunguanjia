# 云管家飞书通知

本分支将 `notification-r52-20260923` 的云管家增量合入现有仓库，不覆盖生产基线中其他版本的登录或提需逻辑。

素材提交审核、修改审核人、组长通过转主管、审核驳回和终审通过时，业务事务写入 `user_notifications`；提审人与当前审核人相同时合并为一条。消息包含视频文件名、产品、审核版本和处理意见。

视频提需沿用已有站内事件，新增需求摘要；分配负责人可通过 `VIDEO_REQUEST_ASSIGNER_NAME` 和 `VIDEO_REQUEST_ASSIGNER_NUMBER` 配置，缺省姓名沿用原设置。

新增 `external_message_id` 保存飞书回执，现有启动建表/补列逻辑负责兼容旧数据库。通知查询接口一并返回该字段。

使用 `wis-centre` 独立通知后台时，设置 `SERVICE_NOTIFICATION_DELIVERY_MODE=external`，使本服务原有外发循环停止投递，避免重复发送。通知后台需能读写业务 SQLite 数据库以读取事件并回写回执，并配置真实飞书应用及收件人工号映射。

生产通知入口为 `https://hub.fandow.com/yxb/wis-marketing-hub/modules/cloud-manager/`，由通知后台的 `CLOUD_NOTIFICATION_PUBLIC_URL` 配置。

专项测试为 `backend/tests/test_service_notifications.py`，使用内存 SQLite 验证提审、转主管、驳回通知以及重复提审去重，不调用飞书。
