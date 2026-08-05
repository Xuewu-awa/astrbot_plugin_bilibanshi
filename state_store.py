"""JSON 状态持久化：原子写入（临时文件 + os.replace）+ 并发写锁。

不依赖 astrbot 框架，便于单元测试。
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class StateStore:
    """管理 data 目录下的多个 JSON 状态文件。

    - load() 同步读取（供 __init__ 等非异步上下文使用）
    - save() 异步写入：asyncio.Lock 串行化并发写，临时文件 + os.replace 保证原子性
    """

    def __init__(self, data_dir) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()

    def _path(self, name: str) -> Path:
        return self.data_dir / f"{name}.json"

    def load(self, name: str, default: Any = None) -> Any:
        """同步读取 JSON 文件；不存在或损坏时返回 default。"""
        path = self._path(name)
        if not path.exists():
            return default
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取 {name}.json 失败: {e}")
            return default

    async def save(self, name: str, data: Any) -> bool:
        """异步原子写入 JSON 文件。成功返回 True。"""
        async with self._write_lock:
            path = self._path(name)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
                return True
            except Exception as e:
                logger.error(f"保存 {name}.json 失败: {e}")
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass
                return False
