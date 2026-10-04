#!/usr/bin/env python3
"""发版包依赖闭环：清单内脚本 import 的本地模块必须也在清单里（防漏文件）。
只认仓库里真实存在的文件；第三方/标准库忽略；动态 __import__ 字符串不管。"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 这些是工具脚本/测试/生成器，有意不进包，跳过对它们的断言（但它们仍可被验）。
ALLOWLIST_EXCEPTIONS = {
    "scripts/new_customer_patch.py",
    "scripts/test_windows_host.py",
}


def _local_module_path(top_name: str):
    for rel in (f"scripts/{top_name}.py", f"tools/{top_name}.py"):
        if os.path.isfile(os.path.join(REPO, rel)):
            return rel
    return None


def _imports_of(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add((a.name or "").split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.level or 0) == 0:
                out.add(node.module.split(".")[0])
    return out


class BundleClosureTests(unittest.TestCase):
    def test_no_missing_local_deps(self):
        from package_windows_full_bundle import APP_ALLOWLIST
        allowed = set(APP_ALLOWLIST) | ALLOWLIST_EXCEPTIONS
        missing = {}
        for rel in APP_ALLOWLIST:
            if not rel.endswith(".py"):
                continue
            full = os.path.join(REPO, rel)
            if not os.path.isfile(full):
                continue
            for top in sorted(_imports_of(full)):
                if not top:
                    continue
                dep = _local_module_path(top)
                if dep and dep not in allowed:
                    missing.setdefault(rel, []).append(dep)
        self.assertEqual(missing, {}, f"清单漏文件（被import但没打包）: {missing}")

    def test_known_runtime_deps_present(self):
        from package_windows_full_bundle import APP_ALLOWLIST
        for rel in (
            "scripts/phone_plan.py",
            "scripts/cloudbypass_v2.py",
            "deploy/windows-full/Repair-RuntimeBinding.ps1",
        ):
            self.assertIn(rel, APP_ALLOWLIST, rel)


if __name__ == "__main__":
    unittest.main(verbosity=2)
