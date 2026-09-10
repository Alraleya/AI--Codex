import argparse
import json
import mimetypes
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from backend.core.annotations import AnnotationStore
from backend.core.agent_timing import summarize_agent_timing
from backend.core.assets import resolve_asset_path
from backend.core.events import EventLog
from backend.core.lock import EpisodeLockedError
from backend.core.projects import Workspace
from backend.core.usage import UsageLedger, summarize_episode
from backend.workbench_compat import (
    LEGACY_WORKBENCH_EPISODE,
    is_retained_legacy_episode,
    load_retained_legacy_status,
)


class WorkbenchApplication:
    """Read-only production viewer with one isolated annotation write boundary."""

    def __init__(self, project_root: Path, workspace_root: Path):
        self.project_root = Path(project_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace = Workspace(self.workspace_root)
        self.events = EventLog(self.workspace_root)
        self.annotations = AnnotationStore(self.workspace_root)
        self.usage = UsageLedger(self.workspace_root)

    def load_status(self, project_id: str, episode_id: str) -> Dict[str, Any]:
        legacy_status_path = (
            self.workspace_root
            / "status"
            / LEGACY_WORKBENCH_EPISODE[0]
            / (LEGACY_WORKBENCH_EPISODE[1] + ".json")
        )
        if is_retained_legacy_episode(project_id, episode_id) and legacy_status_path.is_file():
            return load_retained_legacy_status(self.workspace_root)
        return self.workspace.statuses.load(project_id, episode_id)

    def list_episodes(self, project_id: str) -> list:
        legacy_status_path = (
            self.workspace_root
            / "status"
            / LEGACY_WORKBENCH_EPISODE[0]
            / (LEGACY_WORKBENCH_EPISODE[1] + ".json")
        )
        if project_id != LEGACY_WORKBENCH_EPISODE[0] or not legacy_status_path.is_file():
            # The historical compatibility episode is optional. If it has been
            # removed, list the current v2 episodes normally instead of trying
            # to load a missing s01e01 status file.
            return self.workspace.list_episodes(project_id)

        status = self.load_status(*LEGACY_WORKBENCH_EPISODE)
        if any(item["episode_id"] == LEGACY_WORKBENCH_EPISODE[1] for item in episodes):
            return episodes
        meta_path = self.workspace.episode_dir(*LEGACY_WORKBENCH_EPISODE) / "episode.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        done = sum(stage["state"] in {"done", "skipped", "preserved"} for stage in status["stages"].values())
        return episodes + [{
            "schema_version": status["schema_version"],
            "flow_id": status["flow_id"],
            "flow_version": status["flow_version"],
            "project_id": project_id,
            "episode_id": LEGACY_WORKBENCH_EPISODE[1],
            "name": meta.get("name", "美团孙策"),
            "creative_brief": status["creative_brief"],
            "run": status["run"],
            "annotations": status["annotations"],
            "progress": done / len(status["stages"]),
            "updated_at": status["updated_at"],
            "workbench_compatibility": status["workbench_compatibility"],
        }]

    def list_projects(self) -> list:
        projects = self.workspace.list_projects()
        legacy_project_path = (
            self.workspace_root
            / "projects"
            / LEGACY_WORKBENCH_EPISODE[0]
            / "project.json"
        )
        if (
            legacy_project_path.is_file()
            and not any(item["project_id"] == LEGACY_WORKBENCH_EPISODE[0] for item in projects)
        ):
            projects.append(json.loads(legacy_project_path.read_text(encoding="utf-8")))
        for project in projects:
            project["episode_count"] = len(self.list_episodes(project["project_id"]))
        return projects


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int], app: WorkbenchApplication):
        super().__init__(server_address, WorkbenchRequestHandler)
        self.app = app


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    server: WorkbenchHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:3000")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")

    def _send_json(self, status_code: int, payload: Any) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if length <= 0 or length > 65536:
            raise ValueError("request body must be between 1 and 65536 bytes")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("request body must be valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _dispatch_with_errors(self, method: str) -> None:
        try:
            self._dispatch(method)
        except EpisodeLockedError as error:
            self._send_json(HTTPStatus.CONFLICT, {"error": str(error)})
        except FileNotFoundError as error:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
        except (KeyError, TypeError, ValueError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal workbench error", "detail": str(error)},
            )

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._common_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._dispatch_with_errors("GET")

    def do_POST(self) -> None:
        self._dispatch_with_errors("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        query = parse_qs(parsed.query)
        app = self.server.app

        if method == "GET" and path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "mode": "viewer", "schema_version": "1.0"},
            )
            return

        if method == "GET" and path == "/api/projects":
            self._send_json(HTTPStatus.OK, {"projects": app.list_projects()})
            return

        match = re.fullmatch(r"/api/projects/([^/]+)/episodes", path)
        if match and method == "GET":
            self._send_json(
                HTTPStatus.OK,
                {"episodes": app.list_episodes(match.group(1))},
            )
            return

        match = re.fullmatch(r"/api/episodes/([^/]+)/status", path)
        if match and method == "GET":
            project_id = self._project_query(query)
            self._send_json(
                HTTPStatus.OK,
                app.load_status(project_id, match.group(1)),
            )
            return

        match = re.fullmatch(r"/api/episodes/([^/]+)/events", path)
        if match and method == "GET":
            project_id = self._project_query(query)
            limit = min(200, max(1, int(query.get("limit", ["80"])[0])))
            self._send_json(
                HTTPStatus.OK,
                {"events": app.events.tail(project_id, match.group(1), limit)},
            )
            return

        match = re.fullmatch(r"/api/episodes/([^/]+)/usage", path)
        if match and method == "GET":
            project_id = self._project_query(query)
            episode_id = match.group(1)
            status = app.load_status(project_id, episode_id)
            usage_records = app.usage.records(project_id, episode_id)
            usage_summary = summarize_episode(
                status,
                app.workspace_root,
                app.events.tail(project_id, episode_id, 10000),
                app.usage,
            )
            usage_summary["agent_timing"] = summarize_agent_timing(
                status,
                app.workspace_root,
                usage_records,
            )
            self._send_json(
                HTTPStatus.OK,
                usage_summary,
            )
            return

        match = re.fullmatch(r"/api/episodes/([^/]+)/annotations", path)
        if match:
            episode_id = match.group(1)
            if method == "GET":
                project_id = self._project_query(query)
                pending_only = query.get("pending") == ["1"]
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "annotations": app.annotations.list(
                            project_id, episode_id, pending_only=pending_only
                        )
                    },
                )
                return
            if method == "POST":
                body = self._read_json()
                annotation = app.annotations.add(
                    body.get("project", ""),
                    episode_id,
                    body.get("stage", ""),
                    body.get("content", ""),
                    annotation_type=body.get("type", "suggestion"),
                    task_id=body.get("task_id"),
                    asset_id=body.get("asset_id"),
                    locator=body.get("locator"),
                )
                self._send_json(HTTPStatus.CREATED, annotation)
                return

        match = re.fullmatch(r"/api/episodes/([^/]+)/asset-versions", path)
        if match and method == "GET":
            project_id = self._project_query(query)
            asset_ids = query.get("asset_id")
            if not asset_ids or not asset_ids[0]:
                raise ValueError("asset_id query parameter is required")
            status = app.load_status(project_id, match.group(1))
            asset_id = asset_ids[0]
            active = status["assets"].get(asset_id)
            history = [
                dict(record, history_id=history_id)
                for history_id, record in status.get("asset_history", {}).items()
                if record.get("logical_asset_id") == asset_id
            ]
            if active is None and not history:
                raise FileNotFoundError("asset versions are unavailable")
            history.sort(key=lambda item: int(item.get("asset_revision", 1)), reverse=True)
            self._send_json(
                HTTPStatus.OK,
                {"asset_id": asset_id, "active": active, "history": history},
            )
            return

        match = re.fullmatch(r"/api/episodes/([^/]+)/assets/(.+)", path)
        if match and method == "GET":
            self._serve_asset(self._project_query(query), match.group(1), match.group(2))
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "route not found"})

    @staticmethod
    def _project_query(query: Dict[str, Any]) -> str:
        values = query.get("project")
        if not values or not values[0]:
            raise ValueError("project query parameter is required")
        return values[0]

    def _serve_asset(self, project_id: str, episode_id: str, relative_path: str) -> None:
        app = self.server.app
        status = app.load_status(project_id, episode_id)
        active_paths = {asset["path"]: asset for asset in status["assets"].values()}
        historical_paths = {
            asset["path"]: asset for asset in status.get("asset_history", {}).values()
        }
        allowed_paths = {**historical_paths, **active_paths}
        if relative_path not in allowed_paths:
            raise FileNotFoundError("asset is not registered")
        path = resolve_asset_path(
            app.workspace.episode_dir(project_id, episode_id), relative_path
        )
        if not path.is_file():
            raise FileNotFoundError("asset file is missing")
        content = path.read_bytes()
        mime = allowed_paths[relative_path].get("mime") or mimetypes.guess_type(path.name)[0]
        self.send_response(HTTPStatus.OK)
        self._common_headers()
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def create_server(
    host: str,
    port: int,
    project_root: Path,
    workspace_root: Path,
) -> WorkbenchHTTPServer:
    app = WorkbenchApplication(project_root, workspace_root)
    return WorkbenchHTTPServer((host, port), app)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local manga episode viewer API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path("workspace"))
    arguments = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    server = create_server(
        arguments.host,
        arguments.port,
        project_root,
        arguments.workspace,
    )
    print("AI manga viewer API: http://%s:%d" % server.server_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
