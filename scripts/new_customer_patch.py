#!/usr/bin/env python3
"""客户单文件补丁步骤生成器（只读 git HEAD，不读工作树）。

用法示例：
    python3 scripts/new_customer_patch.py \
        --files deploy/windows-full/Common.ps1 \
        --out /tmp/patch-steps.txt

说明：
- 每个 --files 必须是 git HEAD 已提交的路径，且工作树相对 HEAD 干净。
- 哈希/size 全部用 ``git show HEAD:<path>`` 的字节实算，不读工作树。
- 清单里的 path 口径与 scripts/package_windows_full_bundle.py 的
  manifest_bytes 一致：application 文件为 ``app/<repo相对路径>``。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

# 客户机上的固定安装根（与 Test-FullBundle.ps1 默认 BundleRoot 一致）。
INSTALL_ROOT = r"D:\truepeoplesearch"
MANIFEST_WINDOWS_PATH = INSTALL_ROOT + r"\bundle-manifest.json"
TEST_SCRIPT_WINDOWS_PATH = (
    INSTALL_ROOT + r"\app\deploy\windows-full\Test-FullBundle.ps1"
)
INIT_SCRIPT_WINDOWS_PATH = (
    INSTALL_ROOT + r"\app\deploy\windows-full\Initialize-Runtime.ps1"
)


class PatchError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return env


def _git_text(repo_root: Path, args: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo_root), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
        env=_git_env(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise PatchError(detail or f"git 命令失败：git {' '.join(args)}")
    return completed.stdout.strip()


def _git_bytes(repo_root: Path, spec: str) -> bytes:
    completed = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo_root),
         "show", spec],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env=_git_env(),
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise PatchError(detail or f"HEAD 里没有这个文件：{spec}")
    return completed.stdout


def normalize_repo_path(raw: str) -> str:
    """把用户输入归一化为 repo 相对 POSIX 路径。"""
    text = raw.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    text = text.strip()
    if not text:
        raise PatchError(f"空文件路径：{raw!r}")
    if text.startswith("/") or text.startswith("../") or "/../" in text or text == "..":
        raise PatchError(f"不允许的路径（必须在仓库内）：{raw!r}")
    if len(text) >= 2 and text[1] == ":":
        raise PatchError(f"不允许的路径（必须是仓库相对路径）：{raw!r}")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise PatchError(f"不允许的路径：{raw!r}")
    return "/".join(parts)


def ensure_committed_and_clean(repo_root: Path, posix_path: str) -> None:
    """文件必须在 HEAD 已提交，且工作树相对 HEAD 干净。"""
    try:
        _git_bytes(repo_root, f"HEAD:{posix_path}")
    except PatchError as exc:
        raise PatchError(f"{posix_path} 不在 git HEAD 里（先提交再生成补丁）：{exc}") from exc
    status = _git_text(
        repo_root,
        ["status", "--porcelain=v1", "--untracked-files=all", "--", posix_path],
    )
    if status:
        raise PatchError(
            f"{posix_path} 工作树不干净（相对 HEAD 有改动/新增/未跟踪），"
            "先提交或还原再生成补丁：\n" + status
        )
    diff = _git_text(repo_root, ["diff", "HEAD", "--", posix_path])
    if diff:
        raise PatchError(f"{posix_path} 与 HEAD 有差异，先提交再生成补丁。")


def build_lines(entries: list[dict[str, object]], commit: str, short: str) -> str:
    out: list[str] = []
    out.append("【给工程师核对区（发客户之前先自己对一遍）】")
    out.append(f"commit: {commit}（短哈希 {short}）")
    out.append(f"共 {len(entries)} 个文件，哈希/size 全部来自 git show HEAD:<path> 实算：")
    for entry in entries:
        out.append(
            f"- path={entry['manifest_path']} "
            f"sha256={entry['sha256']} size={entry['size']}"
        )
    out.append("")
    out.append("核对方法：客户跑完第 3 步后，把他截图里的「Source commit / Verified files」")
    out.append("和上面这一行 commit 对一下，个数对一下即可。")
    out.append("")
    out.append("=" * 60)
    out.append("")
    out.append("【发给客户的步骤】直接全文转发下面虚线之后的内容")
    out.append("")
    out.append("-" * 60)
    out.append("")
    out.append("【开始之前】只做一件事")
    out.append("")
    out.append("1. 在电脑上点右键「以管理员身份运行」打开 PowerShell（蓝色窗口）。")
    out.append("   后面每一大步都是一整段命令：整段选中、复制、粘贴到蓝色窗口里、按回车。")
    out.append("   一次只跑一大步，看到「应该看到什么」再跑下一步。")
    out.append("")
    out.append("【第 0 步】把我发你的新文件复制到位（共 "
               f"{len(entries)} 个）")
    out.append("")
    for index, entry in enumerate(entries, start=1):
        out.append(f"{index}. 把我发的这个文件：{entry['repo_path']}")
        out.append(f"   复制到：{entry['windows_path']}")
        if entry.get("is_new"):
            out.append("   这是新文件，直接放进去就行。")
        else:
            out.append("   如果问「是否替换」，点「替换」。")
    out.append("")
    out.append("应该看到什么：每个位置都只有一个新文件，没有多出来的 .new 文件。")
    out.append("")
    out.append("【第 1 步】验文件哈希（确认复制过去的文件是对的）")
    out.append("")
    out.append("把下面这一整段复制进蓝色窗口，按回车：")
    out.append("```powershell")
    for entry in entries:
        out.append(f"$expected_{entry['var']} = '{entry['sha256_upper']}'")
        out.append(
            f"$actual_{entry['var']} = "
            f"(Get-FileHash -LiteralPath '{entry['windows_path']}' "
            "-Algorithm SHA256).Hash"
        )
        out.append(f"$actual_{entry['var']} -eq $expected_{entry['var']}")
    out.append("```")
    out.append("")
    out.append("应该看到什么：每个文件都打印一行 True（有几个文件就看到几个 True）。")
    out.append("如果有一个不是 True：停下、截图发我，不要往下跑。")
    out.append("")
    out.append("【第 2 步】更新清单 bundle-manifest.json（告诉电脑新文件的指纹）")
    out.append("")
    out.append("第 1 步全是 True 了，再把下面这一整段复制进蓝色窗口，按回车：")
    out.append("```powershell")
    out.append(f"$manifestPath = '{MANIFEST_WINDOWS_PATH}'")
    out.append(
        "$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 "
        "| ConvertFrom-Json"
    )
    for entry in entries:
        out.append(
            f"$entry_{entry['var']} = @($manifest.files "
            f"| Where-Object {{ $_.path -eq '{entry['manifest_path']}' }})"
        )
        if entry.get("is_new"):
            out.append(f"if ($entry_{entry['var']}.Count -eq 0) {{")
            out.append(
                f"  $manifest.files += [pscustomobject]@{{category='application'; "
                f"path='{entry['manifest_path']}'; "
                f"sha256='{entry['sha256_lower']}'; size={entry['size']}}}"
            )
            out.append(
                f"  $entry_{entry['var']} = @($manifest.files "
                f"| Where-Object {{ $_.path -eq '{entry['manifest_path']}' }})"
            )
            out.append("} else {")
            out.append(f"  $entry_{entry['var']}[0].sha256 = '{entry['sha256_lower']}'")
            out.append(f"  $entry_{entry['var']}[0].size = {entry['size']}")
            out.append("}")
        else:
            out.append(
                f"if ($entry_{entry['var']}.Count -ne 1) "
                f"{{ throw 'manifest entry not found: {entry['manifest_path']}' }}"
            )
            out.append(f"$entry_{entry['var']}[0].sha256 = '{entry['sha256_lower']}'")
            out.append(f"$entry_{entry['var']}[0].size = {entry['size']}")
    out.append(f"$manifest.commit = '{commit}'")
    out.append(
        "[IO.File]::WriteAllText($manifestPath, "
        "($manifest | ConvertTo-Json -Depth 10), "
        "(New-Object Text.UTF8Encoding($false)))"
    )
    for entry in entries:
        out.append(
            f"Write-Host \"{entry['manifest_path']} sha256=$("
            f"$entry_{entry['var']}[0].sha256) size=$("
            f"$entry_{entry['var']}[0].size)\""
        )
    out.append('Write-Host "commit=$($manifest.commit)"')
    out.append("```")
    out.append("")
    out.append("应该看到什么：打印出来的 sha256 / size / commit 和我告诉你的一致，")
    for entry in entries:
        out.append(f"  {entry['manifest_path']} sha256={entry['sha256_lower']} size={entry['size']}")
    out.append(f"  commit={commit}")
    out.append("对不上就停下、截图发我。注意：这里不要用记事本手工改清单，")
    out.append("必须跑上面这段命令（它是按 path 精确改的，不会碰坏别的地方）。")
    out.append("")
    out.append("【第 3 步】重验 + 初始化（确认整包是对的）")
    out.append("")
    out.append("先跑校验，把这一整段复制进蓝色窗口，按回车：")
    out.append("```powershell")
    # 已初始化的客户机顶层含 backups/config/data/logs/runtime，必须加
    # -AllowRuntimeState，否则 pristine 顶层检查必失败；包内 95 文件哈希照常全验。
    out.append(
        f"powershell -ExecutionPolicy Bypass -File {TEST_SCRIPT_WINDOWS_PATH} "
        "-AllowRuntimeState"
    )
    out.append("```")
    out.append("")
    out.append("应该看到什么：最后三行里有 Full Windows bundle verification passed.，")
    out.append("并且 Source commit 是上面的 commit，Verified files 个数正常。")
    out.append("报错就停下、把红字截图发我。")
    out.append("")
    out.append("校验通过了，再把这一整段复制进蓝色窗口，按回车：")
    out.append("```powershell")
    out.append(
        f"powershell -ExecutionPolicy Bypass -File {INIT_SCRIPT_WINDOWS_PATH}"
    )
    out.append("```")
    out.append("")
    out.append("应该看到什么：看到 D-drive runtime layout initialized. 就完成了，截图发我。")
    out.append("")
    return "\n".join(out) + "\n"


def _arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="客户单文件补丁步骤生成器：只读 git HEAD 实算哈希，输出小白可照做的 txt。"
    )
    parser.add_argument(
        "--files",
        action="append",
        default=[],
        metavar="REPO_PATH",
        help="已在客户清单里的文件，可重复传，如 --files deploy/windows-full/Common.ps1",
    )
    parser.add_argument(
        "--new-files",
        action="append",
        default=[],
        metavar="REPO_PATH",
        help="客户清单里还没有的新文件（自动新增条目），如 --new-files deploy/windows-full/Test-CustomerEnv.ps1",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="输出 txt 路径，如 /tmp/patch-steps.txt",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=REPO_ROOT,
        help="仓库根（默认取脚本所在仓库）",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _arguments(argv)
    repo_root = args.repo_root.resolve()
    if not (repo_root / ".git").exists() and not (repo_root / "scripts").exists():
        print(f"仓库根看起来不对：{repo_root}", file=sys.stderr)
        return 2
    try:
        if _git_text(repo_root, ["rev-parse", "--is-inside-work-tree"]) != "true":
            raise PatchError("不在 git 工作树里")
        commit = _git_text(repo_root, ["rev-parse", "HEAD"]).lower()
        if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise PatchError(f"拿不到 40 位 HEAD commit：{commit!r}")
        short = _git_text(repo_root, ["rev-parse", "--short", "HEAD"])

        seen: set[str] = set()
        entries: list[dict[str, object]] = []
        tasks: list[tuple[str, bool]] = [(raw, False) for raw in (args.files or [])]
        tasks += [(raw, True) for raw in (args.new_files or [])]
        if not tasks:
            raise PatchError("至少传一个 --files 或 --new-files")
        for raw, is_new in tasks:
            posix_path = normalize_repo_path(raw)
            if posix_path in seen:
                raise PatchError(f"重复的文件：{posix_path}")
            seen.add(posix_path)
            ensure_committed_and_clean(repo_root, posix_path)
            blob = _git_bytes(repo_root, f"HEAD:{posix_path}")
            sha_lower = hashlib.sha256(blob).hexdigest()
            # manifest_bytes 口径：application 文件 archive_path = "app/" + 相对路径
            manifest_path = "app/" + posix_path
            windows_path = INSTALL_ROOT + "\\" + manifest_path.replace("/", "\\")
            var = f"f{len(entries)}"
            entries.append(
                {
                    "repo_path": posix_path,
                    "manifest_path": manifest_path,
                    "windows_path": windows_path,
                    "sha256": sha_lower,
                    "sha256_lower": sha_lower,
                    "sha256_upper": sha_lower.upper(),
                    "size": len(blob),
                    "var": var,
                    "is_new": is_new,
                }
            )

        text = build_lines(entries, commit, short)
        out_path: Path = args.out
        if out_path.parent and str(out_path.parent) not in ("", "."):
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
    except PatchError as exc:
        print(f"生成补丁步骤失败：{exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"生成补丁步骤失败：{exc}", file=sys.stderr)
        return 2
    print(f"wrote: {args.out}")
    print(f"commit: {commit} ({short})")
    for entry in entries:
        print(
            f"{entry['manifest_path']} sha256={entry['sha256']} size={entry['size']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
