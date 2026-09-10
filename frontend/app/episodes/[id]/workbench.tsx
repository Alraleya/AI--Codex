"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, assetUrl } from "../../../lib/api";
import type {
  AssetRecord,
  EpisodeStatus,
  HistoricalAssetRecord,
  EpisodeUsage,
  AgentTimingRun,
  StageState,
  WorkbenchEvent,
} from "../../../lib/types";

const stateText: Record<StageState, string> = {
  pending: "待处理",
  running: "制作中",
  reviewing: "审查中",
  repairing: "修复中",
  waiting_confirmation: "待确认",
  done: "已完成",
  skipped: "已跳过",
  preserved: "已保留（跳过重做）",
  blocked: "已阻塞",
};

const runText = {
  idle: "等待 Codex 推进",
  running: "Codex 正在制作",
  paused: "流程已暂停",
  waiting_agent: "等待 Codex 子 Agent",
  waiting_user_decision: "等待用户决定",
  waiting_confirmation: "等待视频生成确认",
  blocked: "流程已阻塞",
  done: "全流程完成",
} as const;

const newFlowStageOrder = [
  "story_design",
  "asset_planning",
  "character_design",
  "visual_design",
  "storyboard_binding",
  "storyboard_generation",
  "video_binding",
  "video_generation",
  "edit_post",
] as const;

const coreStageIds = new Set<string>(newFlowStageOrder);
const unitStageIds = new Set([
  "storyboard_binding",
  "storyboard_generation",
  "video_binding",
  "video_generation",
]);

const coreStageNames: Record<string, string> = {
  story_design: "剧本",
  asset_planning: "定妆资源规划",
  character_design: "角色定妆",
  visual_design: "场景与道具定妆",
  storyboard_binding: "故事板参考绑定",
  storyboard_generation: "故事板",
  video_binding: "视频参考绑定",
  video_generation: "视频",
  edit_post: "剪辑交付",
};

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function durationLabel(value: number | null | undefined) {
  if (value == null) return "—";
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60000) return `${(value / 1000).toFixed(1)}s`;
  const minutes = Math.floor(value / 60000);
  const seconds = Math.round((value % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}

function timingStatusLabel(status: AgentTimingRun["status"]) {
  return {
    success: "成功",
    failed: "失败",
    running: "进行中",
    stopped: "已停止",
  }[status];
}

function modelLabel(value: string | null | undefined) {
  if (!value) return "模型未上报";
  return value.split("/").pop() || value;
}

function videoModelLabel(value: string | null | undefined) {
  return value === "fast" ? "Fast VIP" : value === "mini" ? "Mini" : "待选择";
}

function videoApprovalLabel(value: string | undefined) {
  return value === "approved"
    ? "已审批"
    : value === "generated"
      ? "已生成"
      : "待审批";
}

function number(value: number | null | undefined) {
  return new Intl.NumberFormat("zh-CN").format(value || 0);
}

function tokenLabel(usage: EpisodeUsage | null) {
  if (!usage || usage.source === "not_started") return "—";
  const estimated =
    usage.totals.estimated_input_tokens + usage.totals.estimated_output_tokens;
  return estimated ? `约 ${number(estimated)}` : "暂无估算";
}

function usageKindLabel(kind?: string) {
  return (
    {
      provider_call: "Provider 调用",
      agent_request: "Agent 请求",
      agent_result: "Agent 返回",
      review_round: "审查轮次",
      reuse: "复用",
      repair_plan: "修复计划",
      failure: "失败",
    }[kind || ""] ||
    kind ||
    "执行记录"
  );
}

function assetKind(kind: AssetRecord["kind"]) {
  return { document: "文档", prompt: "提示词", image: "图片", video: "视频" }[
    kind
  ];
}

function isFinalVideoPrompt(asset: AssetRecord | HistoricalAssetRecord) {
  return (
    asset.stage === "story_design" &&
    asset.kind === "prompt" &&
    /shot\d+_prompt_video\.txt$/.test(asset.path)
  );
}

function shotKey(asset: AssetRecord | HistoricalAssetRecord) {
  const match = `${asset.task_id} ${asset.path}`.match(/shot\d{2}/i);
  return match ? match[0].toLowerCase() : asset.task_id;
}

function plannedAssetId(asset: AssetRecord) {
  const value = asset.metadata.planned_asset_id;
  if (typeof value === "string") return value;

  // Recovered or reused design images may predate planned_asset_id metadata.
  // Their canonical filenames still carry the locked asset key, so derive it
  // to keep per-unit reference bindings visible in the workbench.
  const match = asset.path.match(/^(char|scene|prop)_(.+)_sheet\.png$/);
  return match ? `${match[1]}_${match[2]}` : null;
}

function unitArtifactLabel(asset: AssetRecord) {
  if (isFinalVideoPrompt(asset)) return "锁定视频 Prompt";
  if (/shot\d+_prompt_storyboard\.txt$/.test(asset.path))
    return "锁定故事板 Prompt";
  if (asset.stage === "storyboard_binding") return "故事板参考绑定";
  if (asset.stage === "storyboard_generation") return "故事板成图";
  if (asset.stage === "video_binding") return "视频参考绑定";
  if (asset.stage === "video_generation") return "最终视频";
  return asset.label;
}

function unitArtifactOrder(asset: AssetRecord) {
  if (/shot\d+_prompt_storyboard\.txt$/.test(asset.path)) return 1;
  if (isFinalVideoPrompt(asset)) return 2;
  if (asset.stage === "storyboard_binding") return 3;
  if (asset.stage === "storyboard_generation") return 4;
  if (asset.stage === "video_binding") return 5;
  if (asset.stage === "video_generation") return 6;
  return 99;
}

function isFinalAsset(stageId: string, asset: AssetRecord) {
  if (asset.kind === "prompt") return false;
  if (stageId === "story_design") return asset.path === "script.md";
  if (["character_design", "visual_design"].includes(stageId)) {
    return asset.kind === "image";
  }
  if (stageId === "storyboard_binding" || stageId === "video_binding") {
    return asset.role === "reference_binding";
  }
  if (stageId === "storyboard_generation") {
    return asset.kind === "image";
  }
  if (stageId === "video_generation") return asset.kind === "video";
  if (stageId === "edit_post") {
    return asset.kind === "video" || asset.kind === "document";
  }
  return asset.kind === "document";
}

function AssetPreview({
  projectId,
  episodeId,
  asset,
  compact = false,
}: {
  projectId: string;
  episodeId: string;
  asset: AssetRecord;
  compact?: boolean;
}) {
  const url = assetUrl(projectId, episodeId, asset.path);
  if (asset.kind === "image") {
    return <img className="asset-thumb" src={url} alt={asset.label} />;
  }
  if (asset.kind === "video") {
    return (
      <video
        className={`asset-thumb ${
          asset.stage === "video_generation" ? "asset-video-final" : ""
        }`}
        src={url}
        controls={!compact}
        preload="metadata"
      />
    );
  }
  return (
    <div className={`asset-file-icon ${asset.kind}`}>
      <span>{asset.kind === "prompt" ? "P" : "D"}</span>
      <small>{asset.path.split(".").pop()?.toUpperCase()}</small>
    </div>
  );
}

export default function EpisodeWorkbench({
  episodeId,
  projectId,
}: {
  episodeId: string;
  projectId: string;
}) {
  const [status, setStatus] = useState<EpisodeStatus | null>(null);
  const [episodeName, setEpisodeName] = useState(episodeId);
  const [events, setEvents] = useState<WorkbenchEvent[]>([]);
  const [usage, setUsage] = useState<EpisodeUsage | null>(null);
  const [selectedStage, setSelectedStage] = useState("");
  const [promptFolderStage, setPromptFolderStage] = useState<string | null>(
    null,
  );
  const [selectedUnit, setSelectedUnit] = useState("shot01");
  const [unitReferenceIds, setUnitReferenceIds] = useState<
    Record<string, string[]>
  >({});
  const [viewerAsset, setViewerAsset] = useState<
    [string, AssetRecord | HistoricalAssetRecord] | null
  >(null);
  const [viewerVersions, setViewerVersions] = useState<
    Array<AssetRecord | HistoricalAssetRecord>
  >([]);
  const [viewerText, setViewerText] = useState("");
  const [copyState, setCopyState] = useState<"provider" | "full" | null>(null);
  const [timingStageFilter, setTimingStageFilter] = useState("all");
  const [timingStatusFilter, setTimingStatusFilter] = useState<
    "all" | AgentTimingRun["status"]
  >("all");
  const [selectedTimingNode, setSelectedTimingNode] = useState<string | null>(
    null,
  );
  const [error, setError] = useState("");

  const loadAll = useCallback(async () => {
    if (!projectId) return;
    try {
      const [
        nextStatus,
        eventResult,
        episodeResult,
        usageResult,
      ] = await Promise.all([
        api.status(projectId, episodeId),
        api.events(projectId, episodeId),
        api.episodes(projectId),
        api.usage(projectId, episodeId),
      ]);
      if (
        nextStatus.flow_id !== "manga_episode" ||
        nextStatus.flow_version !== "2.0"
      ) {
        throw new Error("该剧集不是新版九阶段流程；旧剧集需要单独适配后再查看");
      }
      const stageIds = Object.keys(nextStatus.stages);
      if (
        stageIds.length !== newFlowStageOrder.length ||
        newFlowStageOrder.some((stageId) => !nextStatus.stages[stageId])
      ) {
        throw new Error("新版剧集状态不完整：工作台需要完整九阶段数据");
      }
      setStatus(nextStatus);
      setEvents(eventResult.events);
      setUsage(usageResult);
      setEpisodeName(
        episodeResult.episodes.find(
          (episode) => episode.episode_id === episodeId,
        )?.name || episodeId,
      );
      setSelectedStage((current) =>
        current && nextStatus.stages[current]
          ? current
          : nextStatus.run.current_stage || Object.keys(nextStatus.stages)[0],
      );
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取剧集状态");
    }
  }, [episodeId, projectId]);

  useEffect(() => {
    void loadAll();
    const timer = window.setInterval(() => void loadAll(), 4000);
    return () => window.clearInterval(timer);
  }, [loadAll]);

  useEffect(() => {
    if (!status) return;
    const count = status.creative_brief.shot_plan.shot_count || 0;
    const currentNumber = Number(selectedUnit.replace("shot", ""));
    if (count > 0 && (currentNumber < 1 || currentNumber > count))
      setSelectedUnit("shot01");
  }, [selectedUnit, status]);

  useEffect(() => {
    if (!status) return;
    const bindings = Object.entries(status.assets).filter(
      ([, asset]) =>
        asset.role === "reference_binding" &&
        ["storyboard_binding", "video_binding"].includes(asset.stage),
    );
    let cancelled = false;
    void Promise.all(
      bindings.map(async ([, asset]) => {
        try {
          const response = await fetch(
            assetUrl(projectId, episodeId, asset.path),
            { cache: "no-store" },
          );
          if (!response.ok) return null;
          const manifest = (await response.json()) as {
            references?: Array<{ asset_id?: string }>;
          };
          return [
            shotKey(asset),
            (manifest.references || [])
              .map((item) => item.asset_id)
              .filter((id): id is string => Boolean(id)),
          ] as const;
        } catch {
          return null;
        }
      }),
    ).then((entries) => {
      if (cancelled) return;
      const next: Record<string, string[]> = {};
      entries.forEach((entry) => {
        if (!entry) return;
        next[entry[0]] = Array.from(
          new Set([...(next[entry[0]] || []), ...entry[1]]),
        );
      });
      setUnitReferenceIds(next);
    });
    return () => {
      cancelled = true;
    };
  }, [episodeId, projectId, status]);

  const stageEntries = useMemo(
    () =>
      status
        ? newFlowStageOrder.map(
            (stageId) => [stageId, status.stages[stageId]] as const,
          )
        : [],
    [status],
  );
  const stageAssets = useMemo(
    () =>
      status
        ? Object.entries(status.assets).filter(
            ([, asset]) => asset.stage === selectedStage,
          )
        : [],
    [selectedStage, status],
  );
  const selectedStageFinalAssets = stageAssets.filter(([, asset]) =>
    isFinalAsset(selectedStage, asset),
  );
  const allPromptAssets = useMemo(
    () =>
      status
        ? Object.entries(status.assets).filter(
            ([, asset]) => asset.kind === "prompt",
          )
        : [],
    [status],
  );
  const intermediatePromptAssets = useMemo(
    () => allPromptAssets.filter(([, asset]) => !isFinalVideoPrompt(asset)),
    [allPromptAssets],
  );
  const assetHistoryCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    if (!status) return counts;
    Object.values(status.asset_history).forEach((asset) => {
      counts[asset.logical_asset_id] =
        (counts[asset.logical_asset_id] || 0) + 1;
    });
    return counts;
  }, [status]);
  const completed = stageEntries.filter(
    ([, stage]) =>
      stage.state === "done" ||
      stage.state === "skipped" ||
      stage.state === "preserved",
  ).length;
  const progress = stageEntries.length
    ? Math.round((completed / stageEntries.length) * 100)
    : 0;
  const machineCurrentStageId = status?.run.current_stage || selectedStage;
  const machineCurrentStage =
    status && machineCurrentStageId
      ? status.stages[machineCurrentStageId]
      : null;
  const usageTotal = usage?.totals;
  const selectedUsage = selectedStage ? usage?.stages[selectedStage] : null;
  const noisyTasks = (usage?.tasks || [])
    .filter((task) => task.task_attempts > 1)
    .slice(0, 8);
  const timing = usage?.agent_timing;
  const timingTotal = timing?.totals;
  const selectedTiming = selectedStage ? timing?.stages[selectedStage] : null;
  const timingHotspots = (timing?.tasks || []).slice(0, 8);
  const timingRuns = timing?.recent || [];
  const timingStageOptions = useMemo(
    () =>
      Array.from(new Set(timingRuns.map((run) => run.stage_id))).filter(
        Boolean,
      ),
    [timingRuns],
  );
  const filteredTimingRuns = useMemo(
    () =>
      timingRuns.filter(
        (run) =>
          (timingStageFilter === "all" || run.stage_id === timingStageFilter) &&
          (timingStatusFilter === "all" || run.status === timingStatusFilter),
      ),
    [timingRuns, timingStageFilter, timingStatusFilter],
  );
  const selectedTimingRun =
    timingRuns.find((run) => run.node_id === selectedTimingNode) ||
    filteredTimingRuns[0] ||
    null;

  async function showAssetVersion(
    assetId: string,
    asset: AssetRecord | HistoricalAssetRecord,
  ) {
    setViewerAsset([assetId, asset]);
    setViewerText("");
    setCopyState(null);
    if (asset.kind === "document" || asset.kind === "prompt") {
      try {
        const response = await fetch(
          assetUrl(projectId, episodeId, asset.path),
          { cache: "no-store" },
        );
        setViewerText(await response.text());
      } catch {
        setViewerText("无法读取文本内容");
      }
    }
  }

  async function openAsset(assetId: string, asset: AssetRecord) {
    try {
      const versions = await api.assetVersions(projectId, episodeId, assetId);
      setViewerVersions([
        ...(versions.active ? [versions.active] : []),
        ...versions.history,
      ]);
    } catch {
      setViewerVersions([asset]);
    }
    await showAssetVersion(assetId, asset);
  }

  async function copyText(content: string, kind: "provider" | "full") {
    try {
      await navigator.clipboard.writeText(content);
      setCopyState(kind);
      window.setTimeout(() => setCopyState(null), 1800);
    } catch {
      setError("复制失败，请检查浏览器剪贴板权限");
    }
  }

  if (!projectId) {
    return (
      <main className="center-state">
        缺少项目参数
        <Link href="/">返回剧集库</Link>
      </main>
    );
  }

  if (!status) {
    return (
      <main className="center-state">
        {error || "正在读取剧集…"}
        <Link href="/">返回剧集库</Link>
      </main>
    );
  }

  const currentStage = status.stages[selectedStage];
  const shotPlan = status.creative_brief.shot_plan;
  const referenceAssets = Object.entries(status.assets).filter(
    ([, asset]) =>
      asset.kind === "image" &&
      ["character_design", "visual_design", "storyboard_generation"].includes(
        asset.stage,
      ),
  );
  const selectedReferenceIds = unitReferenceIds[selectedUnit] || [];
  const selectedReferenceAssets = status.workbench_compatibility
    ? referenceAssets
    : referenceAssets.filter(
        ([, asset]) =>
          (asset.stage === "storyboard_generation" &&
            shotKey(asset) === selectedUnit) ||
          (plannedAssetId(asset) !== null &&
            selectedReferenceIds.includes(plannedAssetId(asset)!)),
      );
  const referenceGroups = [
    ["character_design", "角色定妆"],
    ["visual_design", "场景与道具定妆"],
    ["storyboard_generation", "故事板"],
  ]
    .map(([stageId, label]) => ({
      stageId,
      label,
      assets: selectedReferenceAssets.filter(
        ([, asset]) => asset.stage === stageId,
      ),
    }))
    .filter((group) => group.assets.length > 0);
  const selectedUnitNumber = Number(selectedUnit.replace("shot", ""));
  const selectedUnitDuration = shotPlan.durations_sec[selectedUnitNumber - 1];
  const selectedUnitArtifacts = Object.entries(status.assets)
    .filter(
      ([, asset]) =>
        shotKey(asset) === selectedUnit &&
        (selectedStage === "storyboard_binding"
          ? /shot\d+_prompt_storyboard\.txt$/.test(asset.path) ||
            asset.stage === "storyboard_binding"
          : selectedStage === "video_binding"
            ? isFinalVideoPrompt(asset) || asset.stage === "video_binding"
            : selectedStage === "video_generation"
              ? isFinalVideoPrompt(asset) ||
                asset.stage === "video_binding" ||
                asset.stage === "video_generation"
              : asset.stage === selectedStage),
    )
    .sort(
      ([, left], [, right]) =>
        unitArtifactOrder(left) - unitArtifactOrder(right),
    );
  const selectedStoryboard = selectedUnitArtifacts.find(
    ([, asset]) =>
      asset.stage === "storyboard_generation" && asset.kind === "image",
  );
  const selectedVideo = selectedUnitArtifacts.find(
    ([, asset]) => asset.stage === "video_generation" && asset.kind === "video",
  );
  const unitNumbers = Array.from(
    { length: shotPlan.shot_count || 0 },
    (_, index) => index + 1,
  );
  const videoApproval = status.confirmations?.video_generation;
  const videoModel = videoModelLabel(status.creative_brief.video_model);
  const videoShotApprovals = Array.from(
    { length: shotPlan.shot_count || 0 },
    (_, index) => {
      const shot = `shot${String(index + 1).padStart(2, "0")}`;
      const task = status.tasks[`video_generation:${shot}`];
      const approval = videoApproval?.shots?.[shot];
      return {
        shot,
        duration: shotPlan.durations_sec[index],
        state: approval?.state || "required",
        taskState: task?.state || "pending",
      };
    },
  );
  const atelierFolderStageIds = [
    "story_design",
    "asset_planning",
    "character_design",
    "visual_design",
    "storyboard_generation",
    "video_binding",
    "edit_post",
  ];
  return (
    <main className="workbench-shell atelier-workbench">
      <header className="atelier-header">
        <div className="atelier-brand">
          <span className="atelier-brand-mark">M</span>
          <span>
            <strong>AI 漫剧</strong>
            <small>PRODUCTION CONTROL</small>
          </span>
        </div>
        <div className="atelier-episode">
          <small>
            {projectId} / {episodeId}
          </small>
          <strong>{episodeName}</strong>
        </div>
        <div className="atelier-header-actions">
          <span className={`atelier-sync-dot ${status.run.state}`} />{" "}
          <span>{runText[status.run.state]}</span>
          <button onClick={() => void loadAll()} aria-label="刷新状态">
            刷新
          </button>
          <Link href="/">返回剧集库</Link>
        </div>
      </header>

      <section
        className="atelier-flow"
        id="workflow-status"
        aria-label="完整制作流程"
      >
        <div className="atelier-flow-heading">
          <div>
            <span>PIPELINE / 09 STAGES</span>
            <strong>生产管线</strong>
          </div>
          <div className="atelier-progress">
            <b>{progress}%</b>
            <small>
              {completed} / {stageEntries.length} 已完成
            </small>
          </div>
        </div>
        <div className="atelier-rail">
          <i style={{ width: `${progress}%` }} />
          {stageEntries.map(([stageId, stage], index) => (
        <a
              key={stageId}
              href={coreStageIds.has(stageId) ? "#stage-content" : "#workflow-status"}
              className={`atelier-stage ${stage.state} ${stageId === machineCurrentStageId ? "active" : ""}`}
              onClick={() => setSelectedStage(stageId)}
            >
              <span>{String(index + 1).padStart(2, "0")}</span>
              <strong>{coreStageNames[stageId] || stage.label}</strong>
              <small>{stateText[stage.state]}</small>
            </a>
          ))}
        </div>
      </section>

      <section className="atelier-now">
        <div>
          <span className="atelier-kicker">CURRENT NODE</span>
          <h1>
            {String(
              Math.max(
                0,
                stageEntries.findIndex(([id]) => id === machineCurrentStageId) +
                  1,
              ),
            ).padStart(2, "0")}{" "}
            {machineCurrentStage
              ? coreStageNames[machineCurrentStageId] ||
                machineCurrentStage.label
              : "流程尚未开始"}{" "}
            <em>{runText[status.run.state]}</em>
          </h1>
          <p>
            {status.run.state === "idle"
              ? "Codex 推进后，将继续生成当前节点并进入下一阶段。"
              : status.run.state === "waiting_agent"
                ? "当前节点已交给子 Agent，完成后会将 handoff 回传给主控。"
                : status.run.state === "waiting_confirmation"
                  ? "视频按生成单元逐一审批；未审批的单元不会调用 Provider。"
                  : status.creative_brief.hook || "暂无当前节点说明"}
          </p>
        </div>
        <div className="atelier-brief-meta">
          <span>{status.creative_brief.style}</span>
          <span>{status.creative_brief.target_duration_sec} 秒</span>
          <span>{status.creative_brief.aspect_ratio}</span>
          <span>
            {shotPlan.state === "resolved"
              ? `${shotPlan.shot_count} 个视频单元`
              : "待拆分"}
          </span>
          <span>视频模型：{videoModel}</span>
        </div>
      </section>

      {status.agent_request.state === "waiting" &&
      status.agent_request.request ? (
        <section className="atelier-agent">
          <span className="atelier-agent-dot" />
          <strong>子 Agent 执行中</strong>
          <span>
            {status.agent_request.request.kind} ·{" "}
            {status.agent_request.request.task_id ||
              status.agent_request.request.stage_id}
          </span>
        </section>
      ) : null}
      {error || status.run.last_error ? (
        <div className="atelier-error">{error || status.run.last_error}</div>
      ) : null}

      {!unitStageIds.has(selectedStage) ? (
        <section className="atelier-stage-output" id="stage-content">
          <div className="atelier-unit-heading">
            <div>
              <span>STAGE OUTPUT</span>
              <h2>{coreStageNames[selectedStage] || currentStage?.label}</h2>
              <p>当前阶段只展示本阶段的最终产物，不混入其他节点内容。</p>
            </div>
            <small>{selectedStageFinalAssets.length} 个产物</small>
          </div>
          {selectedStageFinalAssets.length ? (
            <div className="atelier-stage-output-grid">
              {selectedStageFinalAssets.map(([assetId, asset]) => (
                <button
                  type="button"
                  className={`atelier-stage-output-card ${asset.kind}`}
                  key={assetId}
                  onClick={() => void openAsset(assetId, asset)}
                >
                  <div className="atelier-stage-output-preview">
                    <AssetPreview
                      projectId={projectId}
                      episodeId={episodeId}
                      asset={asset}
                    />
                  </div>
                  <span>
                    <strong>{asset.label}</strong>
                    <small>{asset.path}</small>
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="atelier-unit-empty">
              <strong>当前阶段暂无最终产物</strong>
              <span>阶段完成后，相关内容会显示在这里。</span>
            </div>
          )}
        </section>
      ) : null}

      {unitStageIds.has(selectedStage) ? (
      <section
        className="atelier-unit-workspace"
        id="stage-content"
        aria-label="按视频单元归档的制作产物"
      >
        <div className="atelier-unit-heading">
          <div>
            <span>VIDEO UNIT DOSSIER</span>
            <h2>逐单元制作档案</h2>
            <p>
              {coreStageNames[selectedStage]}阶段按视频生成单元展示对应产物。
            </p>
          </div>
          {status.workbench_compatibility ? (
            <em>美团孙策 · 旧版只读兼容</em>
          ) : (
            <small>新版九阶段 · Prompt 不因故事板改写</small>
          )}
        </div>
        <div className="atelier-unit-tabs">
          {unitNumbers.length ? (
            unitNumbers.map((number) => {
              const key = `shot${String(number).padStart(2, "0")}`;
              const boardReady = Object.values(status.assets).some(
                (asset) =>
                  asset.stage === "storyboard_generation" &&
                  shotKey(asset) === key,
              );
              const videoReady = Object.values(status.assets).some(
                (asset) =>
                  asset.stage === "video_generation" && shotKey(asset) === key,
              );
              return (
                <button
                  type="button"
                  className={selectedUnit === key ? "active" : ""}
                  key={key}
                  onClick={() => setSelectedUnit(key)}
                >
                  <b>{String(number).padStart(2, "0")}</b>
                  <span>视频单元</span>
                  <small>
                    {videoReady
                      ? "视频完成"
                      : boardReady
                        ? "故事板完成"
                        : "制作中"}
                  </small>
                </button>
              );
            })
          ) : (
            <div className="atelier-unit-tabs-empty">
              第一阶段完成后显示视频单元。
            </div>
          )}
        </div>
        {unitNumbers.length ? (
          <div className="atelier-unit-body">
            <div className="atelier-unit-main">
              <div className="atelier-unit-title">
                <div>
                  <small>
                    VIDEO UNIT {String(selectedUnitNumber).padStart(2, "0")}
                  </small>
                  <h3>
                    视频单元 {String(selectedUnitNumber).padStart(2, "0")}
                  </h3>
                </div>
                <div>
                  <span>
                    {selectedUnitDuration
                      ? `${selectedUnitDuration} 秒`
                      : "时长待定"}
                  </span>
                  <span>
                    {selectedStoryboard ? "有故事板" : "无故事板或待生成"}
                  </span>
                  <span>{selectedVideo ? "视频已生成" : "视频待生成"}</span>
                </div>
              </div>
              <div className="atelier-unit-artifacts">
                {selectedUnitArtifacts.length ? (
                  selectedUnitArtifacts.map(([assetId, asset]) => (
                    <button
                      type="button"
                      className={`atelier-unit-artifact ${asset.kind}`}
                      key={assetId}
                      onClick={() => void openAsset(assetId, asset)}
                    >
                      <div className="atelier-unit-artifact-preview">
                        <AssetPreview
                          projectId={projectId}
                          episodeId={episodeId}
                          asset={asset}
                        />
                      </div>
                      <span>
                        <small>{unitArtifactLabel(asset)}</small>
                        <strong>{asset.label}</strong>
                        <em>{asset.path}</em>
                      </span>
                      <i>↗</i>
                    </button>
                  ))
                ) : (
                  <div className="atelier-unit-empty">
                    <strong>当前单元尚无产物</strong>
                    <span>
                      第一阶段完成后，会先出现锁定的故事板 Prompt 与视频
                      Prompt。
                    </span>
                  </div>
                )}
              </div>
            </div>
            <aside className="atelier-unit-references">
              <div className="atelier-section-label">
                <span>BOUND REFERENCES</span>
                <strong>
                  {status.workbench_compatibility
                    ? "旧版共享定妆资产"
                    : "本单元绑定定妆"}
                </strong>
              </div>
              {referenceGroups.length ? (
                <div className="atelier-reference-groups">
                  {referenceGroups.map((group) => (
                    <section
                      className="atelier-reference-group"
                      key={group.stageId}
                    >
                      <div className="atelier-reference-group-label">
                        <strong>{group.label}</strong>
                        <small>{group.assets.length} 张</small>
                      </div>
                      <div className="atelier-reference-grid">
                        {group.assets.map(([assetId, asset]) => (
                          <button
                            key={assetId}
                            onClick={() => void openAsset(assetId, asset)}
                            aria-label={`${group.label}：${asset.label}`}
                          >
                            <AssetPreview
                              projectId={projectId}
                              episodeId={episodeId}
                              asset={asset}
                            />
                            <span className="atelier-reference-caption">
                              {asset.label}
                            </span>
                          </button>
                        ))}
                      </div>
                    </section>
                  ))}
                </div>
              ) : (
                <div className="atelier-unit-reference-empty">
                  本单元还没有已绑定并通过的定妆图。
                </div>
              )}
            </aside>
          </div>
        ) : (
          <div className="atelier-unit-empty">
            <strong>等待第一阶段拆分</strong>
            <span>视频生成单元锁定后，相关产物会自动集中到这里。</span>
          </div>
        )}
      </section>
      ) : null}

      {!selectedStage ? <section className="atelier-delivery" id="core-outputs">
        <div className="atelier-delivery-heading">
          <div>
            <span className="atelier-kicker">FINAL DELIVERABLES</span>
            <h2>最终交付物</h2>
          </div>
          <div>
            <small>只展示最终文件</small>
            {intermediatePromptAssets.length ? (
              <button
                className="atelier-prompt-link"
                onClick={() =>
                  setPromptFolderStage((current) =>
                    current === "__all__" ? null : "__all__",
                  )
                }
              >
                Prompt 文件夹 <b>{intermediatePromptAssets.length}</b>
              </button>
            ) : null}
          </div>
        </div>
        <div className="atelier-folder-row">
          {atelierFolderStageIds.map((stageId) => {
            const stage = status.stages[stageId];
            const allAssets = Object.entries(status.assets).filter(
              ([, asset]) => asset.stage === stageId,
            );
            const finalAssets = allAssets.filter(([, asset]) =>
              isFinalAsset(stageId, asset),
            );
            const promptAssets = allAssets.filter(
              ([, asset]) => asset.kind === "prompt",
            );
            return (
              <article
                className="atelier-folder"
                id={`atelier-output-${stageId}`}
                key={stageId}
              >
                <div className="atelier-folder-tab" />
                {finalAssets.length ? (
                  <div className="atelier-folder-assets">
                    {finalAssets.map(([assetId, asset]) => (
                      <button
                        type="button"
                        className="atelier-folder-asset"
                        key={assetId}
                        onClick={() => void openAsset(assetId, asset)}
                        title={`打开${asset.label}`}
                      >
                        <div className="atelier-folder-preview">
                          <AssetPreview
                            projectId={projectId}
                            episodeId={episodeId}
                            asset={asset}
                            compact
                          />
                        </div>
                        <span>{asset.kind === "video" ? "视频" : "文档"}</span>
                      </button>
                    ))}
                  </div>
                ) : (
                  <button
                    type="button"
                    className="atelier-folder-empty"
                    onClick={() => setSelectedStage(stageId)}
                  >
                    暂无最终产物
                  </button>
                )}
                <strong>{coreStageNames[stageId] || stage?.label}</strong>
                <small>
                  {finalAssets.length
                    ? `${finalAssets.length} 个最终文件`
                    : promptAssets.length
                      ? "Prompt 已收纳"
                      : stateText[stage?.state || "pending"]}
                </small>
                <span className="atelier-folder-arrow">↗</span>
              </article>
            );
          })}
          <article className="atelier-folder atelier-prompt-folder">
            <button
              onClick={() =>
                setPromptFolderStage((current) =>
                  current === "__all__" ? null : "__all__",
                )
              }
            >
              <div className="atelier-folder-tab" />
              <div className="atelier-prompt-glyph">P</div>
              <strong>Prompt 文件夹</strong>
              <small>{intermediatePromptAssets.length} 个中间文件</small>
              <span className="atelier-folder-arrow">↗</span>
            </button>
          </article>
        </div>
        {promptFolderStage === "__all__" && intermediatePromptAssets.length ? (
          <div className="atelier-prompt-drawer">
            <div>
              <strong>Prompt 文件夹</strong>
              <button onClick={() => setPromptFolderStage(null)}>收起</button>
            </div>
            <div>
              {intermediatePromptAssets.map(([assetId, asset]) => (
                <button
                  key={assetId}
                  onClick={() => void openAsset(assetId, asset)}
                >
                  <b>P</b>
                  <span>{asset.label}</span>
                  <small>
                    {coreStageNames[asset.stage] ||
                      status.stages[asset.stage]?.label}{" "}
                    · {asset.path}
                  </small>
                </button>
              ))}
            </div>
          </div>
        ) : null}
      </section> : null}

      {!status.workbench_compatibility ? (
        <section className="atelier-approval" aria-label="视频审批闸门">
          <div className="atelier-approval-head">
            <div>
              <span className="atelier-kicker">VIDEO UNIT APPROVAL GATE</span>
              <h2>视频生成单元审批</h2>
              <p>
                本集模型：<strong>{videoModel}</strong> · sessionId：
                <strong>
                  {status.creative_brief.video_session_id || "待配置"}
                </strong>{" "}
                · 每次审批只允许生成一个视频单元。
              </p>
            </div>
            <span
              className={`atelier-approval-state ${status.run.state === "waiting_confirmation" ? "open" : ""}`}
            >
              {status.run.state === "waiting_confirmation"
                ? "等待审批"
                : "按需审批"}
            </span>
          </div>
          {videoShotApprovals.length ? (
            <div className="atelier-approval-grid">
              {videoShotApprovals.map((item) => (
                <div className="atelier-approval-row" key={item.shot}>
                  <b>{item.shot.replace("shot", "视频单元 ")}</b>
                  <span>{item.duration ? `${item.duration} 秒` : "—"}</span>
                  <span className={`atelier-approval-chip ${item.state}`}>
                    {videoApprovalLabel(item.state)}
                  </span>
                  <small>
                    任务：
                    {item.taskState === "passed"
                      ? "完成"
                      : item.taskState === "failed"
                        ? "失败"
                        : item.taskState === "running"
                          ? "生成中"
                          : "未运行"}
                  </small>
                </div>
              ))}
            </div>
          ) : (
            <div className="atelier-approval-empty">
              第一阶段完成视频单元拆分后，这里会显示逐单元审批队列。
            </div>
          )}
        </section>
      ) : null}

      <section className="atelier-usage" aria-label="运行账本">
        <div className="atelier-usage-head">
          <div>
            <span className="atelier-kicker">RUN LEDGER</span>
            <h2>运行账本</h2>
            <p>把任务尝试、真实调用、复用和审查拆开，定位流程成本。</p>
          </div>
          <small>
            {usage?.source === "not_started"
              ? "本集尚未开始写入账本"
              : "实时账本 · 自动刷新 4s"}
          </small>
        </div>
        <div className="atelier-usage-cards">
          <article>
            <small>任务尝试</small>
            <strong>
              {usage?.source === "not_started"
                ? "—"
                : number(usageTotal?.task_attempts)}
            </strong>
            <span>
              {usage?.source === "not_started"
                ? "新账本尚未记录"
                : "状态机重进次数"}
            </span>
          </article>
          <article className="accent">
            <small>Provider 调用</small>
            <strong>
              {usage?.source === "not_started"
                ? "—"
                : number(usageTotal?.provider_calls)}
            </strong>
            <span>
              {usage?.source === "not_started"
                ? "新账本尚未记录"
                : `${number(usageTotal?.successful_provider_calls)} 次成功 · ${number(usageTotal?.failure_count)} 次失败`}
            </span>
          </article>
          <article>
            <small>复用 / 审查</small>
            <strong>
              {usage?.source === "not_started" ? (
                "—"
              ) : (
                <>
                  {number(usageTotal?.reuse_count)} <em>/</em>{" "}
                  {number(usageTotal?.review_rounds)}
                </>
              )}
            </strong>
            <span>
              {usage?.source === "not_started"
                ? "新账本尚未记录"
                : "复用次数 · 审查轮次"}
            </span>
          </article>
          <article className="token">
            <small>Token</small>
            <strong>{tokenLabel(usage)}</strong>
            <span>
              {usageTotal?.actual_input_tokens == null
                ? "真实 token 未上报，仅展示估算"
                : "provider 实际上报"}
            </span>
          </article>
        </div>
        <div className="atelier-usage-grid">
          <div className="atelier-usage-table-wrap">
            <div className="atelier-usage-subhead">
              <strong>按阶段看</strong>
              <small>尝试 ≠ Provider 调用</small>
            </div>
            <div className="atelier-usage-table">
              <div className="atelier-usage-row header">
                <span>阶段</span>
                <span>尝试</span>
                <span>调用</span>
                <span>复用 / 失败</span>
                <span>审查</span>
                <span>估算 token</span>
              </div>
              {stageEntries.map(([stageId, stage]) => {
                const item = usage?.stages[stageId];
                const stageToken =
                  (item?.estimated_input_tokens || 0) +
                  (item?.estimated_output_tokens || 0);
                return (
                  <button
                    className={`atelier-usage-row ${stageId === selectedStage ? "selected" : ""}`}
                    key={stageId}
                    onClick={() => setSelectedStage(stageId)}
                  >
                    <span>
                      <b>{coreStageNames[stageId] || stage.label}</b>
                      <small>{stateText[stage.state]}</small>
                    </span>
                    <strong>
                      {usage?.source === "not_started"
                        ? "—"
                        : number(item?.task_attempts)}
                    </strong>
                    <strong>
                      {usage?.source === "not_started"
                        ? "—"
                        : number(item?.provider_calls)}
                    </strong>
                    <strong>
                      {usage?.source === "not_started"
                        ? "—"
                        : `${number(item?.reuse_count)} / ${number(item?.failure_count)}`}
                    </strong>
                    <strong>
                      {usage?.source === "not_started"
                        ? "—"
                        : number(item?.review_rounds)}
                    </strong>
                    <span>
                      {usage?.source === "not_started"
                        ? "—"
                        : stageToken
                          ? `约 ${number(stageToken)}`
                          : "—"}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
          <aside className="atelier-usage-focus">
            <div className="atelier-usage-subhead">
              <strong>
                {coreStageNames[selectedStage] ||
                  currentStage?.label ||
                  "当前阶段"}
              </strong>
              <small>重复热点</small>
            </div>
            {selectedUsage ? (
              <div className="atelier-focus-summary">
                <b>{number(selectedUsage.task_attempts)}</b>
                <span>次任务尝试</span>
                <i /> <b>{number(selectedUsage.provider_calls)}</b>
                <span>次真实调用</span>
              </div>
            ) : null}
            <div className="atelier-noisy-tasks">
              {noisyTasks.length ? (
                noisyTasks.map((task) => (
                  <div
                    key={`${task.stage_id}:${task.task_id}`}
                    className={task.stage_id === selectedStage ? "current" : ""}
                  >
                    <span>{task.task_id}</span>
                    <b>{number(task.task_attempts)} 次尝试</b>
                    <small>
                      {number(task.provider_calls)} 调用 ·{" "}
                      {number(task.reuse_count)} 复用 ·{" "}
                      {number(task.failure_count)} 失败
                    </small>
                  </div>
                ))
              ) : (
                <p>暂时没有重复任务。</p>
              )}
            </div>
          </aside>
        </div>
        <details className="atelier-usage-details">
          <summary>查看最近执行记录</summary>
          <div>
            {(usage?.recent || []).slice(0, 12).map((item, index) => (
              <div key={`${item.ts}-${index}`}>
                <span className={`usage-status ${item.status || ""}`} />{" "}
                <b>{usageKindLabel(item.kind)}</b>
                <span>
                  {coreStageNames[item.stage_id || ""] ||
                    item.stage_id ||
                    "全局"}
                  {item.task_id ? ` · ${item.task_id}` : ""}
                </span>
                <small>{item.ts ? formatTime(item.ts) : "—"}</small>
              </div>
            ))}
          </div>
        </details>
      </section>

      <section className="atelier-timing" aria-label="节点遥测">
        <div className="atelier-timing-head">
          <div>
            <span className="atelier-kicker">NODE TELEMETRY / CODEX HOOK</span>
            <h2>节点遥测</h2>
            <p>从子 Agent 启动到结束，按节点拆解等待、执行、回传和 token。</p>
          </div>
          <small>
            {timing?.source === "hook"
              ? `钩子账本 · ${timing.log_path}`
              : "等待钩子写入第一条记录"}
          </small>
        </div>
        <div className="atelier-timing-cards">
          <article>
            <small>节点数</small>
            <strong>{timingTotal?.nodes || 0}</strong>
            <span>
              {timingTotal?.completed_nodes || 0} 完成 ·{" "}
              {timingTotal?.running_nodes || 0} 进行中
            </span>
          </article>
          <article className="accent">
            <small>平均执行</small>
            <strong>{durationLabel(timingTotal?.average_runtime_ms)}</strong>
            <span>
              P50 {durationLabel(timingTotal?.p50_runtime_ms)} · P95{" "}
              {durationLabel(timingTotal?.p95_runtime_ms)}
            </span>
          </article>
          <article>
            <small>平均排队</small>
            <strong>{durationLabel(timingTotal?.average_queue_wait_ms)}</strong>
            <span>请求创建到子 Agent 启动</span>
          </article>
          <article className="token">
            <small>估算 token</small>
            <strong>{number(timingTotal?.estimated_total_tokens)}</strong>
            <span>
              输入 {number(timingTotal?.estimated_input_tokens)} · 输出{" "}
              {number(timingTotal?.estimated_output_tokens)}
            </span>
          </article>
          <article>
            <small>执行效率</small>
            <strong>
              {timingTotal?.tokens_per_runtime_minute
                ? `${number(timingTotal.tokens_per_runtime_minute)}`
                : "—"}
            </strong>
            <span>估算 token / 执行分钟</span>
          </article>
        </div>
        <div className="atelier-timing-grid">
          <div className="atelier-timing-table-wrap">
            <div className="atelier-usage-subhead">
              <strong>按阶段看子 Agent</strong>
              <small>耗时来自 Codex 钩子</small>
            </div>
            <div className="atelier-timing-table">
              <div className="atelier-timing-row header">
                <span>阶段</span>
                <span>节点</span>
                <span>平均</span>
                <span>P95</span>
                <span>排队</span>
                <span>估算 token</span>
              </div>
              {stageEntries.map(([stageId, stage]) => {
                const item = timing?.stages[stageId];
                return (
                  <button
                    className={`atelier-timing-row ${stageId === selectedStage ? "selected" : ""}`}
                    key={stageId}
                    onClick={() => setSelectedStage(stageId)}
                  >
                    <span>
                      <b>{coreStageNames[stageId] || stage.label}</b>
                      <small>{stateText[stage.state]}</small>
                    </span>
                    <strong>{number(item?.nodes)}</strong>
                    <strong>{durationLabel(item?.average_runtime_ms)}</strong>
                    <strong>{durationLabel(item?.p95_runtime_ms)}</strong>
                    <strong>
                      {durationLabel(item?.average_queue_wait_ms)}
                    </strong>
                    <span>{number(item?.estimated_total_tokens)}</span>
                  </button>
                );
              })}
            </div>
          </div>
          <aside className="atelier-timing-focus">
            <div className="atelier-usage-subhead">
              <strong>
                {coreStageNames[selectedStage] ||
                  currentStage?.label ||
                  "当前阶段"}
              </strong>
              <small>时间热点</small>
            </div>
            {selectedTiming ? (
              <div className="atelier-focus-summary">
                <b>{number(selectedTiming.nodes)}</b>
                <span>节点</span>
                <i />
                <b>{durationLabel(selectedTiming.total_runtime_ms)}</b>
                <span>累计执行</span>
              </div>
            ) : null}
            <div className="atelier-timing-hotspots">
              {timingHotspots
                .filter(
                  (task) => task.stage_id === selectedStage || !selectedTiming,
                )
                .slice(0, 6)
                .map((task) => (
                  <div
                    key={`${task.stage_id}:${task.task_id}`}
                    className={task.stage_id === selectedStage ? "current" : ""}
                  >
                    <span>{task.task_id}</span>
                    <b>{durationLabel(task.total_runtime_ms)}</b>
                    <small>
                      {number(task.nodes)} 节点 ·{" "}
                      {number(task.estimated_total_tokens)} token · P95{" "}
                      {durationLabel(task.p95_runtime_ms)}
                    </small>
                  </div>
                ))}
            </div>
            {!timingHotspots.length ? (
              <p className="atelier-timing-empty">
                钩子尚未记录子 Agent 节点。
              </p>
            ) : null}
          </aside>
        </div>
        <div className="atelier-timing-log">
          <div className="atelier-timing-log-head">
            <div className="atelier-usage-subhead">
              <strong>钩子执行日志</strong>
              <small>
                {filteredTimingRuns.length} / {timingRuns.length} 个节点 ·
                点击查看完整上下文
              </small>
            </div>
            <div className="atelier-timing-filters">
              <label>
                <span>阶段</span>
                <select
                  value={timingStageFilter}
                  onChange={(event) => setTimingStageFilter(event.target.value)}
                >
                  <option value="all">全部阶段</option>
                  {timingStageOptions.map((stageId) => (
                    <option value={stageId} key={stageId}>
                      {coreStageNames[stageId] || stageId}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>状态</span>
                <select
                  value={timingStatusFilter}
                  onChange={(event) =>
                    setTimingStatusFilter(
                      event.target.value as typeof timingStatusFilter,
                    )
                  }
                >
                  <option value="all">全部状态</option>
                  <option value="success">成功</option>
                  <option value="running">进行中</option>
                  <option value="failed">失败</option>
                  <option value="stopped">已停止</option>
                </select>
              </label>
            </div>
          </div>
          <div className="atelier-timing-log-grid">
            <div className="atelier-timing-recent-list">
              <div className="atelier-timing-log-row header">
                <span>状态</span>
                <span>节点</span>
                <span>阶段 / 任务</span>
                <span>执行</span>
                <span>token</span>
                <span>开始时间</span>
              </div>
              {filteredTimingRuns.length ? (
                filteredTimingRuns.slice(0, 50).map((run) => (
                  <button
                    type="button"
                    className={`atelier-timing-log-row ${run.node_id === selectedTimingRun?.node_id ? "selected" : ""}`}
                    key={run.node_id || `${run.started_at}-${run.task_id}`}
                    onClick={() => setSelectedTimingNode(run.node_id)}
                  >
                    <span>
                      <i className={`timing-status ${run.status}`} />
                      {timingStatusLabel(run.status)}
                    </span>
                    <b>{run.agent_id || "未命名 Agent"}</b>
                    <span>
                      <strong>
                        {coreStageNames[run.stage_id] || run.stage_id}
                      </strong>
                      <small>{run.task_id || "—"}</small>
                    </span>
                    <strong>{durationLabel(run.runtime_ms)}</strong>
                    <span>
                      {run.estimated_total_tokens
                        ? number(run.estimated_total_tokens)
                        : "—"}
                    </span>
                    <small>
                      {run.started_at ? formatTime(run.started_at) : "—"}
                    </small>
                  </button>
                ))
              ) : (
                <p className="atelier-timing-empty">当前筛选条件下没有日志。</p>
              )}
            </div>
            <aside className="atelier-timing-detail">
              <div className="atelier-usage-subhead">
                <strong>节点详情</strong>
                <small>
                  {selectedTimingRun
                    ? timingStatusLabel(selectedTimingRun.status)
                    : "未选择"}
                </small>
              </div>
              {selectedTimingRun ? (
                <div className="atelier-timing-detail-body">
                  <div className="atelier-timing-detail-title">
                    <span
                      className={`timing-status ${selectedTimingRun.status}`}
                    />
                    <div>
                      <strong>
                        {coreStageNames[selectedTimingRun.stage_id] ||
                          selectedTimingRun.stage_id}
                      </strong>
                      <small>{selectedTimingRun.task_id || "未命名任务"}</small>
                    </div>
                  </div>
                  <div className="atelier-timing-detail-metrics">
                    <div>
                      <small>排队</small>
                      <b>{durationLabel(selectedTimingRun.queue_wait_ms)}</b>
                    </div>
                    <div>
                      <small>执行</small>
                      <b>{durationLabel(selectedTimingRun.runtime_ms)}</b>
                    </div>
                    <div>
                      <small>回传</small>
                      <b>{durationLabel(selectedTimingRun.commit_ms)}</b>
                    </div>
                    <div>
                      <small>节点总耗时</small>
                      <b>{durationLabel(selectedTimingRun.total_node_ms)}</b>
                    </div>
                  </div>
                  <dl>
                    <div>
                      <dt>模型</dt>
                      <dd>{modelLabel(selectedTimingRun.model)}</dd>
                    </div>
                    <div>
                      <dt>执行 ID</dt>
                      <dd>{selectedTimingRun.execution_id || "—"}</dd>
                    </div>
                    <div>
                      <dt>revision / attempt</dt>
                      <dd>
                        r{selectedTimingRun.stage_revision ?? "—"} /{" "}
                        {selectedTimingRun.task_attempt ?? "—"}
                      </dd>
                    </div>
                    <div>
                      <dt>开始</dt>
                      <dd>
                        {selectedTimingRun.started_at
                          ? formatTime(selectedTimingRun.started_at)
                          : "—"}
                      </dd>
                    </div>
                    <div>
                      <dt>结束</dt>
                      <dd>
                        {selectedTimingRun.ended_at
                          ? formatTime(selectedTimingRun.ended_at)
                          : "尚未结束"}
                      </dd>
                    </div>
                    <div>
                      <dt>token</dt>
                      <dd>
                        输入 {number(selectedTimingRun.estimated_input_tokens)}{" "}
                        · 输出{" "}
                        {number(selectedTimingRun.estimated_output_tokens)}
                      </dd>
                    </div>
                  </dl>
                  {selectedTimingRun.error ? (
                    <div className="atelier-timing-detail-error">
                      <small>错误</small>
                      <p>{selectedTimingRun.error}</p>
                    </div>
                  ) : null}
                  {selectedTimingRun.last_assistant_message ? (
                    <details className="atelier-timing-message">
                      <summary>查看结束消息</summary>
                      <pre>{selectedTimingRun.last_assistant_message}</pre>
                    </details>
                  ) : null}
                </div>
              ) : (
                <p className="atelier-timing-empty">
                  选择一个节点查看执行上下文。
                </p>
              )}
            </aside>
          </div>
        </div>
      </section>

      <section className="atelier-activity">
        <div className="atelier-delivery-heading">
          <div>
            <span className="atelier-kicker">LIVE LOG</span>
            <h2>最近活动</h2>
          </div>
          <small>自动刷新 · 4s</small>
        </div>
        <div className="atelier-activity-list">
          {events
            .slice()
            .reverse()
            .slice(0, 8)
            .map((event, index) => (
              <article key={`${event.ts}-${index}`}>
                <i className={event.type} />
                <div>
                  <strong>{event.content}</strong>
                  <small>
                    {event.stage || "全局"} · {event.runner} ·{" "}
                    {formatTime(event.ts)}
                  </small>
                </div>
              </article>
            ))}
        </div>
      </section>

      {viewerAsset ? (
        <div className="viewer-backdrop" onClick={() => setViewerAsset(null)}>
          <div
            className="viewer-card"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="viewer-heading">
              <div>
                <strong>{viewerAsset[1].label}</strong>
                <span>{viewerAsset[1].path}</span>
              </div>
              <button onClick={() => setViewerAsset(null)}>关闭</button>
            </div>
            {viewerAsset[1].kind === "image" ? (
              <img
                src={assetUrl(projectId, episodeId, viewerAsset[1].path)}
                alt={viewerAsset[1].label}
              />
            ) : viewerAsset[1].kind === "video" ? (
              <video
                src={assetUrl(projectId, episodeId, viewerAsset[1].path)}
                controls
                autoPlay
              />
            ) : isFinalVideoPrompt(viewerAsset[1]) ? (
              <div className="final-prompt-viewer">
                <div className="final-prompt-toolbar">
                  <span>第一阶段锁定视频提示词</span>
                  <div>
                    <button
                      type="button"
                      onClick={() => void copyText(viewerText, "provider")}
                    >
                      {copyState === "provider" ? "已复制" : "复制正式提示词"}
                    </button>
                  </div>
                </div>
                <div className="final-prompt-block provider">
                  <small>原样提交模型</small>
                  <pre>{viewerText || "正在读取…"}</pre>
                </div>
              </div>
            ) : (
              <pre>{viewerText || "正在读取…"}</pre>
            )}
            {viewerVersions.length > 1 ? (
              <div className="version-strip">
                {viewerVersions.map((asset) => (
                  <button
                    type="button"
                    className={
                      asset.path === viewerAsset[1].path ? "active" : ""
                    }
                    key={asset.path}
                    onClick={() => void showAssetVersion(viewerAsset[0], asset)}
                  >
                    v{asset.asset_revision}
                    {asset.status === "active" ? " 当前" : " 旧版"}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </main>
  );
}
