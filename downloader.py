"""视频下载与合并：aiohttp 流式下载 + ffmpeg 音视频合并。

不依赖 astrbot 框架，便于单元测试。
"""
import asyncio
import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import aiofiles
except ImportError:
    aiofiles = None  # 无 aiofiles 时回退到同步写入

try:
    import aiohttp
except ImportError:
    aiohttp = None  # 无 aiohttp 时使用 session 默认超时

logger = logging.getLogger(__name__)

# 合并后是否在容器中前置 moov（利于在线播放/上传）
_FASTSTART = True

# H.264 编码 id（B站 DASH codecid）
CODEC_H264 = 7


def check_ffmpeg() -> bool:
    """检查 ffmpeg 是否可用。"""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=15
        )
        return result.returncode == 0
    except Exception:
        return False


def clean_filename(filename: str) -> str:
    """清理文件名：提取书名号内容、移除非法字符、限制长度。"""
    if not filename:
        return "untitled"

    if "《" in filename and "》" in filename:
        title_match = re.search(r"《([^《》]+)》", filename)
        if title_match:
            filename = title_match.group(1)

    # 移除非法字符与控制字符（含全角问号，避免部分平台解析路径异常）
    cleaned = re.sub(r'[<>:"/\\|?*？\x00-\x1f]', "", filename)
    if len(cleaned) > 100:
        cleaned = cleaned[:100]

    return cleaned.strip() or "untitled"


def build_ffmpeg_cmd(
    video_path, audio_path, output_path, codecid, transcode: bool = False
) -> List[str]:
    """构造 ffmpeg 合并命令。

    - transcode=True 或源编码非 H.264 时转码为 H.264 main profile
      （yuv420p + level 4.0，QQ 等平台兼容性最好）；
    - 否则直接 copy（快且无损）。
    """
    cmd = ["ffmpeg", "-i", str(video_path), "-i", str(audio_path)]
    if transcode or codecid != CODEC_H264:
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-profile:v", "main", "-level", "4.0", "-pix_fmt", "yuv420p",
        ]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "192k"]
    if _FASTSTART:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-y", "-hide_banner", "-loglevel", "error", str(output_path)]
    return cmd


class Downloader:
    """负责下载 DASH 视频/音频流并用 ffmpeg 合并为 mp4。

    Args:
        download_dir: 下载目录（Path 或 str）。
        session: 共享的 aiohttp.ClientSession。
        semaphore: 可选的并发信号量；为 None 时内部创建。
        ffmpeg_available: ffmpeg 是否可用。
    """

    # 单个文件大小上限（视频+音频各 512MB），防止磁盘耗尽
    MAX_FILE_SIZE = 512 * 1024 * 1024

    def __init__(
        self,
        download_dir,
        session,
        semaphore: Optional[asyncio.Semaphore] = None,
        ffmpeg_available: bool = False,
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.semaphore = semaphore or asyncio.Semaphore(5)
        self.ffmpeg_available = ffmpeg_available
        # 当前会话产生的临时文件（用于 terminate / clean 命令清理）
        self.temp_files: Set[str] = set()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    async def download_video(
        self, video_info: Dict, client, quality: int = 64, transcode: bool = False
    ) -> Tuple[Optional[str], Optional[str]]:
        """下载并合并单个视频，返回 (mp4 路径, 封面路径)；失败返回 (None, None)。

        quality: 目标画质（16/32/64/80）；transcode: 是否强制转码。
        封面用于 NapCat 等协议端发送视频（缺失缩略图会导致上传失败，见
        NapCat #1435/#1485）；封面生成失败不影响主流程。
        任何失败路径都会清理本次产生的半成品文件。
        """
        bvid = video_info.get("bvid", "")
        title = video_info.get("title", "未知标题")
        if not bvid:
            logger.error("没有bvid，无法下载")
            return None, None

        cleaned_title = clean_filename(title)
        unique_id = str(uuid.uuid4())[:8]
        video_temp = self.download_dir / f"video_{unique_id}.m4s"
        audio_temp = self.download_dir / f"audio_{unique_id}.m4s"
        # bili_ 前缀：防止标题以 "-" 开头时被 ffmpeg 解析为选项参数
        output_path = self.download_dir / f"bili_{cleaned_title}_{unique_id}.mp4"
        cover_path = self.download_dir / f"bili_{cleaned_title}_{unique_id}_cover.jpg"

        self.temp_files.update(
            {str(video_temp), str(audio_temp), str(output_path), str(cover_path)}
        )

        success_path: Optional[str] = None
        try:
            logger.info(f"获取下载地址: {bvid} (quality={quality})")
            video_url, audio_url, codecid = await client.get_video_urls(
                bvid, quality=quality
            )
            if not video_url:
                logger.error(f"获取视频地址失败: {bvid}")
                return None, None
            if not audio_url:
                logger.error(f"获取音频地址失败: {bvid}")
                return None, None

            logger.info(f"下载视频: {title}")
            if not await self._download_file(bvid, video_url, video_temp):
                logger.error(f"视频下载失败: {bvid}")
                return None, None

            logger.info(f"下载音频: {title}")
            if not await self._download_file(bvid, audio_url, audio_temp):
                logger.error(f"音频下载失败: {bvid}")
                return None, None

            if not self.ffmpeg_available:
                logger.error("FFmpeg不可用，无法合并")
                return None, None

            logger.info(f"合并视频音频: {title}")
            if not await self._merge_video_audio(
                video_temp, audio_temp, output_path, codecid, transcode
            ):
                logger.error("合并失败")
                return None, None

            # 生成封面（失败不影响发送，但 NapCat 缺缩略图时上传会失败）
            cover_ok = await self._generate_cover(output_path, cover_path)
            if not cover_ok:
                logger.warning("封面生成失败，将不带封面发送")
                cover_path = None

            logger.info(f"下载完成: {output_path}")
            success_path = str(output_path)
            return success_path, str(cover_path) if cover_path else None
        except asyncio.CancelledError:
            # 任务被取消（如 turn_off），仍然清理半成品文件
            logger.info(f"下载被取消，清理临时文件: {title}")
            raise
        except Exception as e:
            logger.error(f"下载视频异常: {e}")
            return None, None
        finally:
            if success_path is None:
                # 失败路径：清理所有半成品
                self._remove_files([video_temp, audio_temp, output_path, cover_path])
            else:
                # 成功路径：video/audio 临时文件在合并成功后已删除，兜底清理
                self._remove_files([video_temp, audio_temp])

    async def _generate_cover(self, video_path: Path, cover_path: Path) -> bool:
        """用 ffmpeg 提取视频第 1 秒帧作为封面。"""
        try:
            cmd = [
                "ffmpeg",
                "-i", str(video_path),
                "-ss", "1",
                "-vframes", "1",
                "-vf", "scale=480:-2",
                "-q:v", "4",
                "-y",
                "-hide_banner",
                "-loglevel", "error",
                str(cover_path),
            ]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            if process.returncode != 0:
                logger.warning(
                    f"封面生成失败: {(stderr or b'').decode(errors='ignore')[:200]}"
                )
                return False
            return cover_path.exists() and cover_path.stat().st_size > 0
        except asyncio.TimeoutError:
            logger.warning("封面生成超时")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"封面生成异常: {e}")
            return False

    async def cleanup_temp_files(self) -> int:
        """删除当前会话记录的所有临时文件，返回删除数量。"""
        paths = list(self.temp_files)
        self.temp_files.clear()
        removed = 0
        for file_path in paths:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    removed += 1
            except Exception as e:
                logger.error(f"删除临时文件失败 {file_path}: {e}")
        return removed

    def cleanup_stale_files(self) -> int:
        """启动时清理下载目录中的残留 .m4s/.part/.tmp 文件（崩溃残留）。

        不清理 .mp4：可能仍有引用，交由 /bilibanshi clean 或 delete_after_send 处理。
        """
        removed = 0
        try:
            for f in self.download_dir.iterdir():
                if f.is_file() and f.suffix.lower() in (".m4s", ".part", ".tmp"):
                    try:
                        f.unlink()
                        removed += 1
                    except Exception as e:
                        logger.warning(f"清理残留文件失败 {f}: {e}")
        except OSError as e:
            logger.warning(f"扫描下载目录失败: {e}")
        if removed:
            logger.info(f"已清理 {removed} 个崩溃残留的临时文件")
        return removed

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _download_file(self, bvid: str, url: str, output_path: Path) -> bool:
        """流式下载文件。DASH 流要求 Referer 精确到视频页。

        - 下载超时放宽：total=None（不限制总时长），仅 sock_read=60s
          （60 秒无数据才判超时），避免 CDN 波动时大文件下载超时
        - 失败重试 1 次（B 站 CDN 节点抖动是常态，重试成功率很高）
        - 超过 MAX_FILE_SIZE 的响应直接拒绝，防止磁盘耗尽
        """
        headers = {"Referer": f"https://www.bilibili.com/video/{bvid}"}
        # 下载超时放宽：total=None（不限制总时长），仅 sock_read=60s
        timeout = None
        if aiohttp is not None:
            timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)
        for attempt in (1, 2):
            try:
                async with self.semaphore:
                    async with self.session.get(
                        url, headers=headers, timeout=timeout
                    ) as response:
                        if response.status != 200:
                            logger.error(f"下载失败: HTTP {response.status}")
                            return False
                        content_length = response.content_length or 0
                        if content_length > self.MAX_FILE_SIZE:
                            logger.error(
                                f"文件过大({content_length / 1024 / 1024:.0f}MB > "
                                f"{self.MAX_FILE_SIZE / 1024 / 1024:.0f}MB)，拒绝下载"
                            )
                            return False
                        if aiofiles is not None:
                            async with aiofiles.open(output_path, "wb") as f:
                                downloaded = 0
                                async for chunk in response.content.iter_chunked(8192):
                                    downloaded += len(chunk)
                                    if downloaded > self.MAX_FILE_SIZE:
                                        logger.error("下载超过大小上限，已中止")
                                        return False
                                    await f.write(chunk)
                        else:
                            # 回退：同步写入（小 chunk，阻塞可忽略）
                            with open(output_path, "wb") as f:
                                downloaded = 0
                                async for chunk in response.content.iter_chunked(8192):
                                    downloaded += len(chunk)
                                    if downloaded > self.MAX_FILE_SIZE:
                                        logger.error("下载超过大小上限，已中止")
                                        return False
                                    f.write(chunk)
                return True
            except asyncio.TimeoutError:
                logger.warning(f"下载超时(第{attempt}次): {url[:60]}")
                if attempt == 1:
                    await asyncio.sleep(2)
                    continue
                return False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"下载出错(第{attempt}次): {e}")
                if attempt == 1:
                    await asyncio.sleep(2)
                    continue
                return False
        return False

    async def _merge_video_audio(
        self, video_path: Path, audio_path: Path, output_path: Path, codecid, transcode: bool = False
    ) -> bool:
        """用 ffmpeg 合并音视频，带超时保护与进程清理。"""
        try:
            cmd = build_ffmpeg_cmd(
                video_path, audio_path, output_path, codecid, transcode
            )
            if transcode or codecid != CODEC_H264:
                logger.info(f"视频编码 codecid={codecid} 非 H.264，转码为 H.264 main profile")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"启动ffmpeg失败: {e}")
            return False

        try:
            # 超时保护：ffmpeg 卡死时不能永久占用搬石锁
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
        except asyncio.TimeoutError:
            logger.error("合并超时(600s)，已终止 ffmpeg")
            await self._kill_process(process)
            return False
        except asyncio.CancelledError:
            # 任务被取消（turn_off 等）：必须杀掉子进程，避免 ffmpeg 残留
            await self._kill_process(process)
            raise
        except Exception as e:
            logger.error(f"合并出错: {e}")
            await self._kill_process(process)
            return False

        if process.returncode == 0:
            return True
        err = (stderr or b"").decode(errors="ignore")[:300]
        logger.error(f"合并失败: {err}")
        return False

    @staticmethod
    async def _kill_process(process) -> None:
        """终止子进程并等待退出。"""
        try:
            if process.returncode is None:
                process.kill()
                await process.wait()
        except Exception as e:
            logger.warning(f"终止ffmpeg进程失败: {e}")

    def _remove_files(self, paths) -> None:
        """删除文件并从 temp_files 集合中移除。"""
        for path in paths:
            try:
                if path.exists():
                    path.unlink()
                    self.temp_files.discard(str(path))
            except Exception as e:
                logger.warning(f"删除临时文件失败 {path}: {e}")
