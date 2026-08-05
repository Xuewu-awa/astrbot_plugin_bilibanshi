"""防回归：强制求值所有模块的注解。

Python 3.14 惰性注解会掩盖 "NameError: name 'X' is not defined" 类错误
（注解里引用了未导入的名字），但在 Python 3.10-3.13 下插件直接加载失败。
get_type_hints() 会强制求值注解，能提前抓住这类问题。
"""
import inspect
import os
import sys
import unittest
from typing import get_type_hints

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bilibili_client
import downloader
import group_policy
import state_store


class TestAnnotationsResolve(unittest.TestCase):
    def test_all_module_annotations(self):
        """模块级函数与所有类方法的注解必须能成功求值。"""
        modules = [bilibili_client, downloader, group_policy, state_store]
        checked = 0
        for mod in modules:
            # 模块级函数
            for name, obj in inspect.getmembers(mod, inspect.isfunction):
                if obj.__module__ == mod.__name__:
                    get_type_hints(obj)
                    checked += 1
            # 类方法（含静态方法）
            for name, cls in inspect.getmembers(mod, inspect.isclass):
                if cls.__module__ != mod.__name__:
                    continue
                for mname, mobj in inspect.getmembers(cls, inspect.isfunction):
                    if mobj.__module__ == mod.__name__:
                        get_type_hints(mobj)
                        checked += 1
        self.assertGreater(checked, 10, "注解检查数量异常，可能未遍历到")


if __name__ == "__main__":
    unittest.main()
