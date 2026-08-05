"""核心逻辑单元测试（不依赖 astrbot / 网络）。

运行: python -m unittest discover -s tests -v
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bilibili_client import (
    BilibiliClient,
    clean_html_title,
    enc_wbi,
    get_mixin_key,
    parse_duration,
    parse_play_count,
)
from downloader import clean_filename
from group_policy import GroupPolicy, normalize_group_id, normalize_group_list
from state_store import StateStore


class MockConfig(dict):
    """模拟 AstrBotConfig 的最小实现。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_called = 0

    def save_config(self):
        self.save_called += 1


class TestWbiSign(unittest.TestCase):
    def test_mixin_key_table_valid(self):
        """打乱表必须是 64 个不重复索引（0-63）。"""
        from bilibili_client import MIXIN_KEY_ENC_TAB

        self.assertEqual(len(MIXIN_KEY_ENC_TAB), 64)
        self.assertEqual(sorted(MIXIN_KEY_ENC_TAB), list(range(64)))

    def test_mixin_key(self):
        key = get_mixin_key("7cd084941338484aae1ad9425b84077c" * 2)
        self.assertEqual(len(key), 32)

    def test_enc_wbi(self):
        img_key = "7cd084941338484aae1ad9425b84077c"
        sub_key = "4932caff0ff746eab6f01bf08b70ac45"
        params = enc_wbi({"keyword": "测试", "page": 1}, img_key, sub_key)
        self.assertIn("wts", params)
        self.assertIn("w_rid", params)
        self.assertEqual(len(params["w_rid"]), 32)
        # 确定性：相同输入产生相同签名
        params2 = enc_wbi({"keyword": "测试", "page": 1}, img_key, sub_key)
        self.assertEqual(params["w_rid"], params2["w_rid"])


class TestParse(unittest.TestCase):
    def test_parse_play_count(self):
        self.assertEqual(parse_play_count("1.2万"), 12000)
        self.assertEqual(parse_play_count("3亿"), 300000000)
        self.assertEqual(parse_play_count("12345"), 12345)
        self.assertEqual(parse_play_count(12345), 12345)
        self.assertEqual(parse_play_count(""), 0)
        self.assertEqual(parse_play_count(None), 0)
        self.assertEqual(parse_play_count("abc"), 0)

    def test_parse_duration(self):
        self.assertEqual(parse_duration("3:45"), 225)
        self.assertEqual(parse_duration("1:20:30"), 4830)
        self.assertEqual(parse_duration("600"), 600)
        self.assertEqual(parse_duration(600), 600)
        self.assertEqual(parse_duration(""), 0)
        self.assertEqual(parse_duration("abc"), 0)

    def test_clean_html_title(self):
        self.assertEqual(clean_html_title("<em class=\"keyword\">搬石</em>测试"), "搬石测试")
        self.assertEqual(clean_html_title("普通标题"), "普通标题")
        self.assertEqual(clean_html_title(""), "")
        self.assertEqual(clean_html_title(None), "")

    def test_clean_filename(self):
        # 书名号提取会替换整个标题（原行为）
        self.assertEqual(clean_filename("《测试视频》第1集"), "测试视频")
        self.assertEqual(clean_filename("【搬石】《测试视频》"), "测试视频")
        # 控制字符被移除
        self.assertEqual(clean_filename("标题\n换行"), "标题换行")
        self.assertEqual(clean_filename("a<b>c:d\"e/f\\g|h?i*j"), "abcdefghij")
        self.assertEqual(clean_filename(""), "untitled")
        self.assertEqual(clean_filename("x" * 200), "x" * 100)
        self.assertEqual(clean_filename("   "), "untitled")


class TestGroupNormalize(unittest.TestCase):
    def test_normalize_group_id(self):
        self.assertEqual(normalize_group_id(123456), "123456")
        self.assertEqual(normalize_group_id("123456"), "123456")
        self.assertEqual(normalize_group_id("Group_123456"), "123456")
        self.assertEqual(normalize_group_id("123-456"), "123-456")  # 多段数字保留原样
        self.assertEqual(normalize_group_id(""), "")
        self.assertEqual(normalize_group_id(None), "")

    def test_normalize_group_list(self):
        self.assertEqual(normalize_group_list(["123", 456, "123", ""]), ["123", "456"])
        self.assertEqual(normalize_group_list("not-a-list"), [])
        self.assertEqual(normalize_group_list(None), [])


class TestGroupPolicy(unittest.TestCase):
    def test_blacklist_mode_default(self):
        config = MockConfig(
            {
                "use_whitelist_mode": False,
                "blacklist_groups": ["111"],
                "whitelist_groups": [],
            }
        )
        policy = GroupPolicy(config)
        self.assertFalse(policy.is_whitelist_mode())
        self.assertTrue(policy.should_send("222"))
        self.assertFalse(policy.should_send("111"))

    def test_whitelist_mode(self):
        config = MockConfig(
            {
                "use_whitelist_mode": True,
                "blacklist_groups": [],
                "whitelist_groups": ["111"],
            }
        )
        policy = GroupPolicy(config)
        self.assertTrue(policy.is_whitelist_mode())
        self.assertTrue(policy.should_send("111"))
        self.assertFalse(policy.should_send("222"))

    def test_allowed_bound_groups(self):
        config = MockConfig(
            {
                "use_whitelist_mode": True,
                "blacklist_groups": [],
                "whitelist_groups": ["111"],
            }
        )
        policy = GroupPolicy(config)
        bound = {"111": "umo1", "222": "umo2"}
        self.assertEqual(policy.allowed_bound_groups(bound), {"111": "umo1"})

    def test_update_list(self):
        config = MockConfig(
            {
                "use_whitelist_mode": True,
                "blacklist_groups": [],
                "whitelist_groups": [],
            }
        )
        policy = GroupPolicy(config)
        msg = policy.update_list("whitelist_groups", "add", "123", "白名单")
        self.assertIn("已添加", msg)
        self.assertEqual(config["whitelist_groups"], ["123"])
        self.assertEqual(config.save_called, 1)
        # 重复添加
        msg = policy.update_list("whitelist_groups", "add", "123", "白名单")
        self.assertIn("已在", msg)
        # 移除
        msg = policy.update_list("whitelist_groups", "remove", "123", "白名单")
        self.assertIn("已移除", msg)
        self.assertEqual(config["whitelist_groups"], [])
        # 未知操作
        msg = policy.update_list("whitelist_groups", "xxx", "123", "白名单")
        self.assertIn("未知操作", msg)

    def test_migrate_legacy_state(self):
        """config 无模式键时，迁移模式+名单。"""
        config = MockConfig({"blacklist_groups": [], "whitelist_groups": []})
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "access_control_state.json"
            legacy.write_text(
                json.dumps(
                    {
                        "use_whitelist_mode": True,
                        "whitelist_groups": ["999"],
                        "blacklist_groups": [],
                    }
                ),
                encoding="utf-8",
            )
            policy = GroupPolicy(config, legacy)
            migrated = policy.migrate_legacy_state()
            self.assertTrue(migrated)
            self.assertTrue(config["use_whitelist_mode"])
            self.assertEqual(config["whitelist_groups"], ["999"])

    def test_migrate_keeps_explicit_mode(self):
        """config 已明确模式时，迁移只补名单、不覆盖模式。"""
        config = MockConfig(
            {
                "use_whitelist_mode": False,
                "blacklist_groups": [],
                "whitelist_groups": [],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "access_control_state.json"
            legacy.write_text(
                json.dumps(
                    {
                        "use_whitelist_mode": True,
                        "whitelist_groups": ["999"],
                        "blacklist_groups": [],
                    }
                ),
                encoding="utf-8",
            )
            policy = GroupPolicy(config, legacy)
            migrated = policy.migrate_legacy_state()
            self.assertTrue(migrated)
            self.assertFalse(config["use_whitelist_mode"])  # 模式不被覆盖
            self.assertEqual(config["whitelist_groups"], ["999"])  # 名单被迁移

    def test_migrate_not_overwrite_existing(self):
        """config 中已有白名单时，迁移不应覆盖。"""
        config = MockConfig(
            {
                "use_whitelist_mode": True,
                "blacklist_groups": [],
                "whitelist_groups": ["111"],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / "access_control_state.json"
            legacy.write_text(
                json.dumps(
                    {
                        "use_whitelist_mode": False,
                        "whitelist_groups": ["999"],
                        "blacklist_groups": [],
                    }
                ),
                encoding="utf-8",
            )
            policy = GroupPolicy(config, legacy)
            migrated = policy.migrate_legacy_state()
            self.assertFalse(migrated)
            self.assertEqual(config["whitelist_groups"], ["111"])


class TestStateStore(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            data = {"a": 1, "b": ["x", "y"]}
            asyncio.run(store.save("test", data))
            self.assertEqual(store.load("test"), data)

    def test_load_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)
            self.assertEqual(store.load("nope", {"d": 1}), {"d": 1})

    def test_load_corrupted_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "bad.json").write_text("{not json", encoding="utf-8")
            store = StateStore(tmp)
            self.assertEqual(store.load("bad", "fallback"), "fallback")

    def test_concurrent_saves_no_corruption(self):
        """并发写 20 次，文件始终是合法 JSON。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(tmp)

            async def worker(i: int):
                await store.save("conc", {"i": i, "payload": "x" * 100})

            async def main():
                await asyncio.gather(*[worker(i) for i in range(20)])

            asyncio.run(main())
            data = store.load("conc")
            self.assertIn("i", data)


class TestDownloaderStaleFiles(unittest.TestCase):
    def test_cleanup_stale_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            download_dir = Path(tmp) / "downloads"
            download_dir.mkdir()
            (download_dir / "video_abc.m4s").write_bytes(b"x")
            (download_dir / "keep.mp4").write_bytes(b"x")
            (download_dir / "x.part").write_bytes(b"x")

            from downloader import Downloader

            d = Downloader(download_dir, session=None)
            removed = d.cleanup_stale_files()
            self.assertEqual(removed, 2)
            self.assertTrue((download_dir / "keep.mp4").exists())


class TestFfmpegCmd(unittest.TestCase):
    def test_h264_uses_copy(self):
        from downloader import build_ffmpeg_cmd

        cmd = build_ffmpeg_cmd("v.m4s", "a.m4s", "out.mp4", 7)
        self.assertIn("-c:v", cmd)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")

    def test_non_h264_transcodes(self):
        from downloader import build_ffmpeg_cmd

        cmd = build_ffmpeg_cmd("v.m4s", "a.m4s", "out.mp4", 12)  # HEVC
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertIn("yuv420p", cmd)
        self.assertIn("+faststart", cmd)

    def test_force_transcode_h264(self):
        """transcode=True 时 H.264 源也转码为 main profile。"""
        from downloader import build_ffmpeg_cmd

        cmd = build_ffmpeg_cmd("v.m4s", "a.m4s", "out.mp4", 7, transcode=True)
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        self.assertEqual(cmd[cmd.index("-profile:v") + 1], "main")
        self.assertEqual(cmd[cmd.index("-level") + 1], "4.0")

    def test_output_last(self):
        from downloader import build_ffmpeg_cmd

        cmd = build_ffmpeg_cmd("v.m4s", "a.m4s", "out.mp4", 7)
        self.assertEqual(cmd[-1], "out.mp4")
        self.assertEqual(cmd[0], "ffmpeg")


class TestPickStream(unittest.TestCase):
    def test_prefers_h264_within_quality(self):
        streams = [
            {"id": 64, "codecid": 12, "bandwidth": 2000},  # HEVC 720P
            {"id": 64, "codecid": 7, "bandwidth": 1500},  # H.264 720P
            {"id": 80, "codecid": 7, "bandwidth": 3000},  # H.264 1080P
        ]
        best = BilibiliClient._pick_best_video_stream(streams, quality=64)
        self.assertEqual(best["id"], 64)
        self.assertEqual(best["codecid"], 7)

    def test_fallback_when_no_h264(self):
        streams = [
            {"id": 64, "codecid": 12, "bandwidth": 2000},
            {"id": 80, "codecid": 12, "bandwidth": 3000},
        ]
        best = BilibiliClient._pick_best_video_stream(streams, quality=64)
        self.assertEqual(best["id"], 64)  # 限定画质内选最大带宽

    def test_empty(self):
        self.assertIsNone(BilibiliClient._pick_best_video_stream([], 64))


if __name__ == "__main__":
    unittest.main()
