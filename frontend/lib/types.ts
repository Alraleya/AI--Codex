export type StageState =
  | "pending"
  | "running"
  | "reviewing"
  | "repairing"
  | "waiting_confirmation"
  | "done"
  | "skipped"
  | "preserved"
  | "blocked";

export type RunState =
  | "idle"
  | "running"
  | "paused"
  | "waiting_agent"
  | "waiting_user_decision"
  | "waiting_confirmation"
  | "blocked"
  | "done";

export interface AnnotationSummary {
  state: "clear" | "pending";
  latest_revision: number;
  handled_revision: number;
  pending_count: number;
  redo_count: number;
  manifest_path: "annotations/index.json";
  updated_at: string | null;
}

export interface CreativeBrief {
  topic: string;
  hook: string;
  style: string;
  style_constraints: string;
  target_duration_sec: number;
  shot_plan: {
    state: "pending" | "resolved";
    shot_count: number | null;
    durations_sec: number[];
    reason: string;
  };
  storyboard_plan: {
    state: "pending" | "resolved";
    shots: Array<{
      shot_number: number;
      columns: number;
      rows: number;
      panel_count: number;
      timepoints_sec: number[];
      reason: string;
    }>;
  };
  aspect_ratio: string;
  production_mode?: "storyboard" | "dialogue_direct";
  video_model?: "pending" | "fast" | "mini";
  video_session_id?: string | null;
  provided_script?: string;
}

export interface StageStatus {
  label: string;
  state: StageState;
  skills: string[];
  depends_on: string[];
  execution_mode: "main_agent" | "parallel_tasks";
  task_unit: string;
  attempt: number;
  revision: number;
  started_at: string | null;
  completed_at: string | null;
  inputs: string[];
  outputs: string[];
  handoff: { summary: string; outputs: string[] } | null;
  usage: { input_tokens: number; output_tokens: number };
  review: { state: string; reason: string };
  error: string | null;
}

export interface AssetRecord {
  stage: string;
  task_id: string;
  kind: "document" | "prompt" | "image" | "video";
  role: string;
  label: string;
  path: string;
  mime: string;
  size: number;
  revision: number;
  asset_revision: number;
  supersedes_revision: number | null;
  status: "active" | "superseded";
  created_at: string;
  metadata: Record<string, unknown>;
}

export interface HistoricalAssetRecord extends AssetRecord {
  logical_asset_id: string;
  original_path: string;
  history_id?: string;
  superseded_at: string;
}

export interface TaskStatus {
  id: string;
  task_id: string;
  execution_id: string | null;
  execution_history: string[];
  stage: string;
  unit: string;
  executor: "main_agent" | "parallel_tasks";
  state:
    | "pending"
    | "queued"
    | "running"
    | "reviewing"
    | "passed"
    | "failed"
    | "redo_requested";
  attempt: number;
  asset_ids: string[];
  annotation_ids: string[];
  started_at: string | null;
  completed_at: string | null;
  review: { state: string; reason: string };
  error: string | null;
  updated_at: string;
}

export interface RepairSummary {
  state: "clear" | "planned" | "processing" | "blocked";
  latest_revision: number;
  active_plan_id: string | null;
  manifest_path: "repairs/index.json";
  updated_at: string | null;
}

export interface AgentRequest {
  state: "clear" | "waiting";
  request: {
    id: string;
    kind: "stage_generation" | "storyboard_sequence_review";
    stage_id: string;
    task_id: string | null;
    execution_id: string;
    stage_revision: number;
    created_at: string;
    payload: Record<string, unknown>;
  } | null;
}

export interface EpisodeStatus {
  schema_version: string;
  flow_id: "manga_episode";
  flow_version: "2.0";
  workbench_compatibility?: {
    mode: "retained_meituan_sunce_v1";
    source_flow_version: "1.0";
    read_only: true;
  };
  project_id: string;
  episode_id: string;
  creative_brief: CreativeBrief;
  run: {
    state: RunState;
    current_stage: string | null;
    last_error: string | null;
    waiting_for?: string | null;
  };
  stages: Record<string, StageStatus>;
  tasks: Record<string, TaskStatus>;
  assets: Record<string, AssetRecord>;
  asset_history: Record<string, HistoricalAssetRecord>;
  annotations: AnnotationSummary;
  repairs: RepairSummary;
  agent_request: AgentRequest;
  confirmations?: {
    video_generation?: {
      state: string;
      revision_fingerprint: string | null;
      approved_at: string | null;
      shots?: Record<
        string,
        {
          state: "required" | "approved" | "generated";
          revision_fingerprint: string | null;
          approved_at: string | null;
          generated_at: string | null;
        }
      >;
    };
  };
  metrics?: {
    input_tokens: number;
    output_tokens: number;
    image_generations: number;
    video_generations: number;
  };
  created_at: string;
  updated_at: string;
}

export interface UsageCounter {
  task_attempts: number;
  provider_calls: number;
  successful_provider_calls: number;
  reuse_count: number;
  failure_count: number;
  review_rounds: number;
  repair_plans: number;
  image_generations: number;
  video_generations: number;
  video_seconds: number;
  actual_input_tokens: number | null;
  actual_output_tokens: number | null;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  stage_attempts?: number;
}

export interface EpisodeUsage {
  source: "ledger" | "not_started";
  generated_at: string;
  totals: UsageCounter;
  stages: Record<string, UsageCounter>;
  tasks: Array<
    UsageCounter & {
      stage_id: string;
      task_id: string;
      last_error: string | null;
    }
  >;
  agent_timing: AgentTimingSummary;
  recent: Array<{
    ts?: string;
    kind?: string;
    status?: string;
    stage_id?: string;
    task_id?: string;
    provider?: string;
    error?: string;
    repair_plan_id?: string;
  }>;
}

export interface AgentTimingMetrics {
  nodes: number;
  completed_nodes: number;
  running_nodes: number;
  failed_nodes: number;
  stopped_nodes: number;
  total_runtime_ms: number;
  average_runtime_ms: number | null;
  p50_runtime_ms: number | null;
  p95_runtime_ms: number | null;
  total_queue_wait_ms: number;
  average_queue_wait_ms: number | null;
  total_node_ms: number;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  estimated_total_tokens: number;
  actual_input_tokens: number | null;
  actual_output_tokens: number | null;
  actual_total_tokens: number | null;
  tokens_per_runtime_minute: number | null;
}

export interface AgentTimingRun {
  node_id: string | null;
  project_id: string;
  episode_id: string;
  request_id: string | null;
  request_kind: string | null;
  stage_id: string;
  task_id: string | null;
  execution_id: string | null;
  stage_revision: number | null;
  task_attempt: number | null;
  session_id: string | null;
  agent_id: string | null;
  agent_type: string | null;
  model: string | null;
  status: "success" | "failed" | "running" | "stopped";
  started_at: string | null;
  ended_at: string | null;
  runtime_ms: number | null;
  queue_wait_ms: number | null;
  commit_ms: number | null;
  total_node_ms: number | null;
  estimated_input_tokens: number;
  estimated_output_tokens: number;
  estimated_total_tokens: number;
  actual_input_tokens: number | null;
  actual_output_tokens: number | null;
  error: string | null;
  last_assistant_message: string | null;
}

export interface AgentTimingSummary {
  source: "hook" | "not_started";
  schema_version: string;
  log_path: string;
  generated_at: string;
  totals: AgentTimingMetrics;
  stages: Record<string, AgentTimingMetrics>;
  tasks: Array<AgentTimingMetrics & { stage_id: string; task_id: string }>;
  recent: AgentTimingRun[];
}

export interface WorkbenchEvent {
  ts: string;
  type: "info" | "progress" | "asset" | "decision" | "done" | "block" | "error";
  stage: string | null;
  runner: string;
  content: string;
  meta: Record<string, unknown>;
}

export interface ProjectSummary {
  project_id: string;
  name: string;
  episode_count: number;
  created_at: string;
  updated_at: string;
}

export interface EpisodeSummary {
  schema_version: string;
  flow_id: "manga_episode";
  flow_version: "2.0";
  workbench_compatibility?: EpisodeStatus["workbench_compatibility"];
  project_id: string;
  episode_id: string;
  name: string;
  creative_brief: CreativeBrief;
  run: EpisodeStatus["run"];
  annotations: AnnotationSummary;
  progress: number;
  updated_at: string;
}

export interface AnnotationRecord {
  id: string;
  revision: number;
  stage: string;
  task_id: string;
  asset_id: string | null;
  asset_revision: number | null;
  type: "suggestion" | "redo";
  locator: Record<string, unknown>;
  content: string;
  created_at: string;
  updated_at: string;
  status:
    | "open"
    | "planned"
    | "processing"
    | "verifying"
    | "resolved"
    | "dismissed"
    | "superseded";
  repair_plan_id: string | null;
  resolution: { status: string; reason: string; resolved_at: string } | null;
}
