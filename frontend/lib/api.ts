import type {
  EpisodeStatus,
  EpisodeSummary,
  ProjectSummary,
  WorkbenchEvent,
  AssetRecord,
  HistoricalAssetRecord,
  EpisodeUsage,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({ error: "无法解析工作台响应" }));
  if (!response.ok) {
    throw new Error(payload.error || payload.detail || `请求失败（${response.status}）`);
  }
  return payload as T;
}

export const api = {
  projects: () => request<{ projects: ProjectSummary[] }>("/api/projects"),
  episodes: (projectId: string) =>
    request<{ episodes: EpisodeSummary[] }>(
      `/api/projects/${encodeURIComponent(projectId)}/episodes`,
    ),
  status: (projectId: string, episodeId: string) =>
    request<EpisodeStatus>(
      `/api/episodes/${encodeURIComponent(episodeId)}/status?project=${encodeURIComponent(projectId)}`,
    ),
  events: (projectId: string, episodeId: string) =>
    request<{ events: WorkbenchEvent[] }>(
      `/api/episodes/${encodeURIComponent(episodeId)}/events?project=${encodeURIComponent(projectId)}`,
    ),
  usage: (projectId: string, episodeId: string) =>
    request<EpisodeUsage>(
      `/api/episodes/${encodeURIComponent(episodeId)}/usage?project=${encodeURIComponent(projectId)}`,
    ),
  assetVersions: (projectId: string, episodeId: string, assetId: string) =>
    request<{
      asset_id: string;
      active: AssetRecord | null;
      history: HistoricalAssetRecord[];
    }>(
      `/api/episodes/${encodeURIComponent(episodeId)}/asset-versions?project=${encodeURIComponent(projectId)}&asset_id=${encodeURIComponent(assetId)}`,
    ),
};

export function assetUrl(projectId: string, episodeId: string, path: string): string {
  return `/api/episodes/${encodeURIComponent(episodeId)}/assets/${encodeURIComponent(path)}?project=${encodeURIComponent(projectId)}`;
}
