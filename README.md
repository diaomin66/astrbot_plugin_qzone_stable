# QQ空间Ultra（QzoneUltra）

面向 AstrBot 的 QQ 空间插件。当前版本在本地 daemon、Cookie 持久化、图片/文件发布、自然语言 LLM 回复和点赞校验安全性的基础上，对标 `Zhalslar/astrbot_plugin_qzone` 的中文命令体验。

## 功能

- 查看 QQ 空间说说、详情、评论、访客。
- 点赞、评论、回复评论、发布和删除自己的说说。
- AI 写说说、AI 评论、AI 回评。
- 表白墙投稿、匿名投稿、撤稿、看稿、过稿、拒稿。
- 定时自动发说说、自动评论好友说说，可配置随机时间偏移，并持久记录已自动评论的说说，避免重复打扰。
- 可选 pillowmd 样式渲染，看说说、访客、稿件和审核通知会优先渲染成图，失败时回退文本。
- 保留本地 daemon 管理、自动/手动 Cookie 绑定和旧 LLM tools 兼容能力。

## 中文命令

序号从 `0` 开始，`0` 表示最新一条，`-1` 表示当前页最后一条，支持范围语法如 `2~5`。`@用户` 或 QQ 号表示查看指定用户空间；不指定时默认查看好友动态流。

| 命令 | 别名 | 权限 | 参数 | 功能 |
| --- | --- | --- | --- | --- |
| 查看访客 | - | ADMIN | - | 查看最近访客 |
| 看说说 | 查看说说 | ALL | `[@用户/QQ] [序号/范围]` | 查看说说详情 |
| 评说说 | 评论说说、读说说 | ALL | `[@用户/QQ] [序号/范围]` | AI 评论说说，可配置评论后点赞 |
| 赞说说 | - | ALL | `[@用户/QQ] [序号/范围]` | 点赞说说 |
| 发说说 | - | ADMIN | `<文本> [图片]` | 立即发布说说 |
| 写说说 | 写稿 | ADMIN | `<主题> [图片]` | AI 生成待审核稿件 |
| 删说说 | - | ADMIN | `<序号>` | 删除自己发布的说说 |
| 回评 | 回复评论 | ALL | `<稿件ID> [评论序号]` | 回复已查看/已发布稿件下的评论 |
| 投稿 | - | ALL | `<文本> [图片]` | 投稿到表白墙 |
| 匿名投稿 | - | ALL | `<文本> [图片]` | 匿名投稿到表白墙 |
| 撤稿 | - | ALL | `<稿件ID>` | 撤回自己的待审核投稿 |
| 看稿 | 查看稿件 | ADMIN | `[稿件ID]` | 查看待审核稿件 |
| 过稿 | 通过稿件、通过投稿 | ADMIN | `<稿件ID>` | 审核并发布稿件 |
| 拒稿 | 拒绝稿件、拒绝投稿 | ADMIN | `<稿件ID> [原因]` | 拒绝稿件 |

保留的管理命令：

- `/qzone help`
- `/qzone status`
- `/qzone bind <cookie>`
- `/qzone autobind`
- `/qzone unbind`

## LLM tools

对标工具：

- `llm_view_feed`
- `llm_publish_feed`

兼容工具：

- `qzone_get_status`
- `qzone_list_feed`
- `qzone_detail_feed`
- `qzone_publish_post`
- `qzone_comment_post`
- `qzone_like_post`

`qzone_like_post` 会继续区分“请求已被 QQ 空间接受”和“读回校验暂未同步”，不会因为 QQ 空间显示滞后把成功操作误报成失败；用户可见回复会交给 LLM 组织成自然语言，避免泄露 raw JSON、fid、cursor、status_code 等内部字段。

## 配置

除了原有 daemon 配置外，新增/兼容以下配置段：

- `manage_group`：投稿审核通知群；为空时尝试私发管理员。
- `pillowmd_style_dir`：可选的 pillowmd 样式目录；配置后会优先把说说/稿件展示渲染成图片。
- `llm.post_provider_id` / `llm.comment_provider_id` / `llm.reply_provider_id`：分别指定写稿、评论、回评的 LLM provider。
- `llm.post_prompt` / `llm.comment_prompt` / `llm.reply_prompt`：提示词。
- `source.ignore_groups` / `source.ignore_users` / `source.post_max_msg`：自动写稿/读说说来源控制，AI 写稿会尽量抽取群聊文本作为参考上下文。
- `trigger.publish_cron` / `trigger.publish_offset`：自动发说说基准时间和随机偏移秒数。
- `trigger.comment_cron` / `trigger.comment_offset`：自动评论基准时间和随机偏移秒数。
- `trigger.read_prob`：收到消息时概率触发读说说/评论。
- `trigger.send_admin` / `trigger.like_when_comment`：自动评论反馈和评论时点赞。
- `cookies_str`：可选，从配置直接绑定 Cookie。
- `show_name`：过稿发布时在正文头部展示投稿者昵称；匿名投稿显示为“匿名者”。

## 稿件和回评

`看说说`、`评说说`、`赞说说` 查询到的说说会缓存成目标插件风格的稿件 ID，展示文本中会出现 `稿件 #ID`。之后可以用：

```text
回评 ID
回评 ID 0
```

默认回复最后一条非自己评论；指定评论序号时从 `0` 开始。表白墙投稿通过后也会保存发布后的 fid，继续支持同一套 `回评` 流程。

## Cookie

仍支持手动绑定：

```text
/qzone bind p_skey=...; p_uin=o123456789; uin=o123456789; skey=...
```

当 AstrBot 使用 `aiocqhttp` / OneBot v11 时，插件也会尝试通过 `/qzone autobind` 或自动绑定从平台获取 Cookie。
