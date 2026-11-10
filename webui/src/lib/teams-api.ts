import { request, WebUIMutationTransport } from "./api";

export interface TeamsProjectSummary {
  project_id: string;
  display_name: string;
  created_at: string;
}

export interface TeamsBotSummary {
  bot_id: string;
  display_name: string;
  role: string;
  endpoint: string;
  enabled: boolean;
  instance_id: string | null;
  capabilities: string[];
  has_open_tasks: boolean;
  inbox_url: string | null;
  inbox_principal_id: string | null;
  inbox_bound: boolean;
}

export interface TeamsTaskSummary {
  task_id: string;
  project_id: string;
  workflow_id: string;
  bot_id: string;
  dispatch_state: string;
  delivery_mode: string;
  run_state: string;
  remote_run_id: string | null;
  stop_confirmed: boolean;
  effects_uncertain: boolean;
  cancel_state: string;
  delivery_state: string;
  outcome_code: string | null;
  result_available: boolean;
  result_fetched?: boolean;
  result_truncated?: boolean;
  deadline_at: string;
  created_at: string;
  finished_at: string | null;
  last_error: string;
  kind: string;
  conversation_root: string;
  hop: number;
  caller_principal: string;
}

export interface TeamsSnapshot {
  projects: TeamsProjectSummary[];
  bots: TeamsBotSummary[];
  tasks: TeamsTaskSummary[];
  quarantines: Record<string, { reason: string; created_at: string }>;
  protocol: number;
}

export interface TeamsTaskResult {
  task_id: string;
  run_state: string;
  outcome_code: string | null;
  result_text: string;
  result_truncated: boolean;
}

export interface TeamsScaffoldResult {
  bot_id: string;
  home: string;
  endpoint: string;
  secret_env: string;
  /** Operator-local file holding the one-time generated service key. */
  key_file: string;
  /** Environment names that must be set before the bot can load. */
  required_env: string[];
  start_command: string;
  started: boolean;
}

/** Per-model usage slice; the hit-rate denominator is observed input only. */
export interface TeamsUsageModel {
  provider: string;
  model: string;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  observed_input_tokens: number;
  cache_read_rate: number | null;
  coverage: number | null;
}

export interface TeamsBotUsage {
  bot_id: string;
  display_name: string;
  window: string;
  available: boolean;
  reason?: string;
  totals?: TeamsUsageModel;
  models?: TeamsUsageModel[];
}

export interface TeamsUsageResponse {
  window: string;
  bots: TeamsBotUsage[];
}

export interface TeamsPeerEdge {
  from_bot_id: string;
  to_bot_id: string;
}

export function fetchTeamsSnapshot(token: string, base: string = "", signal?: AbortSignal): Promise<TeamsSnapshot> {
  return request<TeamsSnapshot>(`${base}/api/teams/snapshot`, token, { signal }, 20_000);
}

export function fetchTeamsUsage(
  token: string,
  window: "7d" | "30d" | "all" = "30d",
  base: string = "",
  signal?: AbortSignal,
): Promise<TeamsUsageResponse> {
  return request<TeamsUsageResponse>(
    `${base}/api/teams/usage?window=${window}`,
    token,
    { signal },
    20_000,
  );
}

export function fetchTeamsBotUsage(
  token: string,
  botId: string,
  window: "7d" | "30d" | "all" = "30d",
  base: string = "",
  signal?: AbortSignal,
): Promise<TeamsBotUsage> {
  return request<TeamsBotUsage>(
    `${base}/api/teams/bots/${encodeURIComponent(botId)}/usage?window=${window}`,
    token,
    { signal },
    20_000,
  );
}

export function fetchTeamsPeerEdges(
  token: string,
  base: string = "",
  signal?: AbortSignal,
): Promise<{ edges: TeamsPeerEdge[] }> {
  return request<{ edges: TeamsPeerEdge[] }>(
    `${base}/api/teams/peer-edges`,
    token,
    { signal },
    20_000,
  );
}

export function fetchTeamsTask(
  token: string,
  taskId: string,
  base: string = "",
  signal?: AbortSignal,
): Promise<TeamsTaskSummary> {
  return request<TeamsTaskSummary>(
    `${base}/api/teams/tasks/${encodeURIComponent(taskId)}`,
    token,
    { signal },
    20_000,
  );
}

export function fetchTeamsTaskResult(
  token: string,
  taskId: string,
  base: string = "",
  signal?: AbortSignal,
): Promise<TeamsTaskResult> {
  return request<TeamsTaskResult>(
    `${base}/api/teams/tasks/${encodeURIComponent(taskId)}/result`,
    token,
    { signal },
    20_000,
  );
}

export function createTeamsProject(
  transport: WebUIMutationTransport,
  projectId: string,
  displayName: string,
): Promise<{ project_id: string }> {
  return transport.requestMutation("teams.create_project", {
    project_id: projectId,
    display_name: displayName,
  });
}

export function createTeamsTask(
  transport: WebUIMutationTransport,
  values: {
    project: string;
    bot: string;
    input: string;
    operation_key: string;
    workflow?: string;
  },
): Promise<TeamsTaskSummary> {
  return transport.requestMutation("teams.create_task", values);
}

export function cancelTeamsTask(
  transport: WebUIMutationTransport,
  taskId: string,
): Promise<TeamsTaskSummary> {
  return transport.requestMutation("teams.cancel_task", { task_id: taskId });
}

export interface TeamsWorkflowStopResult {
  workflow_id: string;
  revoked: number;
  cancelled: number;
  unconfirmed: number;
  total: number;
}

export function stopTeamsWorkflow(
  transport: WebUIMutationTransport,
  workflowId: string,
): Promise<TeamsWorkflowStopResult> {
  return transport.requestMutation("teams.stop_workflow", { workflow_id: workflowId });
}

export function scaffoldTeamsBot(
  transport: WebUIMutationTransport,
  values: { bot_id: string; role?: string; display_name?: string },
): Promise<TeamsScaffoldResult> {
  return transport.requestMutation("teams.scaffold_bot", values);
}
