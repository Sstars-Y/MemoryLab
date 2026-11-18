# 核心接口与适配

`MemoryService(project, data_root, persona_id)` 只打开项目 `.local/` 下显式指定的数据。`Scope` 由宿主创建，包含固定人格、用户、会话和运行代号；模型和浏览器不能提供人格目录或扩大用户范围。

核心方法：`record_message`、`search_messages`、`read_messages`、`correct_memory`、`delete_preview`、`delete_memory`、`restore_memory`、`purge_memory`、`clear_recycle`、`read_attachment`、`recognize_attachment`、`export`、`snapshot`、`verify_snapshot`、`restore_snapshot`。用户消息先保存，生成失败不回滚消息。删除接受用户预览后选定的消息 ID 集合。

`ChatManager` 管理请求、幂等、内存草稿、停止、暂停边界和来源。`FakeProvider` 用于合成测试；`RemoteProvider` 使用明确配置的兼容 `/models`、`/chat/completions`、可选 `/audio/transcriptions` 接口。仅文本回答；不提供自由工具调用、SQL 或文件访问。请求密钥只存在内存，不自动读取环境中的 API 密钥。

网页 API：

| 功能 | 路由 |
|---|---|
| 启动会话 | `POST /api/bootstrap`，启动凭据只放网页 fragment |
| 状态、会话 | `GET /api/state`；`POST /api/sessions`；`GET /api/sessions/{id}` |
| 聊天、恢复生成状态 | `POST /api/chat`；`GET /api/requests/{id}?session_id=...`；`POST /api/requests/{id}/stop` |
| 搜索、展开、来源 | `GET /api/search`；`GET /api/messages/{id}`；`GET /api/messages/{id}/sources` |
| 纠正、遗忘 | `POST /api/corrections`；`GET /api/messages/{id}/delete-preview`；`POST /api/forget` |
| 回收站 | `GET /api/recycle`；`POST /api/recycle/{id}/restore` 或 `/purge`；`POST /api/recycle/clear` |
| 附件 | `POST /api/uploads`；`GET /api/attachments/{id}`；`POST .../recognize` 或 `.../correction` |
| 保存开关 | `POST /api/recording`，开启和暂停均清理临时上下文边界 |
| 配置与后台 | `POST /api/config/models`、`/config/test`、`/config`、`/config/credentials/clear`、`/persona`、`/embedding`、`/jobs/retry` |
| 导出与快照 | `POST /api/exports`、`/snapshots`；`POST /api/snapshots/{id}/verify` 或 `/restore` |
| 管理 | `GET /api/products`；`POST /api/users/open`、`/quit` |

修改操作需要本机 Origin、HttpOnly 会话 cookie 和 CSRF token；主机头仅接受绑定的回环端口。不启用 CORS。聊天正文通过 `textContent` 显示，CSP 禁止内联脚本。普通附件入口拒绝回收站内容，回收站管理入口独立。

连接设置：`/config/models` 只查询模型目录，无需先选择聊天模型；`/config/test` 用固定短文本验证聊天及可选整理模型的完整流式回复，不改动当前连接或记忆。`/config` 通过同样验证后才保存并切换，验证失败保留原连接及配置文件。回复测试不依赖 `/models` 接口，也不验证媒体能力。测试结果含 `chat_verified`、`models`（实际测试的模型）、`latency_ms`、规范化 `base_url` 及 `media_verified:false`。

`request_timeout` 为单次模型请求的总时限，整数 30–600 秒，默认 180 秒，覆盖连接、等待、推理及传输；连接本身另有 20 秒上限。此设置同样传递给整理和图片描述适配器。持续心跳不会绕过总时限；流式 `finish_reason:stop` 或 `[DONE]` 是完成标记，完成后不继续等待网关 EOF。连接测试失败明确标注聊天模型或整理模型，不冒充已保存的聊天草稿。

`thinking_mode` 接受 `default`（默认）、`off`、`on`。对官方硅基流动域名下的 `Qwen/Qwen3.5-*`，后两者分别映射为布尔 `enable_thinking:false/true`，默认不发送该参数。其他地址或模型不发送这一厂商扩展。基础回复探针显式使用 `enable_thinking:false`；结果的 `probe_thinking_disabled` 列出受影响模型，`thinking_verified:false` 表示尚未验证思考模式。探针不会修改保存的日常聊天模式。

配置请求仅接受既定字段落盘，排除 `api_key`。空白密钥默认仅对相同的当前 API 基址沿用现有内存密钥；`use_existing_key:false` 可显式禁止沿用。`/config/credentials/clear` 清除当前远程连接并回到模拟模式。`/state` 的 `connection.active` 和 `connection.has_key` 只暴露状态，不返回密钥或掩码。HTTP 错误仅显示状态码和固定排错建议，不把上游响应正文返给网页。

检索默认最多 30 条，核心最多 200 条；原文展开每侧最多 20 条；分页 offset 最多 10000。模型上下文使用保守的 UTF-8 字节预算和固定候选数，原始媒体另预留预算，供应商仍可能因自己的 token 计量或媒体限制拒绝请求；此时草稿丢弃并显示失败。

适配器当前支持常见文字流式响应、图片 data URL、WAV/MP3 `input_audio` 和独立语音转写；实际服务须配置其能力。M4A/OGG 可以本地保存播放并走转写，不能直接当作 WAV/MP3 上传聊天。未配置识别时保留“未识别”，不会发送本机文件路径冒充媒体。
