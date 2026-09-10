#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def wait_for_url(
    url: str,
    timeout_sec: int = 30,
    process: Optional[subprocess.Popen] = None,
) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.status == 200
        except Exception:
            time.sleep(0.25)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="启动本地 AI 漫剧观察台")
    parser.add_argument("--open", action="store_true", help="就绪后打开浏览器")
    parser.add_argument("--api-port", type=int, default=8765)
    parser.add_argument("--web-port", type=int, default=3000)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()

    project_root = PROJECT_ROOT
    frontend_dir = project_root / "frontend"
    workspace_root = (args.workspace or project_root / "workspace").resolve()
    npm = shutil.which("npm")
    if npm is None:
        print("未找到 npm，请先安装 Node.js 20 或更高版本。", file=sys.stderr)
        return 1
    if not (frontend_dir / "node_modules").is_dir():
        print("前端依赖尚未安装，请先在 frontend 目录运行 npm install。", file=sys.stderr)
        return 1

    backend_url = "http://127.0.0.1:%d" % args.api_port
    frontend_url = "http://127.0.0.1:%d" % args.web_port
    environment = os.environ.copy()
    environment["WORKBENCH_API_URL"] = backend_url
    environment["NEXT_PUBLIC_WORKBENCH_API_URL"] = backend_url
    processes: List[subprocess.Popen] = []
    try:
        backend_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "backend.api",
                "--workspace",
                str(workspace_root),
                "--port",
                str(args.api_port),
            ],
            cwd=str(project_root),
        )
        processes.append(backend_process)
        if not wait_for_url(
            backend_url + "/api/health", timeout_sec=15, process=backend_process
        ):
            print("后端未能在 15 秒内就绪，请查看上方错误。", file=sys.stderr)
            return 1

        frontend_process = subprocess.Popen(
            [npm, "run", "dev", "--", "--hostname", "127.0.0.1", "--port", str(args.web_port)],
            cwd=str(frontend_dir),
            env=environment,
        )
        processes.append(frontend_process)
        if wait_for_url(frontend_url, process=frontend_process):
            print("AI 漫剧观察台已就绪：%s" % frontend_url)
            if args.open:
                webbrowser.open(frontend_url)
        else:
            print("工作台未能在 30 秒内就绪，请查看上方错误。", file=sys.stderr)
            return 1
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        failed = next((process.returncode for process in processes if process.returncode), 0)
        return int(failed or 0)
    except KeyboardInterrupt:
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
