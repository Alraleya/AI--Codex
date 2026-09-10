#!/usr/bin/env python3
"""Prepare and verify a local AI Manga Codex checkout without generating media."""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = PROJECT_ROOT / "frontend"
MIN_PYTHON = (3, 9)
MIN_NODE = (20, 9)


def version_tuple(value: str) -> Tuple[int, int]:
    match = re.search(r"(\d+)\.(\d+)", value)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def run(
    command: Sequence[str],
    *,
    cwd: Path = PROJECT_ROOT,
    capture: bool = False,
    timeout: int = 900,
) -> subprocess.CompletedProcess:
    printable = " ".join(command)
    print(f"\n→ {printable}")
    return subprocess.run(
        list(command),
        cwd=str(cwd),
        check=False,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )


def command_output(command: Sequence[str], timeout: int = 20) -> Tuple[int, str]:
    try:
        result = run(command, capture=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return result.returncode, output


def executable_from_env(name: str) -> Optional[str]:
    configured = os.environ.get(name, "").strip()
    if not configured:
        return None
    path = Path(configured).expanduser()
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def check_provider_status() -> None:
    print("\nProvider 就绪检查（只读，不生成媒体）")

    codex = shutil.which("codex")
    if codex is None:
        print("  [可选] 图片：未找到 Codex CLI；可按接入指南配置自定义图片 CLI。")
    else:
        version_code, version_text = command_output([codex, "--version"])
        login_code, login_text = command_output([codex, "login", "status"])
        features_code, features_text = command_output([codex, "features", "list"])
        image_feature = any(
            line.startswith("image_generation") and line.rstrip().endswith("true")
            for line in features_text.splitlines()
        )
        if version_code == 0 and login_code == 0 and features_code == 0 and image_feature:
            print(f"  [就绪] 图片：{version_text.splitlines()[0]}；{login_text.splitlines()[0]}；image_generation 已启用。")
        else:
            print("  [可选] 图片：Codex CLI 存在，但登录或 image_generation 尚未就绪。")

    image_cli = executable_from_env("JIMENG_IMAGE_CLI")
    if os.environ.get("JIMENG_IMAGE_CLI"):
        state = "就绪" if image_cli else "需修复"
        print(f"  [{state}] 自定义图片 CLI：{os.environ['JIMENG_IMAGE_CLI']}")

    video_cli = executable_from_env("JIMENG_VIDEO_CLI") or shutil.which("dreamina")
    if video_cli:
        print(f"  [就绪] 视频 CLI：{video_cli}（实际生成仍需当前 revision 人工确认）")
    else:
        print("  [可选] 视频：未配置 JIMENG_VIDEO_CLI，PATH 中也没有 dreamina。")

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        print(f"  [就绪] 视频校验：{ffprobe}")
    else:
        print("  [建议] 未找到 ffprobe；流程仍可运行，但视频校验会较弱。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一键配置并验证 AI漫剧-Codex 本地环境；不会调用媒体生成服务。"
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="只检查版本和 Provider，不安装依赖、不运行测试或构建。",
    )
    args = parser.parse_args()

    print("AI漫剧-Codex 环境配置")
    failures: List[str] = []

    python_version = sys.version_info[:2]
    if python_version < MIN_PYTHON:
        failures.append(f"需要 Python 3.9+，当前为 {sys.version.split()[0]}")
    else:
        print(f"[就绪] Python {sys.version.split()[0]}")

    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None:
        failures.append("未找到 Node.js 20.9+")
    else:
        node_code, node_text = command_output([node, "--version"])
        if node_code != 0 or version_tuple(node_text) < MIN_NODE:
            failures.append(f"需要 Node.js 20.9+，当前为 {node_text or '未知版本'}")
        else:
            print(f"[就绪] Node.js {node_text}")
    if npm is None:
        failures.append("未找到 npm")
    else:
        npm_code, npm_text = command_output([npm, "--version"])
        if npm_code != 0:
            failures.append("npm 无法运行")
        else:
            print(f"[就绪] npm {npm_text}")

    if failures:
        print("\n环境配置未完成：", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        print("安装缺失的系统依赖后，再让 Codex 重试同一句口令。", file=sys.stderr)
        return 1

    check_provider_status()
    if args.check_only:
        print("\n检查完成；未安装依赖，也未运行测试或构建。")
        return 0

    (PROJECT_ROOT / "workspace").mkdir(exist_ok=True)
    npm_install = "ci" if (FRONTEND_DIR / "package-lock.json").is_file() else "install"
    commands = [
        ([npm, npm_install], FRONTEND_DIR, "前端依赖安装"),
        ([sys.executable, "-m", "unittest", "discover", "-s", "backend/tests", "-v"], PROJECT_ROOT, "Python 测试"),
        ([npm, "run", "build"], FRONTEND_DIR, "前端构建"),
    ]
    for command, cwd, label in commands:
        try:
            result = run(command, cwd=cwd)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"\n[失败] {label}：{exc}", file=sys.stderr)
            return 1
        if result.returncode != 0:
            print(f"\n[失败] {label}，退出码 {result.returncode}", file=sys.stderr)
            return result.returncode
        print(f"[通过] {label}")

    print("\n✓ 环境配置完成。可运行：python3 scripts/start_workbench.py --open")
    print("  本脚本没有调用任何图片或视频生成服务。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
