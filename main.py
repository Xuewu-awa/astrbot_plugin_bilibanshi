import asyncio
import os
import random
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import *
from astrbot.core.message.message_event_result import MessageChain

try:
    # AstrBot 以包方式加载插件
    from .bilibili_client import BilibiliClient, parse_play_count
    from .downloader import Downloader, check_ffmpeg
    from .group_policy import GroupPolicy, normalize_group_id
    from .state_store import StateStore
except ImportError:
    # 以独立文件方式加载（兼容旧版 AstrBot）
    from bilibili_client import BilibiliClient, parse_play_count
    from downloader import Downloader, check_ffmpeg
    from group_policy import GroupPolicy, normalize_group_id
    from state_store import StateStore


@register("astrbot_plugin_bilibanshi", "Xuewu", "B站搬石 - 随机搬视频到群", "1.2.0")
class BilibiliPolluterPlugin(Star):
    # /bilibanshi now 防刷屏参数
    MANUAL_NOW_WINDOW_SECONDS = 60
    MANUAL_NOW_COOLDOWN_SECONDS = 60
    # 已发送标题记录上限，避免 runtime_state.json 无限膨胀
    MAX_SENT_TITLES = 5000

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        # AstrBot WebUI 配置
        self.config = config

        # 数据目录：官方规范要求存于 data/plugin_data/<插件名>/ 下，
        # 避免更新/重装插件时数据被覆盖；旧版插件目录下的 data/ 会自动迁移
        self.data_dir = self._resolve_data_dir()

        # 下载目录
        self.download_dir = self.data_dir / "downloads"
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # 状态持久化（原子写 + 并发锁）
        self.store = StateStore(self.data_dir)

        # 已绑定的群（运行时数据）
        self.bound_groups: Dict[str, str] = self.store.load("bound_groups", {}) or {}

        # 运行状态（已发送标题、手动触发冷却）
        runtime_state = self.store.load("runtime_state", {}) or {}
        self.sent_titles: Dict[str, Dict[str, str]] = self._restore_sent_titles(
            runtime_state.get("sent_titles", {})
        )
        self.last_manual_trigger_ts = float(runtime_state.get("last_manual_trigger_ts", 0) or 0)
        self.manual_cooldown_until_ts = float(runtime_state.get("manual_cooldown_until_ts", 0) or 0)
        self.manual_trigger_lock = asyncio.Lock()

        # 群推送策略（白名单/黑名单，config 为唯一数据源）
        # 旧版本遗留的 access_control_state.json 在 initialize() 中一次性迁移
        self.policy = GroupPolicy(config, self.data_dir / "access_control_state.json")

        # 运行状态
        self.running = False
        self.task: Optional[asyncio.Task] = None
        # 搬石互斥锁：防止定时任务与手动 now 并发下载/写状态
        self.scan_lock = asyncio.Lock()
        # 上次搬石是否失败（用于定时任务失败退避，避免持续触发风控）
        self._last_scan_failed = False

        # 并发控制信号量（最大5个并发请求）
        self.semaphore = asyncio.Semaphore(5)

        # 共享的HTTP会话与网络组件（在 initialize 中创建）
        self.session: Optional[aiohttp.ClientSession] = None
        self.client: Optional[BilibiliClient] = None
        self.downloader: Optional[Downloader] = None

        # 请求头
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        # 检查ffmpeg
        self.ffmpeg_available = check_ffmpeg()
        if not self.ffmpeg_available:
            logger.warning("FFmpeg未安装，视频合并功能将不可用")

        logger.info(f"B站搬石已加载，下载目录: {self.download_dir}")

    @staticmethod
    def _resolve_data_dir() -> Path:
        """解析数据目录：优先官方规范位置，旧版目录数据自动迁移。

        官方规范：data/plugin_data/<plugin_name>/（AstrBot >= 4.9.2）。
        旧版插件目录下的 data/ 会在首次启动时整体迁移。
        """
        plugin_name = "astrbot_plugin_bilibanshi"
        legacy_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "data"

        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            official_dir = Path(get_astrbot_data_path()) / "plugin_data" / plugin_name
        except Exception:
            logger.warning("无法获取 AstrBot 数据目录，回退使用插件目录下的 data/")
            legacy_dir.mkdir(parents=True, exist_ok=True)
            return legacy_dir

        # 一次性迁移旧数据（仅当官方目录尚不存在时）
        if not official_dir.exists() and legacy_dir.exists():
            try:
                legacy_dir.rename(official_dir)
                logger.info(f"已迁移数据目录: {legacy_dir} -> {official_dir}")
            except OSError:
                try:
                    import shutil

                    shutil.move(str(legacy_dir), str(official_dir))
                    logger.info(f"已迁移数据目录: {legacy_dir} -> {official_dir}")
                except Exception as e:
                    logger.error(f"数据目录迁移失败，继续使用旧目录: {e}")
                    legacy_dir.mkdir(parents=True, exist_ok=True)
                    return legacy_dir

        official_dir.mkdir(parents=True, exist_ok=True)
        return official_dir

    # ==================== 生命周期 ====================

    async def initialize(self):
        """插件初始化 - 创建会话、迁移旧配置、启动定时任务"""
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
        self.session = aiohttp.ClientSession(headers=self.headers, timeout=timeout)
        self.client = BilibiliClient(self.session, self.semaphore)
        self.downloader = Downloader(
            self.download_dir,
            self.session,
            self.semaphore,
            self.ffmpeg_available,
        )

        # 迁移旧版本群权限配置（一次性）
        self.policy.migrate_legacy_state()

        # 清理上次异常退出残留的 .m4s/.part 文件
        self.downloader.cleanup_stale_files()

        # 初始化B站cookies（防止412错误）
        await self.client.init_cookies()

        # 开机自启动
        if self.config.get("auto_start", True):
            self.running = True
            self.task = asyncio.create_task(self._timer_task())
            logger.info("B站搬石已开机自启动")

    async def terminate(self):
        """插件卸载时清理"""
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                logger.info("定时任务已取消")
            except Exception as e:
                logger.error(f"定时任务停止时出错: {e}")
            self.task = None

        # 清理临时文件（下载中的协程被 cancel 后已自行清理半成品）
        if self.downloader:
            await self.downloader.cleanup_temp_files()

        # 关闭HTTP会话
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        self.client = None
        self.downloader = None

    # ==================== 自动记录群 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """自动记录群 unified_msg_origin（懒人模式）"""
        try:
            umo = event.unified_msg_origin
            group_id = normalize_group_id(getattr(event.message_obj, "group_id", None))

            if not group_id or not umo:
                return

            # 如果是新群，自动记录
            if group_id not in self.bound_groups:
                self.bound_groups[group_id] = umo
                await self.store.save("bound_groups", self.bound_groups)
                logger.info(f"✅ 自动记录新群: {group_id}")

        except Exception as e:
            logger.error(f"自动记录群失败: {e}")

    # ==================== 标题去重 ====================

    @staticmethod
    def _normalize_title(title: str) -> str:
        """规范化标题，用于去重判断"""
        if not title:
            return ""
        return re.sub(r"\s+", " ", str(title)).strip().lower()

    def _restore_sent_titles(self, raw: Any) -> Dict[str, Dict[str, str]]:
        """从持久化数据恢复已发送标题，并裁剪到上限。"""
        sent_titles: Dict[str, Dict[str, str]] = {}
        if isinstance(raw, dict):
            for _, item in raw.items():
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title", "")).strip()
                normalized_title = self._normalize_title(title)
                if not normalized_title:
                    continue
                sent_titles[normalized_title] = {
                    "title": title,
                    "sent_at": str(item.get("sent_at", "")).strip(),
                    "failed": item.get("failed") is True,
                }
        self._prune_sent_titles(sent_titles)
        return sent_titles

    def _prune_sent_titles(self, sent_titles: Optional[Dict[str, Dict[str, str]]] = None) -> None:
        """删除最旧的记录，保持上限。"""
        target = sent_titles if sent_titles is not None else self.sent_titles
        while len(target) > self.MAX_SENT_TITLES:
            target.pop(next(iter(target)))

    def _has_sent_title(self, title: str) -> bool:
        """判断标题是否已处理过（成功发送或失败记录）"""
        normalized_title = self._normalize_title(title)
        if not normalized_title:
            return False
        return normalized_title in self.sent_titles

    def _runtime_state_dict(self) -> Dict[str, Any]:
        return {
            "sent_titles": self.sent_titles,
            "last_manual_trigger_ts": self.last_manual_trigger_ts,
            "manual_cooldown_until_ts": self.manual_cooldown_until_ts,
        }

    async def _record_sent_title(self, title: str, failed: bool = False) -> None:
        """记录已处理标题（failed=True 表示发送/下载失败，避免死循环重试）"""
        normalized_title = self._normalize_title(title)
        if not normalized_title:
            return

        self.sent_titles[normalized_title] = {
            "title": str(title).strip(),
            "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "failed": failed,
        }
        self._prune_sent_titles()
        await self.store.save("runtime_state", self._runtime_state_dict())
        if failed:
            logger.info(f"已记录失败标题(后续跳过): {title}")
        else:
            logger.info(f"已记录发送标题: {title}")

    # ==================== 手动触发冷却 ====================

    async def _check_manual_now_cooldown(self) -> Optional[str]:
        """检查 /bilibanshi now 的冷却状态"""
        async with self.manual_trigger_lock:
            now_ts = datetime.now().timestamp()

            if self.manual_cooldown_until_ts > now_ts:
                remaining_seconds = max(
                    1, int(self.manual_cooldown_until_ts - now_ts + 0.999)
                )
                return f"⏳ /bilibanshi now 冷却中，请 {remaining_seconds} 秒后再试"

            if (
                self.last_manual_trigger_ts
                and now_ts - self.last_manual_trigger_ts < self.MANUAL_NOW_WINDOW_SECONDS
            ):
                self.manual_cooldown_until_ts = (
                    now_ts + self.MANUAL_NOW_COOLDOWN_SECONDS
                )
                await self.store.save("runtime_state", self._runtime_state_dict())
                logger.info("检测到 /bilibanshi now 1分钟内重复触发，已进入冷却")
                return "⏳ 检测到 1 分钟内重复触发，已进入 60 秒冷却，请稍后再试"

            self.last_manual_trigger_ts = now_ts
            self.manual_cooldown_until_ts = 0.0
            await self.store.save("runtime_state", self._runtime_state_dict())
            return None

    def _get_manual_now_cooldown_remaining(self) -> int:
        """获取 /bilibanshi now 剩余冷却秒数"""
        now_ts = datetime.now().timestamp()
        if self.manual_cooldown_until_ts <= now_ts:
            return 0
        return max(1, int(self.manual_cooldown_until_ts - now_ts + 0.999))

    # ==================== 搜索部分 ====================

    def _should_include_video(self, title: str) -> bool:
        """判断视频标题是否包含关键词（空关键词自动忽略）"""
        if not title:
            return False

        title_lower = title.lower()
        for keyword in self.config.get("search_keywords", []):
            keyword_lower = str(keyword).strip().lower()
            if not keyword_lower:
                continue
            if keyword_lower in title_lower:
                return True
        return False

    async def _search_videos_by_keyword(
        self, keyword: str, max_pages: int = 3
    ) -> List[Dict[str, Any]]:
        """使用指定关键词搜索视频，返回符合要求的视频列表"""
        if not self.client:
            logger.error("HTTP会话未初始化")
            return []

        videos: List[Dict[str, Any]] = []
        max_duration = self.config.get("max_duration", 600)
        logger.info(f"搜索关键词: {keyword}, 最大时长: {max_duration}秒")

        for page in range(1, max_pages + 1):
            items = await self.client.search_page(keyword, page)
            if not items:
                break  # 接口失败或无结果，不再翻页

            for video in items:
                title = video.get("title", "")
                if not self._should_include_video(title):
                    continue

                if self._has_sent_title(title):
                    logger.debug(f"视频标题已处理过，跳过: {title}")
                    continue

                duration_seconds = video.get("duration_seconds", 0)
                if duration_seconds > max_duration:
                    logger.debug(
                        f"视频时长 {duration_seconds}秒 超过限制 {max_duration}秒，跳过: {title}"
                    )
                    continue

                video["play_count"] = parse_play_count(video.get("play", 0))
                videos.append(video)
                logger.debug(f"找到符合时长的视频: {title} ({video.get('duration', '')})")

            # 页间延迟，降低风控概率
            if page < max_pages:
                await asyncio.sleep(random.uniform(0.5, 1))

        logger.info(f"关键词 '{keyword}' 搜索完成: 找到 {len(videos)} 个符合时长的视频")
        return videos

    async def _select_random_video(self) -> Optional[Dict[str, Any]]:
        """随机选一个关键词，随机找一个符合时长要求的视频"""
        keywords = self.config.get("search_keywords", [])
        if not keywords:
            logger.error("没有搜索关键词")
            return None

        max_duration = self.config.get("max_duration", 600)
        max_attempts = 10  # 最大尝试次数，避免死循环

        for attempt in range(max_attempts):
            keyword = random.choice(keywords)
            logger.info(f"第{attempt+1}次尝试，选中关键词: {keyword}")

            max_pages_config = self.config.get("max_pages", 3)
            try:
                pages = random.randint(1, max(1, int(max_pages_config)))
            except (TypeError, ValueError):
                pages = 3

            videos = await self._search_videos_by_keyword(keyword, pages)

            if not videos:
                logger.info(f"关键词 '{keyword}' 没有找到符合时长的视频")
                continue

            unsent_videos = [
                v for v in videos if not self._has_sent_title(v.get("title", ""))
            ]
            if not unsent_videos:
                logger.info(f"关键词 '{keyword}' 搜索结果均为已处理标题，重新选择")
                continue

            selected = random.choice(unsent_videos)
            duration = selected.get("duration_seconds", 0)
            logger.info(
                f"随机选中视频: {selected['title']} "
                f"(时长: {selected.get('duration', '')}, {duration}秒)"
            )

            if duration <= max_duration:
                return selected
            logger.info(f"视频时长 {duration}秒 超过限制，重新选择")

        logger.error(f"尝试 {max_attempts} 次后仍未找到合适的视频")
        return None

    # ==================== 发送部分 ====================

    def _create_video_message(
        self,
        video_info: Dict[str, Any],
        file_path: str,
        cover_path: Optional[str] = None,
    ) -> List[Any]:
        """创建视频消息链。cover_path: 视频封面（NapCat 缺缩略图时上传会失败）。"""
        title = video_info.get("title", "未知标题")
        author = video_info.get("author", "未知UP主")
        play = video_info.get("play_count", 0)
        bvid = video_info.get("bvid", "")
        duration = video_info.get("duration", "未知")

        text_msg = (
            f"【B站搬石】\n"
            f"标题：{title}\n"
            f"UP主：{author}\n"
            f"时长：{duration}\n"
            f"播放量：{play}\n"
            f"链接：https://www.bilibili.com/video/{bvid}"
        )

        chain = [Plain(text_msg)]

        # 检查文件是否存在
        if file_path and os.path.exists(file_path) and os.path.getsize(file_path) > 0:
            video = Video.fromFileSystem(path=file_path)
            # 关键：直接用绝对路径作为 file 字段，避免 file:/// URI 在 NapCat
            # 等协议端对中文/特殊字符路径解码失败导致 "rich media transfer failed"
            video.file = str(Path(file_path).resolve())
            # 附带封面：NapCat 发送视频需要缩略图，缺失时上传失败（NapCat #1435/#1485）
            if cover_path and os.path.exists(cover_path):
                video.cover = str(Path(cover_path).resolve())
            chain.append(video)

        return chain

    async def _send_to_current_chat(
        self,
        event: AstrMessageEvent,
        file_path: str,
        video_info: Dict[str, Any],
        cover_path: Optional[str] = None,
    ) -> bool:
        """直接发送到当前聊天（不走 RespondStage，可感知发送失败）。

        返回是否发送成功。
        """
        chain = self._create_video_message(video_info, file_path, cover_path)
        try:
            await event.send(MessageChain(chain))
            logger.info("已发送到当前聊天")
            return True
        except Exception as e:
            logger.error(f"发送到当前聊天失败: {e}")
            return False

    async def _send_to_all_groups(
        self,
        file_path: str,
        video_info: Dict[str, Any],
        cover_path: Optional[str] = None,
    ) -> tuple[int, int]:
        """发送到所有已绑定的群，返回 (成功数, 尝试数)"""
        if not self.bound_groups:
            logger.warning("没有已绑定的群，等待自动记录...")
            return 0, 0

        target_groups = self.policy.allowed_bound_groups(self.bound_groups)
        if not target_groups:
            logger.warning(
                f"当前为{self.policy.mode_name()}，没有可发送的群，跳过本次推送"
            )
            return 0, 0

        skipped_count = len(self.bound_groups) - len(target_groups)
        if skipped_count > 0:
            logger.info(
                f"群推送过滤完成: 已绑定 {len(self.bound_groups)} 个群，"
                f"可发送 {len(target_groups)} 个，跳过 {skipped_count} 个"
            )

        chain = self._create_video_message(video_info, file_path, cover_path)
        message_chain = MessageChain(chain)

        # 使用信号量控制并发发送（最多同时发3个群）
        send_semaphore = asyncio.Semaphore(3)
        send_success_count = 0
        send_attempt_count = 0

        async def send_to_group(group_id: str, umo: str):
            nonlocal send_success_count, send_attempt_count
            async with send_semaphore:
                if not self.policy.should_send(group_id):
                    logger.info(
                        f"发送前最终校验拦截群 {group_id}："
                        f"{self.policy.block_reason(group_id)}"
                    )
                    return
                send_attempt_count += 1
                try:
                    await self.context.send_message(umo, message_chain)
                    send_success_count += 1
                    logger.info(f"已发送到群 {group_id}")
                except Exception as e:
                    logger.error(f"发送到群 {group_id} 失败: {e}")
                # 群之间延迟
                await asyncio.sleep(1)

        tasks = [send_to_group(gid, umo) for gid, umo in target_groups.items()]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"本次群推送成功发送到 {send_success_count} 个群")
        return send_success_count, send_attempt_count

    # ==================== 核心流程 ====================

    def _get_manual_block_message(self, event: AstrMessageEvent) -> Optional[str]:
        """检查手动触发所在群是否允许接收视频"""
        group_id = getattr(event.message_obj, "group_id", None)
        if group_id is None:
            return None
        group_id = str(group_id)
        if self.policy.should_send(group_id):
            return None
        return f"❌ {self.policy.block_reason(group_id)}，已取消发送"

    async def _execute_scan_and_download(
        self,
    ) -> tuple[bool, Optional[str], Optional[str], Optional[Dict[str, Any]]]:
        """执行扫描和下载的核心逻辑，返回 (成功标志, 文件路径, 封面路径, 视频信息)"""
        target = await self._select_random_video()
        if not target:
            logger.error("没有找到合适的视频")
            return False, None, None, None

        logger.info(
            f"选中视频: {target['title']} (BV: {target['bvid']}, 时长: {target.get('duration', '')})"
        )

        file_path, cover_path = await self.downloader.download_video(
            target,
            self.client,
            quality=int(self.config.get("video_quality", 64) or 64),
            transcode=bool(self.config.get("transcode", False)),
        )
        if not file_path or not os.path.exists(file_path):
            logger.error("下载失败")
            return False, None, None, target

        return True, file_path, cover_path, target

    async def _cleanup_after_send(self, file_path: str, cover_path: Optional[str] = None) -> None:
        """发送后清理文件（视频 + 封面）"""
        if self.config.get("delete_after_send", True):
            for path in [file_path, cover_path]:
                if not path:
                    continue
                try:
                    if os.path.exists(path):
                        os.remove(path)
                        if self.downloader:
                            self.downloader.temp_files.discard(path)
                        logger.info(f"已删除文件: {path}")
                except Exception as e:
                    logger.error(f"删除文件失败 {path}: {e}")

    async def _scan_and_download(self, event: Optional[AstrMessageEvent] = None):
        """扫描并下载视频的主流程。

        Args:
            event: 有 event 说明是命令触发，发送到当前聊天（直接发送，可感知失败）；
                   无 event 说明是定时任务，发送到所有已绑定的群。
        """
        # 互斥锁：同一时间只允许一个搬石流程（定时任务 / 手动触发）
        async with self.scan_lock:
            logger.info("开始随机搬石...")

            if event:
                block_message = self._get_manual_block_message(event)
                if block_message:
                    await event.send(MessageChain([Plain(block_message)]))
                    return
            else:
                if not self.policy.allowed_bound_groups(self.bound_groups):
                    logger.warning(
                        f"当前为{self.policy.mode_name()}，没有可发送的群，跳过本次搬石"
                    )
                    return

            # 执行下载
            success, file_path, cover_path, video_info = await self._execute_scan_and_download()

            if not success:
                self._last_scan_failed = video_info is not None
                if video_info:
                    # 下载失败：记录标题，避免下次反复尝试同一视频
                    await self._record_sent_title(
                        video_info.get("title", ""), failed=True
                    )
                if event:
                    await event.send(
                        MessageChain([Plain("❌ 没有找到合适的视频或下载失败")])
                    )
                return

            # 发送消息
            if event:
                # 命令触发：直接发送到当前聊天，失败可感知
                send_ok = await self._send_to_current_chat(
                    event, file_path, video_info, cover_path
                )
                self._last_scan_failed = not send_ok
                if send_ok:
                    await self._record_sent_title(video_info.get("title", ""))
                    await self._cleanup_after_send(file_path, cover_path)
                else:
                    # 发送失败：记录 failed 标题并保留文件，便于排查
                    await self._record_sent_title(
                        video_info.get("title", ""), failed=True
                    )
                    await event.send(
                        MessageChain(
                            [
                                Plain(
                                    "⚠️ 视频已下载但发送失败（平台拒绝该文件），"
                                    f"文件保留在: {file_path}"
                                )
                            ]
                        )
                    )
                return

            # 定时任务：发送到所有群
            send_success_count, send_attempt_count = await self._send_to_all_groups(
                file_path, video_info, cover_path
            )
            if send_attempt_count > 0:
                # 全部发送失败也记录（failed），避免死循环重试同一视频
                all_failed = send_success_count == 0
                self._last_scan_failed = all_failed
                await self._record_sent_title(
                    video_info.get("title", ""), failed=all_failed
                )
                if all_failed:
                    # 保留文件供排查（可手动发送该文件判断是文件问题还是协议端问题）
                    logger.warning(
                        f"视频发送到所有群失败，文件保留: {file_path} "
                        f"（可用 /bilibanshi clean 清理）"
                    )
                    return
            else:
                self._last_scan_failed = False
                logger.warning(
                    f"视频未发送到任何群，不记录标题: {video_info.get('title', '')}"
                )

            # 清理文件
            await self._cleanup_after_send(file_path, cover_path)
            self._last_scan_failed = False

    # ==================== 定时任务 ====================

    @staticmethod
    def _normalize_hhmm(value: Any) -> str:
        """规范化 HH:MM 时间，兼容 "9:00" 等非零填充格式。"""
        if not value:
            return ""
        parts = str(value).strip().split(":")
        if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
            try:
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
            except ValueError:
                return ""
        return ""

    def _is_quiet_hours(self) -> bool:
        """检查当前是否在免打扰时段内（支持跨午夜）"""
        quiet_start = self._normalize_hhmm(self.config.get("quiet_hours_start", ""))
        quiet_end = self._normalize_hhmm(self.config.get("quiet_hours_end", ""))

        if not quiet_start or not quiet_end:
            return False

        now = datetime.now().strftime("%H:%M")
        if quiet_start <= quiet_end:
            # 正常范围，如 01:00 - 08:00
            return quiet_start <= now < quiet_end
        # 跨午夜范围，如 23:00 - 08:00
        return now >= quiet_start or now < quiet_end

    async def _timer_task(self):
        """定时任务（带异常恢复）"""
        while self.running:
            try:
                # 检查免打扰时段
                if self._is_quiet_hours():
                    logger.debug("当前在免打扰时段，跳过本次推送")
                    await asyncio.sleep(self.config.get("scan_interval", 60))
                    continue

                # 执行扫描和下载
                await self._scan_and_download()

                # 等待下一次扫描；发送/下载失败后退避更长时间，
                # 避免持续高频富媒体上传触发 QQ 限流
                if self._last_scan_failed:
                    await asyncio.sleep(max(self.config.get("scan_interval", 60), 600))
                else:
                    await asyncio.sleep(self.config.get("scan_interval", 60))

            except asyncio.CancelledError:
                logger.info("定时任务被取消")
                break
            except Exception as e:
                logger.error(f"定时任务异常: {e}")
                # 发生异常时等待较长时间再重试，避免频繁失败
                await asyncio.sleep(60)

    # ==================== 指令区 ====================

    @filter.command("bilibanshi on")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def turn_on(self, event: AstrMessageEvent):
        """开启搬石"""
        if self.running:
            yield event.plain_result("搬石已经在运行了")
            return

        self.running = True
        self.task = asyncio.create_task(self._timer_task())
        yield event.plain_result("B站搬石已开启")

    @filter.command("bilibanshi off")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def turn_off(self, event: AstrMessageEvent):
        """关闭搬石"""
        if not self.running:
            yield event.plain_result("搬石已经关闭了")
            return

        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                logger.info("定时任务已取消")
            except Exception as e:
                logger.error(f"停止定时任务时出错: {e}")
            self.task = None
        yield event.plain_result("B站搬石已关闭")

    @filter.command("bilibanshi now")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def scan_now(self, event: AstrMessageEvent):
        """立即执行一次（发送到当前聊天）"""
        block_message = self._get_manual_block_message(event)
        if block_message:
            await event.send(MessageChain([Plain(block_message)]))
            return

        cooldown_message = await self._check_manual_now_cooldown()
        if cooldown_message:
            await event.send(MessageChain([Plain(cooldown_message)]))
            return

        # 定时任务或另一个手动触发正在搬石时，避免无提示排队等待
        if self.scan_lock.locked():
            await event.send(MessageChain([Plain("⏳ 正在执行搬石任务，请稍候再试")]))
            return

        await event.send(MessageChain([Plain("开始搬石...")]))
        await self._scan_and_download(event)

    @filter.command("bilibanshi list")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def list_status(self, event: AstrMessageEvent):
        """查看当前状态"""
        max_duration = self.config.get("max_duration", 600)
        use_whitelist_mode = self.policy.is_whitelist_mode()
        cooldown_remaining = self._get_manual_now_cooldown_remaining()
        failed_count = sum(
            1 for item in self.sent_titles.values() if item.get("failed") is True
        )

        status = [
            "=== B站搬石状态 ===",
            f"运行状态: {'✅ 运行中' if self.running else '❌ 已停止'}",
            f"扫描间隔: {self.config.get('scan_interval', 60)}秒",
            f"最大时长: {max_duration}秒 ({max_duration // 60}分钟)",
            f"已记录标题: {len(self.sent_titles)} 个"
            + (f"（其中失败 {failed_count} 个）" if failed_count else ""),
            f"/bilibanshi now 冷却: {'⏳ 剩余 ' + str(cooldown_remaining) + ' 秒' if cooldown_remaining > 0 else '✅ 无'}",
            f"群推送模式: {self.policy.mode_name()}",
            f"已绑定的群: {len(self.bound_groups)} 个",
            f"白名单群: {len(self.policy.group_list('whitelist_groups'))} 个",
            f"黑名单群: {len(self.policy.group_list('blacklist_groups'))} 个",
            f"关键词: {len(self.config.get('search_keywords', []))} 个",
            f"数据目录: {self.data_dir}",
            f"FFmpeg: {'✅ 可用' if self.ffmpeg_available else '❌ 不可用'}",
        ]

        whitelist_groups = sorted(self.policy.group_list("whitelist_groups"))
        blacklist_groups = sorted(self.policy.group_list("blacklist_groups"))

        if use_whitelist_mode and not whitelist_groups:
            status.append("提示: 当前为白名单模式，但白名单为空，不会向任何群推送")
        else:
            active_list_name = "白名单" if use_whitelist_mode else "黑名单"
            active_groups = whitelist_groups if use_whitelist_mode else blacklist_groups
            if active_groups:
                status.append(f"当前生效的{active_list_name}: {', '.join(active_groups)}")

        # 显示已绑定的群号
        if self.bound_groups:
            status.append(f"群列表: {', '.join(self.bound_groups.keys())}")

        yield event.plain_result("\n".join(status))

    @filter.command("bilibanshi mode")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_group_mode(self, event: AstrMessageEvent):
        """设置群推送模式: /bilibanshi mode whitelist 或 /bilibanshi mode blacklist"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result(
                f"当前模式: {self.policy.mode_name()}\n用法: /bilibanshi mode <whitelist|blacklist>"
            )
            return

        mode = parts[2].lower()
        if mode in ["whitelist", "white", "白名单"]:
            self.config["use_whitelist_mode"] = True
            self.config.save_config()
            message = "✅ 已切换到白名单模式"
            if not self.policy.group_list("whitelist_groups"):
                message += "（当前白名单为空，不会向任何群推送）"
            yield event.plain_result(message)
        elif mode in ["blacklist", "black", "黑名单"]:
            self.config["use_whitelist_mode"] = False
            self.config.save_config()
            yield event.plain_result("✅ 已切换到黑名单模式")
        else:
            yield event.plain_result("模式只能是 whitelist 或 blacklist")

    @filter.command("bilibanshi blacklist")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_blacklist(self, event: AstrMessageEvent):
        """管理黑名单: /bilibanshi blacklist add 123456 或 remove 123456"""
        parts = event.message_str.strip().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /bilibanshi blacklist <add|remove> <群号>")
            return

        action = parts[2].lower()
        group_id = parts[3]
        if not group_id.isdigit():
            yield event.plain_result("群号必须是数字")
            return

        message = self.policy.update_list("blacklist_groups", action, group_id, "黑名单")
        yield event.plain_result(message)

    @filter.command("bilibanshi whitelist")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_whitelist(self, event: AstrMessageEvent):
        """管理白名单: /bilibanshi whitelist add 123456 或 remove 123456"""
        parts = event.message_str.strip().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /bilibanshi whitelist <add|remove> <群号>")
            return

        action = parts[2].lower()
        group_id = parts[3]
        if not group_id.isdigit():
            yield event.plain_result("群号必须是数字")
            return

        message = self.policy.update_list("whitelist_groups", action, group_id, "白名单")
        yield event.plain_result(message)

    @filter.command("bilibanshi keyword")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def manage_keyword(self, event: AstrMessageEvent):
        """管理关键词: /bilibanshi keyword add 搞笑 或 remove 搞笑"""
        parts = event.message_str.strip().split()
        if len(parts) < 4:
            yield event.plain_result("用法: /bilibanshi keyword <add|remove> <关键词>")
            return

        action = parts[2].lower()
        keyword = " ".join(parts[3:])  # 支持带空格的关键词

        keywords = self.config.get("search_keywords", [])
        if not isinstance(keywords, list):
            keywords = []

        if action == "add":
            if keyword not in keywords:
                keywords.append(keyword)
                self.config["search_keywords"] = keywords
                self.config.save_config()
                yield event.plain_result(f"✅ 已添加关键词: {keyword}")
            else:
                yield event.plain_result("该关键词已存在")
        elif action == "remove":
            if keyword in keywords:
                keywords.remove(keyword)
                self.config["search_keywords"] = keywords
                self.config.save_config()
                yield event.plain_result(f"✅ 已删除关键词: {keyword}")
            else:
                yield event.plain_result("未找到该关键词")
        else:
            yield event.plain_result("未知操作，请使用 add 或 remove")

    @filter.command("bilibanshi interval")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_interval(self, event: AstrMessageEvent):
        """设置扫描间隔(秒): /bilibanshi interval 60"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /bilibanshi interval <秒数>")
            return

        try:
            interval = int(parts[2])
            if interval < 10:
                yield event.plain_result("间隔不能小于10秒")
                return

            self.config["scan_interval"] = interval
            self.config.save_config()
            yield event.plain_result(f"已设置扫描间隔: {interval}秒")
        except ValueError:
            yield event.plain_result("请输入有效的数字")

    @filter.command("bilibanshi maxduration")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def set_max_duration(self, event: AstrMessageEvent):
        """设置最大时长（秒）: /bilibanshi maxduration 600"""
        parts = event.message_str.strip().split()
        if len(parts) < 3:
            yield event.plain_result("用法: /bilibanshi maxduration <秒数>")
            return

        try:
            duration = int(parts[2])
            if duration < 10:
                yield event.plain_result("时长不能小于10秒")
                return

            self.config["max_duration"] = duration
            self.config.save_config()
            yield event.plain_result(
                f"已设置最大时长: {duration}秒 ({duration // 60}分钟)"
            )
        except ValueError:
            yield event.plain_result("请输入有效的数字")

    @filter.command("bilibanshi clean")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clean_temp_files(self, event: AstrMessageEvent):
        """手动清理临时文件"""
        if not self.downloader:
            yield event.plain_result("插件尚未初始化完成")
            return
        count = await self.downloader.cleanup_temp_files()
        yield event.plain_result(f"已清理 {count} 个临时文件")
