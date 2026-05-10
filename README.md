# AstrBot QQ空间 Stable Bridge

更稳定的 QQ 空间插件方案。

## 特点

- 本地 daemon 承担登录态、请求、保活和重试
- AstrBot 负责命令、LLM tools 和展示
- Cookie 持久化，断线后可自动恢复
- 支持从 OneBot v11 平台自动获取 Cookie

## 使用

1. 把插件放进 AstrBot 的 `data/plugins/` 目录。
2. 在面板里填好配置。
3. 用 `/qzone bind <cookie>` 绑定 Cookie。
4. 常用命令：
   - `/qzone status`
   - `/qzone autobind`
   - `/qzone feed`
   - `/qzone detail <hostuin> <fid>`
   - `/qzone post <content>`
   - `/qzone comment <hostuin> <fid> <content>`
   - `/qzone like <hostuin> <fid>`

## Cookie 格式

支持两种输入：

- `p_skey=...; p_uin=o123456789; uin=o123456789; skey=...`
- `{"p_skey":"...","p_uin":"o123456789","uin":"o123456789"}`

## LLM tools

- `qzone_get_status`
- `qzone_list_feed`
- `qzone_detail_feed`
- `qzone_publish_post`
- `qzone_comment_post`
- `qzone_like_post`

