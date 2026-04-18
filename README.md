# B站搬石插件

## 简介

随机从B站搬石到群的机器人插件。自动搜索关键词、随机选视频、下载并发送。

## 指令列表

### 基础控制

/bilibanshi on 开启定时搬石（开机自启动）
/bilibanshi off 关闭定时搬石
/bilibanshi now 立即执行一次（发送到当前聊天）

### 配置管理

/bilibanshi list 查看当前状态
/bilibanshi interval <秒> 设置搬石间隔（如 3600）
/bilibanshi maxduration <秒> 设置视频最大时长（默认60秒/1分钟）

### 关键词管理

/bilibanshi keyword add <关键词>
/bilibanshi keyword remove <关键词>

### 黑名单管理

/bilibanshi h <群号> 添加黑名单群
/bilibanshi -h <群号> 移除黑名单群

## 功能说明

· **定时任务**：按设定间隔自动搜索并发送到机器人加入的所有群（黑名单除外）

· **手动触发**：/bilibanshi now 只发送到当前聊天

· **黑名单**：屏蔽不想接收的群

## 依赖

· FFmpeg（用于视频合并）
## 许可证
MIT
