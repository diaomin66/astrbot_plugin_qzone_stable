# QQ空间Ultra（QzoneUltra）

QQ空间Ultra 是一个面向 AstrBot 的 QQ 空间插件，提供中文命令、LLM 工具、Cookie 绑定、本地 daemon、图片发布、说说渲染、投稿审核和自动评论能力。插件优先适配 OneBot v11 / aiocqhttp，也支持手动 Cookie 绑定后使用核心 QQ 空间功能。

## 功能概览

- 查看好友动态、指定用户说说、说说详情、评论和最近访客。
- 点赞、评论、回复评论、发布说说、删除自己发布的说说。
- AI 写说说、AI 评论、AI 回评，生成内容会走当前 AstrBot 会话 provider 和人设。
- 支持文本、图片和文件形式的说说发布，并可返回 QQ 空间风格发布结果图。
- 表白墙投稿、匿名投稿、撤稿、看稿、过稿、拒稿。
- 定时自动发说说、自动评论好友说说，并记录已处理动态，避免重复打扰。
- 可选 pillowmd 风格渲染；渲染失败时自动回退文本。
- LLM tool 结果会转成自然语言回复，避免向用户暴露 raw JSON、fid、cursor 等内部字段。

## 安装

1. 将本仓库放入 AstrBot 的插件目录，例如 `data/plugins/astrbot_plugin_qzone_ultra`。
2. 在插件目录安装依赖：

```bash
pip install -r requirements.txt
```

3. 重启 AstrBot，或在 AstrBot 管理面板重新加载插件。
4. 在 QQ 私聊或群聊里发送 `/qzone status` 检查 daemon 和 Cookie 状态。

插件会在首次使用时按需启动本地 daemon。daemon 只监听 `127.0.0.1`，用于隔离 QQ 空间请求、Cookie 管理和渲染逻辑。

## Cookie 绑定

推荐在 OneBot v11 / aiocqhttp 环境使用自动绑定：

```text
/qzone autobind
```

如果平台无法提供 Cookie，可以手动绑定：

```text
/qzone bind p_skey=...; p_uin=o123456789; uin=o123456789; skey=...
```

也可以在 AstrBot 插件配置里填写 `cookies_str`，插件初始化时会尝试自动写入登录态。

## 中文命令

序号从 `1` 开始，`1` 或 `最新` 表示最新一条，`-1` 表示当前页最后一条。支持范围语法，例如 `1~3`。旧用法里的 `0` 会兼容为最新一条。

| 命令 | 别名 | 权限 | 用法 | 说明 |
| --- | --- | --- | --- | --- |
| 查看访客 | - | 管理员 | `查看访客` | 查看最近访客 |
| 看说说 | 查看说说 | 所有人 | `看说说 [@用户/QQ] [序号/范围]` | 查看好友动态或指定用户说说 |
| 评说说 | 评论说说、读说说 | 所有人 | `评说说 [@用户/QQ] [序号/范围] [评论内容]` | 评论说说；内容为空时由 AI 生成 |
| 赞说说 | - | 所有人 | `赞说说 [@用户/QQ] [序号/范围]` | 点赞说说 |
| 发说说 | - | 管理员 | `发说说 <文本> [图片]` | 立即发布说说 |
| 写说说 | 写稿 | 管理员 | `写说说 <主题> [图片]` | 生成待审核或待发布文案 |
| 删说说 | - | 管理员 | `删说说 <序号>` | 删除自己发布的说说 |
| 回评 | 回复评论 | 所有人 | `回评 <稿件ID> [评论序号]` | 回复已缓存稿件或已发布说说下的评论 |
| 投稿 | - | 所有人 | `投稿 <文本> [图片]` | 投稿到表白墙 |
| 匿名投稿 | - | 所有人 | `匿名投稿 <文本> [图片]` | 匿名投稿到表白墙 |
| 撤稿 | - | 所有人 | `撤稿 <稿件ID>` | 撤回自己的待审核投稿 |
| 看稿 | 查看稿件 | 管理员 | `看稿 [稿件ID]` | 查看待审核稿件 |
| 过稿 | 通过稿件、通过投稿 | 管理员 | `过稿 <稿件ID>` | 审核并发布稿件 |
| 拒稿 | 拒绝稿件、拒绝投稿 | 管理员 | `拒稿 <稿件ID> [原因]` | 拒绝稿件 |

保留的兼容命令：

```text
/qzone help
/qzone status
/qzone bind <cookie>
/qzone autobind
/qzone unbind
/qzone feed [hostuin] [limit] [cursor]
/qzone detail <hostuin> <fid> [appid]
/qzone post <content>
/qzone comment <hostuin> <fid> <content>
/qzone like <hostuin> <fid> [appid] [unlike]
```

## LLM 工具

推荐工具：

- `llm_view_feed`
- `llm_publish_feed`

兼容工具：

- `qzone_get_status`
- `qzone_list_feed`
- `qzone_view_post`
- `qzone_detail_feed`
- `qzone_publish_post`
- `qzone_comment_post`
- `qzone_delete_post`
- `qzone_like_post`

`qzone_view_post`、`qzone_comment_post`、`qzone_delete_post`、`qzone_like_post` 推荐使用 `target_uin` 加 `selector`。`selector` 可以写 `latest`、`最新`、`第2条`、`2`、`1~3` 或真实 `fid`。旧参数 `hostuin`、`fid`、`appid`、`latest`、`index` 仍保留兼容。

点赞会区分“请求已被 QQ 空间接受”和“读回校验暂未同步”。如果 QQ 空间读回有延迟，插件会保持成功结果并提示校验不确定，不会把已接受的点赞误报为失败。

## 配置

常用配置项：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `admin_uins` | 空 | 管理员 QQ 号，多个用英文逗号分隔 |
| `cookies_str` | 空 | 可选 Cookie 字符串，用于初始化自动绑定 |
| `daemon_port` | `18999` | 本地 daemon 端口 |
| `auto_start_daemon` | `true` | 首次使用时自动启动 daemon |
| `auto_bind_cookie` | `true` | 登录态缺失时尝试从 OneBot 自动获取 Cookie |
| `manage_group` | 空 | 投稿审核通知群；为空时尝试私发管理员 |
| `pillowmd_style_dir` | 空 | 可选 pillowmd 样式目录 |
| `render_publish_result` | `true` | 发布成功后返回渲染图 |
| `llm.post_provider_id` | 空 | 写说说使用的 LLM provider；空表示当前会话默认 provider |
| `llm.comment_provider_id` | 空 | 评论使用的 LLM provider |
| `llm.reply_provider_id` | 空 | 回评使用的 LLM provider |
| `trigger.publish_cron` | 空 | 自动发说说 cron 表达式；空表示关闭 |
| `trigger.comment_cron` | 空 | 自动评论 cron 表达式；空表示关闭 |
| `trigger.read_prob` | `0.0` | 收到消息时概率触发读说说和自动评论 |

完整配置见 `_conf_schema.json`。Cron 表达式格式为 `分 时 日 月 周`，例如 `30 8 * * *` 表示每天 8:30。

## 数据目录

运行数据默认写入 AstrBot 分配给插件的数据目录，通常包括：

- Cookie 和登录状态。
- daemon 状态和保活信息。
- 投稿草稿、稿件 ID、已发布 fid。
- 自动评论去重记录。
- 渲染临时文件和发布结果图。

这些内容属于本地运行数据，不应该提交到 GitHub。仓库已通过 `.gitignore` 忽略 `data/`、缓存目录、虚拟环境和测试目录。

## 排障

- `/qzone status` 显示未绑定：先执行 `/qzone autobind`，失败后使用 `/qzone bind <cookie>`。
- daemon 无法启动：确认 `daemon_port` 没有被占用，并检查 AstrBot 日志。
- 自动绑定失败：确认 AstrBot 使用的是 OneBot v11 / aiocqhttp，且适配器允许获取 Cookie。
- 图片发布失败：确认图片可被 AstrBot 正常读取，远程图片地址可访问。
- LLM 生成内容为空：检查 AstrBot 当前会话 provider，或分别配置 `llm.post_provider_id`、`llm.comment_provider_id`、`llm.reply_provider_id`。
- 点赞成功但提示校验不确定：通常是 QQ 空间读回延迟，可稍后再查看目标说说。

## 发布文件

发布到 GitHub 的插件文件只保留运行必需内容：

- `metadata.yaml`
- `main.py`
- `daemon_main.py`
- `_conf_schema.json`
- `requirements.txt`
- `logo.png`
- `qzone_bridge/`
- `README.md`

测试、缓存、运行数据、虚拟环境和临时文件不属于发布包。
