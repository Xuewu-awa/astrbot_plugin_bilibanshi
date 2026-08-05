"""B站 API 客户端：wbi 签名、视频搜索、播放流地址获取。

不依赖 astrbot 框架，便于单元测试。
"""
import asyncio
import hashlib
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

logger = logging.getLogger(__name__)

# wbi 签名打乱表（bilibili-API-collect 公开算法）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def get_mixin_key(orig: str) -> str:
    """根据原始 key 计算 mixin key（取打乱后前 32 位）。"""
    return "".join(orig[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def enc_wbi(params: Dict[str, Any], img_key: str, sub_key: str) -> Dict[str, Any]:
    """对请求参数附加 wts 时间戳并计算 w_rid 签名。"""
    mixin_key = get_mixin_key(img_key + sub_key)
    curr_time = round(time.time())
    params = {**params, "wts": curr_time}
    params = dict(sorted(params.items()))
    # 过滤 urlencode 会转义的字符
    params = {
        k: "".join(filter(lambda c: c not in "!'()*", str(v)))
        for k, v in params.items()
    }
    query = urlencode(params)
    wbi_sign = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    params["w_rid"] = wbi_sign
    return params


def clean_html_title(title: Any) -> str:
    """去掉搜索结果标题中的 <em> 等 HTML 标签。"""
    if not title:
        return ""
    return _HTML_TAG_RE.sub("", str(title)).strip()


def parse_play_count(play_text: Any) -> int:
    """将播放量文本（"1.2万"、"3亿" 或数字）转换为整数。"""
    if not play_text:
        return 0
    if isinstance(play_text, (int, float)):
        return int(play_text)

    play_text = str(play_text).strip()
    unit_multiplier = 1
    if "万" in play_text:
        play_text = play_text.replace("万", "")
        unit_multiplier = 10000
    elif "亿" in play_text:
        play_text = play_text.replace("亿", "")
        unit_multiplier = 100000000

    try:
        numbers = re.findall(r"\d+\.?\d*", play_text)
        if numbers:
            return int(float(numbers[0]) * unit_multiplier)
        return 0
    except (ValueError, TypeError):
        return 0


def parse_duration(duration_text: Any) -> int:
    """将时长文本（"3:45"、"1:20:30" 或秒数）转换为秒数。"""
    if not duration_text:
        return 0
    duration_text = str(duration_text).strip()
    try:
        if ":" in duration_text:
            parts = duration_text.split(":")
            if len(parts) == 2:  # MM:SS
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:  # HH:MM:SS
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return int(duration_text)
    except (ValueError, TypeError):
        return 0


class BilibiliClient:
    """封装 B站搜索与播放地址 API。

    Args:
        session: 共享的 aiohttp.ClientSession。
        semaphore: 可选的并发信号量；为 None 时内部创建。
    """

    def __init__(self, session, semaphore: Optional[asyncio.Semaphore] = None):
        self.session = session
        self.semaphore = semaphore or asyncio.Semaphore(5)
        self._wbi_keys: Optional[Tuple[str, str]] = None
        self._wbi_keys_fetched_at: float = 0.0
        self._wbi_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # cookies / wbi 密钥
    # ------------------------------------------------------------------

    async def init_cookies(self) -> None:
        """初始化 buvid3/buvid4 等 cookies，降低 412 风控概率。"""
        try:
            async with self.semaphore:
                async with self.session.get(
                    "https://api.bilibili.com/x/frontend/finger/spi"
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"获取B站cookies请求失败: {resp.status}")
                        return
                    data = await resp.json()
            if data.get("code") != 0:
                logger.warning(f"获取B站cookies失败: {data}")
                return
            b3 = (data.get("data") or {}).get("b_3", "")
            b4 = (data.get("data") or {}).get("b_4", "")
            if b3:
                self.session.cookie_jar.update_cookies({"buvid3": b3})
            if b4:
                self.session.cookie_jar.update_cookies({"buvid4": b4})
            logger.info("B站cookies初始化成功")
        except Exception as e:
            logger.warning(f"初始化B站cookies异常: {e}")

    async def _get_wbi_keys(self) -> Optional[Tuple[str, str]]:
        """获取 wbi 签名密钥（img_key, sub_key），缓存 1 小时。"""
        now = time.time()
        if self._wbi_keys and now - self._wbi_keys_fetched_at < 3600:
            return self._wbi_keys

        async with self._wbi_lock:
            if self._wbi_keys and time.time() - self._wbi_keys_fetched_at < 3600:
                return self._wbi_keys
            try:
                async with self.semaphore:
                    async with self.session.get(
                        "https://api.bilibili.com/x/web-interface/nav"
                    ) as resp:
                        if resp.status != 200:
                            logger.warning(f"获取wbi密钥请求失败: {resp.status}")
                            return None
                        data = await resp.json()
                wbi_img = (data.get("data") or {}).get("wbi_img") or {}
                img_url = wbi_img.get("img_url", "")
                sub_url = wbi_img.get("sub_url", "")
                if not img_url or not sub_url:
                    logger.warning("wbi_img 数据缺失，无法获取签名密钥")
                    return None
                img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
                sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
                if len(img_key) != 32 or len(sub_key) != 32:
                    logger.warning(f"wbi 密钥格式异常: img={len(img_key)} sub={len(sub_key)}")
                    return None
                self._wbi_keys = (img_key, sub_key)
                self._wbi_keys_fetched_at = time.time()
                return self._wbi_keys
            except Exception as e:
                logger.warning(f"获取wbi密钥异常: {e}")
                return None

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    async def search_page(self, keyword: str, page: int = 1) -> List[Dict[str, Any]]:
        """搜索一页视频，返回原始条目列表（title 已去除 HTML 标签）。

        优先使用带 wbi 签名的 wbi/search/type 接口；失败时回退到
        search/all/v2。接口失败返回 []。
        """
        items = await self._wbi_search_page(keyword, page)
        if items is None:
            items = await self._legacy_search_page(keyword, page)
        return items or []

    async def _wbi_search_page(
        self, keyword: str, page: int
    ) -> Optional[List[Dict[str, Any]]]:
        """带 wbi 签名的搜索。失败返回 None（表示需要回退）。"""
        keys = await self._get_wbi_keys()
        if not keys:
            logger.warning("wbi 密钥不可用，回退到无签名搜索接口")
            return None
        try:
            params = enc_wbi(
                {
                    "keyword": keyword,
                    "page": page,
                    "search_type": "video",
                },
                keys[0],
                keys[1],
            )
            api_url = "https://api.bilibili.com/x/web-interface/wbi/search/type?" + urlencode(params)
            async with self.semaphore:
                async with self.session.get(api_url) as response:
                    if response.status != 200:
                        logger.warning(f"wbi搜索请求失败: HTTP {response.status}")
                        return None
                    data = await response.json()
            if data.get("code") == -412:
                logger.warning("wbi搜索触发风控(-412)，回退到无签名搜索接口")
                return None
            if data.get("code") != 0:
                logger.warning(f"wbi搜索失败: {data.get('message', '未知错误')}")
                return None
            result = (data.get("data") or {}).get("result") or []
            return self._normalize_search_items(result, keyword)
        except asyncio.TimeoutError:
            logger.warning(f"wbi搜索第{page}页超时")
            return None
        except Exception as e:
            logger.error(f"wbi搜索第{page}页出错: {e}")
            return None

    async def _legacy_search_page(
        self, keyword: str, page: int
    ) -> Optional[List[Dict[str, Any]]]:
        """无签名回退接口 search/all/v2。"""
        try:
            api_url = (
                "https://api.bilibili.com/x/web-interface/search/all/v2"
                f"?keyword={quote(keyword)}&page={page}"
            )
            async with self.semaphore:
                async with self.session.get(api_url) as response:
                    if response.status != 200:
                        logger.warning(f"回退搜索请求失败: HTTP {response.status}")
                        return None
                    data = await response.json()
            if data.get("code") == -412:
                logger.warning("回退搜索也触发风控(-412)，搜索暂时不可用")
                return None
            if data.get("code") != 0:
                logger.warning(f"回退搜索失败: {data.get('message', '未知错误')}")
                return None
            video_results = None
            for item in (data.get("data") or {}).get("result", []):
                if item.get("result_type") == "video":
                    video_results = item.get("data", [])
                    break
            if video_results is None:
                return []
            return self._normalize_search_items(video_results, keyword)
        except asyncio.TimeoutError:
            logger.warning(f"回退搜索第{page}页超时")
            return None
        except Exception as e:
            logger.error(f"回退搜索第{page}页出错: {e}")
            return None

    @staticmethod
    def _normalize_search_items(
        raw_items: List[Dict[str, Any]], keyword: str
    ) -> List[Dict[str, Any]]:
        """将搜索接口返回的条目统一结构，去掉 HTML 标签。"""
        items = []
        for video in raw_items:
            try:
                title = clean_html_title(video.get("title", ""))
                bvid = video.get("bvid", "")
                if not title or not bvid:
                    continue
                duration_text = video.get("duration", "")
                items.append(
                    {
                        "title": title,
                        "bvid": bvid,
                        "url": f"https://www.bilibili.com/video/{bvid}",
                        "play": video.get("play", 0),
                        "duration": duration_text,
                        "duration_seconds": parse_duration(duration_text),
                        "author": video.get("author", ""),
                        "search_keyword": keyword,
                    }
                )
            except Exception as e:
                logger.error(f"解析视频信息失败: {e}")
                continue
        return items

    # ------------------------------------------------------------------
    # 播放地址
    # ------------------------------------------------------------------

    async def get_video_urls(
        self, bvid: str, quality: int = 64
    ) -> Tuple[Optional[str], Optional[str], Optional[int]]:
        """获取视频/音频流地址，返回 (video_url, audio_url, video_codecid)。

        video_codecid: 7=H.264, 12=HEVC(H.265), 13=AV1；用于决定是否转码。
        只选择 H.264(优先) 且清晰度 <= quality 的流，避免大会员专属流导致 403。
        quality 对应 B站 DASH id：16=360P, 32=480P, 64=720P, 80=1080P。
        """
        if not self.session:
            logger.error("HTTP会话未初始化")
            return None, None, None
        try:
            # 第1步：通过 view 接口获取 cid
            view_api = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            async with self.semaphore:
                async with self.session.get(view_api) as response:
                    if response.status != 200:
                        logger.error(f"获取视频信息API失败: {response.status}")
                        return None, None, None
                    view_data = await response.json()
            if view_data.get("code") != 0:
                logger.error(
                    f"获取视频信息失败: {view_data.get('message', '未知错误')}"
                )
                return None, None, None

            cid = (view_data.get("data") or {}).get("cid")
            if not cid:
                pages = (view_data.get("data") or {}).get("pages", [])
                if pages:
                    cid = pages[0].get("cid")
            if not cid:
                logger.error(f"未找到视频cid: {bvid}")
                return None, None, None

            # 第2步：获取 DASH 播放地址（分离的视频/音频流）
            playurl_api = (
                f"https://api.bilibili.com/x/player/playurl?bvid={bvid}"
                f"&cid={cid}&qn={quality}&fnval=16&fourk=0"
            )
            async with self.semaphore:
                async with self.session.get(playurl_api) as response:
                    if response.status != 200:
                        logger.error(f"获取播放地址API失败: {response.status}")
                        return None, None, None
                    playurl_data = await response.json()
            if playurl_data.get("code") != 0:
                logger.error(
                    f"获取播放地址失败: {playurl_data.get('message', '未知错误')}"
                )
                return None, None, None

            dash = (playurl_data.get("data") or {}).get("dash")
            if not dash:
                logger.error(f"未找到DASH数据: {bvid}")
                return None, None, None

            video_qualities = dash.get("video", [])
            audio_qualities = dash.get("audio", [])
            if not video_qualities:
                logger.error(f"没有找到视频流: {bvid}")
                return None, None, None
            if not audio_qualities:
                logger.error(f"没有找到音频流: {bvid}")
                return None, None, None

            best_video = self._pick_best_video_stream(video_qualities, quality)
            best_audio = max(audio_qualities, key=lambda x: x.get("bandwidth", 0))
            if not best_video:
                logger.error(f"没有可用的视频流: {bvid}")
                return None, None, None

            video_url = best_video.get("baseUrl") or best_video.get("base_url")
            audio_url = best_audio.get("baseUrl") or best_audio.get("base_url")
            codecid = best_video.get("codecid")
            if not video_url or not audio_url:
                logger.error(f"视频或音频URL为空: {bvid}")
                return None, None, None

            logger.info(f"成功获取视频流地址: {bvid} (codecid={codecid})")
            return video_url, audio_url, codecid

        except Exception as e:
            logger.error(f"获取视频URL失败 {bvid}: {e}")
            return None, None, None

    @staticmethod
    def _pick_best_video_stream(
        streams: List[Dict[str, Any]], quality: int = 64
    ) -> Optional[Dict[str, Any]]:
        """优先选择 H.264(codecid=7) 且清晰度 id<=quality 的流。

        回退顺序：H.264+限定画质 -> H.264 任意画质 -> 任意编码+限定画质 ->
        任意编码；同池内按带宽取最大。
        """
        pools = [
            [s for s in streams if s.get("codecid") == 7 and 0 < s.get("id", 0) <= quality],
            [s for s in streams if s.get("codecid") == 7],
            [s for s in streams if 0 < s.get("id", 0) <= quality],
            streams,
        ]
        for pool in pools:
            if pool:
                return max(pool, key=lambda s: s.get("bandwidth", 0))
        return None
