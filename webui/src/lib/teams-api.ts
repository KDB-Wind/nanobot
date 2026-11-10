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
}

export interface TeamsTaskSummary {
  task_id: string;
  project_id: string;
  workflow_id: string;
  bot_id: string;
  dispatch_state: string;
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

export function fetchTeamsSnapshot(token: string, base: string = "", signal?: AbortSignal): Promise<TeamsSnapshot> {
  return request<TeamsSnapshot>(`${base}/api/teams/snapshot`, token, { signal }, 20_000);
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

export function scaffoldTeamsBot(
  transport: WebUIMutationTransport,
  values: { bot_id: string; role?: string; display_name?: string },
): Promise<TeamsScaffoldResult> {
  return transport.requestMutation("teams.scaffold_bot", values);
}
