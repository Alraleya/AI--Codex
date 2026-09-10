"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import type { EpisodeSummary, ProjectSummary } from "../lib/types";

const runLabels = {
  idle: "待推进",
  running: "制作中",
  paused: "已暂停",
  waiting_agent: "等待子 Agent",
  waiting_user_decision: "等待故事板决定",
  waiting_confirmation: "待确认视频",
  blocked: "已阻塞",
  done: "已完成",
} as const;

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function shotLabel(episode: EpisodeSummary) {
  const plan = episode.creative_brief.shot_plan;
  return plan.state === "resolved" && plan.shot_count
    ? `${plan.shot_count} 个视频单元`
    : "等待拆分视频单元";
}

export default function HomePage() {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [episodes, setEpisodes] = useState<EpisodeSummary[]>([]);
  const [selectedProject, setSelectedProject] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadProjects = useCallback(async () => {
    try {
      const result = await api.projects();
      setProjects(result.projects);
      setSelectedProject((current) =>
        result.projects.some((project) => project.project_id === current)
          ? current
          : result.projects[0]?.project_id || "",
      );
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取项目");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadEpisodes = useCallback(async () => {
    if (!selectedProject) {
      setEpisodes([]);
      return;
    }
    try {
      const result = await api.episodes(selectedProject);
      setEpisodes(
        result.episodes.filter((episode) => episode.flow_version === "2.0"),
      );
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取剧集");
    }
  }, [selectedProject]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  useEffect(() => {
    void loadEpisodes();
    const timer = window.setInterval(() => void loadEpisodes(), 5000);
    return () => window.clearInterval(timer);
  }, [loadEpisodes]);

  const project = useMemo(
    () => projects.find((item) => item.project_id === selectedProject),
    [projects, selectedProject],
  );

  if (loading) {
    return <main className="center-state">正在读取本地剧集…</main>;
  }

  return (
    <main className="library-shell">
      <header className="app-bar">
        <div className="brand-mark">M</div>
        <div>
          <strong>AI 漫剧工作台</strong>
          <span>本地制作观察台</span>
        </div>
        <div className="viewer-badge">只读状态</div>
      </header>

      <div className="library-layout">
        <aside className="project-sidebar">
          <div className="sidebar-heading">
            <span>项目</span>
            <small>{projects.length}</small>
          </div>
          <div className="project-stack">
            {projects.map((item) => (
              <button
                className={
                  item.project_id === selectedProject
                    ? "project-button active"
                    : "project-button"
                }
                key={item.project_id}
                onClick={() => setSelectedProject(item.project_id)}
              >
                <span className="project-avatar">{item.name.slice(0, 1)}</span>
                <span>
                  <strong>{item.name}</strong>
                  <small>{item.episode_count} 集</small>
                </span>
              </button>
            ))}
          </div>
          <p className="codex-hint">
            新建和制作请直接在 Codex 中完成，工作台会自动读取最新状态。
          </p>
        </aside>

        <section className="episode-library">
          <div className="library-heading">
            <div>
              <p>剧集库</p>
              <h1>{project?.name || "尚无项目"}</h1>
            </div>
            <button
              className="quiet-button"
              onClick={() => void loadEpisodes()}
            >
              刷新
            </button>
          </div>

          {error ? <div className="error-banner">{error}</div> : null}

          {episodes.length ? (
            <div className="episode-grid">
              {episodes.map((episode) => {
                const percent = Math.round(episode.progress * 100);
                return (
                  <Link
                    className="episode-card"
                    key={episode.episode_id}
                    href={`/episodes/${encodeURIComponent(episode.episode_id)}?project=${encodeURIComponent(episode.project_id)}`}
                  >
                    <div className="episode-card-top">
                      <span className={`run-pill ${episode.run.state}`}>
                        {runLabels[episode.run.state]}
                      </span>
                      <small>{formatTime(episode.updated_at)}</small>
                    </div>
                    <h2>{episode.name}</h2>
                    <p>{episode.creative_brief.topic}</p>
                    <div className="episode-meta-row">
                      <span>{episode.creative_brief.style}</span>
                      <span>
                        {episode.creative_brief.target_duration_sec} 秒
                      </span>
                      <span>{shotLabel(episode)}</span>
                    </div>
                    <div className="progress-copy">
                      <span>全流程</span>
                      <strong>{percent}%</strong>
                    </div>
                    <div className="progress-track">
                      <i style={{ width: `${percent}%` }} />
                    </div>
                    <div className="episode-card-bottom">
                      <span>{episode.run.current_stage || "流程已结束"}</span>
                    </div>
                  </Link>
                );
              })}
            </div>
          ) : (
            <div className="empty-panel">
              <strong>还没有可展示的剧集</strong>
              <p>在 Codex 中创建剧集后，这里会自动按项目分类出现。</p>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
