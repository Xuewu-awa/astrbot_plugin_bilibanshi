"""群推送策略：白名单/黑名单模式、群过滤、名单管理。

不依赖 astrbot 框架（config 仅要求是 dict 且提供 save_config()），
便于单元测试。白名单/黑名单以 AstrBot 配置为唯一数据源；
旧版本遗留的 access_control_state.json 会在首次启动时一次性迁移。
"""
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def normalize_group_id(group_id: Any) -> str:
    """规范化群号：兼容纯数字、带前缀/后缀的字符串。"""
    if group_id is None:
        return ""
    normalized = str(group_id).strip()
    if not normalized:
        return ""
    digits = re.findall(r"\d+", normalized)
    if len(digits) == 1:
        return digits[0]
    return normalized


def normalize_group_list(group_list: Any) -> List[str]:
    """规范化群号列表并去重。"""
    if not isinstance(group_list, list):
        return []
    normalized_groups = []
    for group_id in group_list:
        normalized_group_id = normalize_group_id(group_id)
        if normalized_group_id and normalized_group_id not in normalized_groups:
            normalized_groups.append(normalized_group_id)
    return normalized_groups


class GroupPolicy:
    """封装白名单/黑名单模式的判断与修改。

    Args:
        config: AstrBot 插件配置（dict 子类），白名单/黑名单存储于此。
        legacy_state_path: 旧版本 access_control_state.json 路径（可为 None）。
    """

    def __init__(self, config, legacy_state_path: Optional[Path] = None) -> None:
        self.config = config
        self.legacy_state_path = Path(legacy_state_path) if legacy_state_path else None

    # ------------------------------------------------------------------
    # 旧版本数据迁移
    # ------------------------------------------------------------------

    def migrate_legacy_state(self) -> bool:
        """将旧版 access_control_state.json 一次性迁移到配置。

        迁移条件（避免覆盖用户当前设置）：
        - config 中白名单为空且本地文件存在非空名单；或
        - config 中没有 use_whitelist_mode 键（旧 schema 产物）。
        返回是否发生了迁移。
        """
        if not self.legacy_state_path or not self.legacy_state_path.exists():
            return False
        try:
            with open(self.legacy_state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return False

            local_whitelist = normalize_group_list(data.get("whitelist_groups", []))
            local_blacklist = normalize_group_list(data.get("blacklist_groups", []))
            local_mode = bool(data.get("use_whitelist_mode", False))

            current_whitelist = self.group_list("whitelist_groups")
            needs_mode = "use_whitelist_mode" not in self.config
            needs_whitelist = not current_whitelist and bool(local_whitelist)

            if not (needs_mode or needs_whitelist):
                return False

            if needs_mode:
                self.config["use_whitelist_mode"] = local_mode
            if needs_whitelist:
                self.config["whitelist_groups"] = local_whitelist
            if local_blacklist and not self.group_list("blacklist_groups"):
                self.config["blacklist_groups"] = local_blacklist

            self.config.save_config()
            logger.info("已从旧版 access_control_state.json 迁移群权限配置")
            return True
        except Exception as e:
            logger.error(f"迁移旧版群权限配置失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def is_whitelist_mode(self) -> bool:
        return bool(self.config.get("use_whitelist_mode", False))

    def mode_name(self) -> str:
        return "白名单模式" if self.is_whitelist_mode() else "黑名单模式"

    def group_list(self, config_key: str) -> Set[str]:
        """获取并规范化某名单配置（whitelist_groups / blacklist_groups）。"""
        groups = self.config.get(config_key, [])
        if not isinstance(groups, list):
            return set()
        return {g for g in (normalize_group_id(x) for x in groups) if g}

    def should_send(self, group_id: Any) -> bool:
        """判断群在当前模式下是否允许推送。"""
        normalized = normalize_group_id(group_id)
        if not normalized:
            return False
        if self.is_whitelist_mode():
            return normalized in self.group_list("whitelist_groups")
        return normalized not in self.group_list("blacklist_groups")

    def allowed_bound_groups(self, bound_groups: Dict[str, str]) -> Dict[str, str]:
        """从已绑定群中筛出当前模式允许推送的 {group_id: umo}。"""
        return {
            normalize_group_id(gid): umo
            for gid, umo in bound_groups.items()
            if self.should_send(gid)
        }

    def block_reason(self, group_id: Any) -> str:
        """群被拦截时的原因文案。"""
        if self.is_whitelist_mode():
            return f"当前为白名单模式，群 {group_id} 不在白名单中"
        return f"当前为黑名单模式，群 {group_id} 在黑名单中"

    # ------------------------------------------------------------------
    # 修改
    # ------------------------------------------------------------------

    def update_list(self, config_key: str, action: str, group_id: Any, list_name: str) -> str:
        """添加/移除群名单并保存配置，返回结果文案。"""
        normalized_group_id = normalize_group_id(group_id)
        if not normalized_group_id:
            return "群号无效"

        group_list = normalize_group_list(self.config.get(config_key, []))

        if action == "add":
            if normalized_group_id not in group_list:
                group_list.append(normalized_group_id)
                self.config[config_key] = group_list
                self.config.save_config()
                return f"✅ 已添加{list_name}群: {normalized_group_id}"
            return f"该群已在{list_name}中"

        if action == "remove":
            if normalized_group_id in group_list:
                group_list.remove(normalized_group_id)
                self.config[config_key] = group_list
                self.config.save_config()
                return f"✅ 已移除{list_name}群: {normalized_group_id}"
            return f"该群不在{list_name}中"

        return "未知操作，请使用 add 或 remove"
