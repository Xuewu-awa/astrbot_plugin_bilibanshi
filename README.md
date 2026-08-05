# B站搬石插件

## 简介

这是一个 AstrBot 插件，用于随机搜索 B 站视频并发送到群聊。

支持功能：

- 定时自动搬石
- 当前聊天手动搬石
- 已发送视频标题去重
- `/bilibanshi now` 防刷屏冷却
- 搜索关键词管理
- 最大视频时长限制
- 群白名单 / 黑名单模式
- 免打扰时段

## 工作方式

插件会在群消息中自动记录机器人所在群的信息，之后定时任务会向这些已记录的群推送视频。

插件还会在每次视频成功发送后记录该视频标题；后续搜索到相同标题时会自动跳过，避免重复搬运同一个视频。

为防止手动触发刷屏，`/bilibanshi now` 加入了冷却限制：如果 1 分钟内连续触发该命令，则会进入 1 分钟冷却，在冷却结束前不会再次执行发送。

## 设置项

可在 AstrBot 插件设置页中配置：

- `auto_start`：开机自启动
- `scan_interval`：扫描间隔（秒）
- `max_duration`：最大视频时长（秒）
- `max_pages`：搜索页数
- `video_quality`：视频画质（16/32/64/80，默认 64=720P）
- `transcode`：强制转码为 H.264 main profile（默认关闭，兼容性最好但更耗 CPU）
- `delete_after_send`：发送后删除本地视频
- `search_keywords`：搜索关键词列表
- `use_whitelist_mode`：是否启用白名单模式
- `whitelist_groups`：白名单群号列表
- `blacklist_groups`：黑名单群号列表
- `quiet_hours_start`：免打扰开始时间
- `quiet_hours_end`：免打扰结束时间

### 群推送模式说明

#### 黑名单模式

默认模式。

- `blacklist_groups` 中的群不发送视频
- 其他已绑定群正常发送视频

#### 白名单模式

- 只有 `whitelist_groups` 中的群会发送视频
- 如果白名单为空，则不会向任何群发送视频

说明：

- 定时搬石会按照当前模式过滤群
- `/bilibanshi now` 在群内手动触发时，也会遵守当前模式
- 白名单/黑名单配置以 AstrBot 配置为唯一数据源（WebUI 或命令修改均可，重启不会丢失）；从旧版本升级时，插件会自动迁移旧版 `data/access_control_state.json` 中的名单

## 指令列表

> 所有指令（包括 `/bilibanshi list`）均需要**管理员权限**才能执行，防止普通群成员查看服务器信息、随意触发下载或修改配置。

### 基础控制

- `/bilibanshi on`：开启定时搬石
- `/bilibanshi off`：关闭定时搬石
- `/bilibanshi now`：立即执行一次，发送到当前聊天
- `/bilibanshi list`：查看当前状态

说明：

- `/bilibanshi now` 在 1 分钟内若连续触发，会进入 60 秒冷却
- `/bilibanshi list` 会显示当前已记录标题数量和 `/bilibanshi now` 冷却状态

### 配置管理

- `/bilibanshi interval <秒>`：设置搬石间隔
- `/bilibanshi maxduration <秒>`：设置最大视频时长
- `/bilibanshi mode <whitelist|blacklist>`：切换群推送模式

### 关键词管理

- `/bilibanshi keyword add <关键词>`
- `/bilibanshi keyword remove <关键词>`

### 黑名单管理

- `/bilibanshi blacklist add <群号>`
- `/bilibanshi blacklist remove <群号>`

### 白名单管理

- `/bilibanshi whitelist add <群号>`
- `/bilibanshi whitelist remove <群号>`

### 其他指令

- `/bilibanshi clean`：清理当前记录的临时文件

## 使用示例

### 避免重复发送同一视频

- 插件每次成功发送视频后，都会记录该视频标题
- 之后再次搜索到相同标题时会自动跳过
- 记录数据保存在 AstrBot 数据目录的 `data/plugin_data/astrbot_plugin_bilibanshi/runtime_state.json`（旧版插件目录下的 `data/` 会在首次启动时自动迁移）

### 防止手动刷屏

1. 第一次执行：`/bilibanshi now`
2. 如果在 1 分钟内再次执行 `/bilibanshi now`
3. 插件会拒绝本次请求，并进入 60 秒冷却
4. 可使用 `/bilibanshi list` 查看剩余冷却时间

### 只允许指定群接收视频

1. 在设置中开启 `use_whitelist_mode`
2. 或发送命令：`/bilibanshi mode whitelist`
3. 添加允许接收视频的群：
   - `/bilibanshi whitelist add 123456`
   - `/bilibanshi whitelist add 234567`

### 屏蔽某些群

1. 保持默认黑名单模式
2. 或发送命令：`/bilibanshi mode blacklist`
3. 添加不接收视频的群：
   - `/bilibanshi blacklist add 123456`

### 视频发送失败排查（QQ/NapCat）

如果遇到 `rich media transfer failed` 错误：

1. 先手动在 QQ 里发送一个 mp4 视频测试。手动也发不出 → 是 QQ/NapCat 侧问题（版本不匹配、风控、上传接口故障），与插件无关
2. 手动能发但插件发不出 → 请更新插件到最新版本（v1.2.0 起：视频默认 720P、H.264 优先、文件以绝对路径发送、失败时保留文件并提示）
3. 仍失败可在插件设置中将 `video_quality` 调低（如 32）或开启 `transcode` 强制转码
4. 插件发送失败时会保留视频文件并提示路径，可用 `/bilibanshi clean` 清理
5. v1.2.0 起插件会为视频自动生成封面随消息发送（NapCat 发送视频需要缩略图，缺失会导致上传失败，见 NapCat #1435/#1485）

## 项目结构（v1.2.0）

```
main.py            # 插件入口：命令路由、定时任务、核心流程
bilibili_client.py # B站API客户端：wbi签名、搜索、播放流地址
downloader.py     # 视频下载、ffmpeg合并（H.264兼容QQ发送）、临时文件清理
state_store.py     # JSON状态原子持久化（并发安全）
group_policy.py    # 白名单/黑名单策略与旧版数据迁移
_conf_schema.json  # 配置 schema（含白名单配置项）
```

## 依赖

- `FFmpeg`：用于音视频合并

## 许可证

MIT
