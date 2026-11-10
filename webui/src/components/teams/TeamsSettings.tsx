import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  cancelTeamsTask,
  createTeamsProject,
  createTeamsTask,
  fetchTeamsSnapshot,
  fetchTeamsTaskResult,
  scaffoldTeamsBot,
  TeamsScaffoldResult,
  TeamsSnapshot,
  TeamsTaskResult,
} from "@/lib/teams-api";
import { useClient } from "@/providers/ClientProvider";

function newOperationKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `op-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function StateChip({ label, tone }: { label: string; tone: "ok" | "warn" | "bad" | "idle" }) {
  const palette = {
    ok: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400",
    warn: "bg-amber-500/15 text-amber-600 dark:text-amber-400",
    bad: "bg-red-500/15 text-red-600 dark:text-red-400",
    idle: "bg-zinc-500/15 text-zinc-500",
  }[tone];
  return <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${palette}`}>{label}</span>;
}

export function TeamsSettings() {
  const { t } = useTranslation();
  const { client, getToken, webuiCapabilities } = useClient();
  const enabled = webuiCapabilities.includes("teams.async.v1");
  const [snapshot, setSnapshot] = useState<TeamsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [projectFilter, setProjectFilter] = useState("");
  const [input, setInput] = useState("");
  const [botChoice, setBotChoice] = useState("");
  const [submitState, setSubmitState] = useState<string | null>(null);
  // F09: the operation key binds to the pending business draft. A lost or
  // failed response keeps the draft so a retry reuses the same key; only a
  // consumed submission or an edited draft mints a new one.
  const [draft, setDraft] = useState<{ input: string; bot: string; key: string } | null>(null);
  const [submitPending, setSubmitPending] = useState(false);
  // F08: the result body is fetched through the coordinator's public route,
  // never with bot service credentials.
  const [resultView, setResultView] = useState<TeamsTaskResult | null>(null);
  const [resultError, setResultError] = useState<string | null>(null);
  const [scaffold, setScaffold] = useState<TeamsScaffoldResult | null>(null);
  const [scaffoldId, setScaffoldId] = useState("");
  const [newProject, setNewProject] = useState("");

  const tx = useCallback(
    (key: string, fallback: string, vars?: Record<string, string>) =>
      t(key, { defaultValue: fallback, ...(vars ?? {}) }),
    [t],
  );

  const refresh = useCallback(async () => {
    if (!enabled) return;
    try {
      const data = await fetchTeamsSnapshot(getToken());
      setSnapshot(data);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  }, [enabled, getToken]);

  useEffect(() => {
    void refresh();
    if (!enabled) return;
    const timer = window.setInterval(() => void refresh(), 4000);
    return () => window.clearInterval(timer);
  }, [enabled, refresh]);

  const tasks = useMemo(() => {
    if (!snapshot) return [];
    return projectFilter
      ? snapshot.tasks.filter((task) => task.project_id === projectFilter)
      : snapshot.tasks;
  }, [snapshot, projectFilter]);

  if (!enabled) {
    return (
      <div className="settings-stack" data-teams-root>
        <p className="text-sm text-zinc-500">
          {tx("settings.teams.unavailable", "The async teams workbench is not enabled on this host.")}
        </p>
      </div>
    );
  }

  const operationKeyFor = (currentInput: string, currentBot: string): string => {
    if (draft && draft.input === currentInput && draft.bot === currentBot) {
      return draft.key;
    }
    const key = newOperationKey();
    setDraft({ input: currentInput, bot: currentBot, key });
    return key;
  };

  const showResult = async (taskId: string) => {
    setResultError(null);
    try {
      setResultView(await fetchTeamsTaskResult(getToken(), taskId));
    } catch (reason) {
      setResultView(null);
      setResultError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const submitTask = async () => {
    if (!input.trim() || !botChoice || submitPending) return;
    setSubmitPending(true);
    setSubmitState(tx("settings.teams.submitting", "Submitting..."));
    try {
      const task = await createTeamsTask(client, {
        project: projectFilter || snapshot?.projects[0]?.project_id || "default",
        bot: botChoice,
        input,
        operation_key: operationKeyFor(input, botChoice),
      });
      setDraft(null);
      setSubmitState(tx("settings.teams.submitted", "Submitted: {{id}}", { id: task.task_id }));
      setInput("");
      await refresh();
    } catch (reason) {
      // The draft (and its key) is kept: a retry resubmits the same business
      // operation instead of minting a second one.
      setSubmitState(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitPending(false);
    }
  };

  const doScaffold = async () => {
    if (!scaffoldId.trim()) return;
    try {
      const result = await scaffoldTeamsBot(client, { bot_id: scaffoldId.trim() });
      setScaffold(result);
      setScaffoldId("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  const addProject = async () => {
    if (!newProject.trim()) return;
    try {
      await createTeamsProject(client, newProject.trim(), newProject.trim());
      setNewProject("");
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    }
  };

  return (
    <div className="settings-stack" data-teams-root>
      <h2 className="text-lg font-semibold">{tx("settings.teams.title", "Teams workbench")}</h2>
      {error ? <p role="alert" className="text-sm text-red-500">{error}</p> : null}

      <div className="flex flex-wrap items-center gap-2">
        <label className="text-sm" htmlFor="teams-project-filter">
          {tx("settings.teams.project", "Project")}
        </label>
        <select
          id="teams-project-filter"
          value={projectFilter}
          onChange={(event) => setProjectFilter(event.target.value)}
          className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
        >
          <option value="">{tx("settings.teams.allProjects", "All projects")}</option>
          {snapshot?.projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>
              {project.display_name}
            </option>
          ))}
        </select>
        <input
          aria-label={tx("settings.teams.newProject", "New project id")}
          value={newProject}
          onChange={(event) => setNewProject(event.target.value)}
          placeholder={tx("settings.teams.newProject", "New project id")}
          className="w-40 rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
        />
        <button type="button" onClick={() => void addProject()} className="rounded border px-2 py-1 text-sm">
          {tx("settings.teams.createProject", "Create project")}
        </button>
        <button type="button" onClick={() => void refresh()} className="rounded border px-2 py-1 text-sm">
          {tx("settings.teams.refresh", "Refresh")}
        </button>
      </div>

      <section aria-label={tx("settings.teams.bots", "Bots")} className="rounded border p-3 dark:border-zinc-800">
        <h3 className="mb-2 font-medium">{tx("settings.teams.bots", "Bots")}</h3>
        <ul className="mb-2 space-y-1 text-sm">
          {snapshot?.bots.map((bot) => (
            <li key={bot.bot_id} data-bot-id={bot.bot_id}>
              <span className="font-medium">{bot.display_name}</span>{" "}
              <span className="text-zinc-400">{bot.endpoint}</span>{" "}
              {bot.instance_id ? <StateChip label={bot.instance_id.slice(0, 12)} tone="idle" /> : null}
            </li>
          ))}
          {!snapshot?.bots.length ? (
            <li className="text-zinc-400">{tx("settings.teams.noBots", "No bots registered yet.")}</li>
          ) : null}
        </ul>
        <div className="flex items-center gap-2">
          <input
            aria-label={tx("settings.teams.scaffoldId", "New bot id")}
            value={scaffoldId}
            onChange={(event) => setScaffoldId(event.target.value)}
            className="w-40 rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          />
          <button type="button" onClick={() => void doScaffold()} className="rounded border px-2 py-1 text-sm">
            {tx("settings.teams.scaffold", "Generate bot home")}
          </button>
        </div>
        {scaffold ? (
          <div data-scaffold-result className="mt-2 rounded bg-zinc-100 p-2 text-xs dark:bg-zinc-900">
            <p>{tx("settings.teams.scaffoldHint", "Start the bot manually with:")}</p>
            <code className="block whitespace-pre-wrap">{scaffold.start_command}</code>
            <p className="mt-1">
              {tx("settings.teams.scaffoldSecret", "Service key (shown once):")}{" "}
              <code data-scaffold-key>{scaffold.secret_env}={scaffold.api_key}</code>
            </p>
            <p className="text-zinc-400">
              {tx("settings.teams.notStarted", "The bot is not started yet.")}
            </p>
          </div>
        ) : null}
      </section>

      <section aria-label={tx("settings.teams.newTask", "New task")} className="rounded border p-3 dark:border-zinc-800">
        <h3 className="mb-2 font-medium">{tx("settings.teams.newTask", "New task")}</h3>
        <div className="flex flex-col gap-2 sm:flex-row">
          <select
            aria-label={tx("settings.teams.bot", "Bot")}
            value={botChoice}
            onChange={(event) => setBotChoice(event.target.value)}
            className="rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          >
            <option value="">{tx("settings.teams.chooseBot", "Choose a bot")}</option>
            {snapshot?.bots.map((bot) => (
              <option key={bot.bot_id} value={bot.bot_id}>{bot.display_name}</option>
            ))}
          </select>
          <textarea
            aria-label={tx("settings.teams.input", "Task input")}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            rows={2}
            className="flex-1 rounded border border-zinc-300 bg-transparent px-2 py-1 text-sm dark:border-zinc-700"
          />
          <button
            type="button"
            onClick={() => void submitTask()}
            disabled={!input.trim() || !botChoice || submitPending}
            className="self-start rounded border px-3 py-1 text-sm disabled:opacity-50"
          >
            {tx("settings.teams.submit", "Submit task")}
          </button>
        </div>
        {submitState ? <p className="mt-1 text-sm text-zinc-500" data-submit-state>{submitState}</p> : null}
      </section>

      <section aria-label={tx("settings.teams.tasks", "Tasks")} className="rounded border p-3 dark:border-zinc-800">
        <h3 className="mb-2 font-medium">{tx("settings.teams.tasks", "Tasks")}</h3>
        <table className="w-full text-left text-sm">
          <thead className="text-xs uppercase text-zinc-400">
            <tr>
              <th className="py-1">{tx("settings.teams.task", "Task")}</th>
              <th>{tx("settings.teams.dispatch", "Dispatch")}</th>
              <th>{tx("settings.teams.run", "Run")}</th>
              <th>{tx("settings.teams.cancel", "Cancel")}</th>
              <th>{tx("settings.teams.delivery", "Delivery")}</th>
              <th>{tx("settings.teams.result", "Result")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.task_id} data-task-id={task.task_id}>
                <td className="py-1 font-mono text-xs">{task.task_id.slice(0, 16)}…</td>
                <td><StateChip label={task.dispatch_state} tone={task.dispatch_state === "accepted" ? "ok" : task.dispatch_state === "indeterminate" ? "bad" : "idle"} /></td>
                <td><StateChip label={task.run_state} tone={task.run_state === "succeeded" ? "ok" : task.run_state === "failed" ? "bad" : "idle"} /></td>
                <td>
                  {task.cancel_state === "none" ? (
                    <span className="text-zinc-400">—</span>
                  ) : (
                    <StateChip
                      label={task.cancel_state}
                      tone={task.cancel_state === "confirmed" ? "ok" : task.cancel_state === "unconfirmed" ? "bad" : "warn"}
                    />
                  )}
                </td>
                <td><StateChip label={task.delivery_state} tone={task.delivery_state === "injected" ? "ok" : "idle"} /></td>
                <td>
                  {task.result_available ? (
                    <button
                      type="button"
                      data-result-view={task.task_id}
                      className="rounded border px-2 py-0.5 text-xs"
                      onClick={() => void showResult(task.task_id)}
                    >
                      {tx("settings.teams.viewResult", "View result")}
                    </button>
                  ) : (
                    <span className="text-zinc-400">—</span>
                  )}
                </td>
                <td>
                  {task.dispatch_state === "pending" || task.dispatch_state === "accepted" ? (
                    task.run_state !== "succeeded" && task.run_state !== "failed" && task.run_state !== "cancelled" && task.run_state !== "interrupted" ? (
                      <button
                        type="button"
                        className="rounded border px-2 py-0.5 text-xs"
                        onClick={async () => {
                          await cancelTeamsTask(client, task.task_id);
                          await refresh();
                        }}
                      >
                        {tx("settings.teams.cancelAction", "Request cancel")}
                      </button>
                    ) : null
                  ) : null}
                </td>
              </tr>
            ))}
            {!tasks.length ? (
              <tr><td colSpan={7} className="py-2 text-zinc-400">{tx("settings.teams.noTasks", "No tasks yet.")}</td></tr>
            ) : null}
          </tbody>
        </table>
        {resultError ? <p role="alert" className="mt-2 text-xs text-red-500">{resultError}</p> : null}
        {resultView ? (
          <div data-task-result className="mt-2 rounded bg-zinc-100 p-2 text-xs dark:bg-zinc-900">
            <p className="font-medium">
              {tx("settings.teams.resultFor", "Result of {{id}}", { id: resultView.task_id })}{" "}
              {resultView.result_truncated ? (
                <span>({tx("settings.teams.truncated", "truncated")})</span>
              ) : null}
            </p>
            <pre className="mt-1 whitespace-pre-wrap font-mono">{resultView.result_text}</pre>
            <button
              type="button"
              className="mt-1 rounded border px-2 py-0.5 text-xs"
              onClick={() => setResultView(null)}
            >
              {tx("settings.teams.closeResult", "Close")}
            </button>
          </div>
        ) : null}
        {snapshot && Object.keys(snapshot.quarantines).length ? (
          <p role="alert" className="mt-2 text-xs text-amber-500">
            {tx("settings.teams.quarantined", "Some instances are quarantined pending operator review.")}
          </p>
        ) : null}
      </section>
    </div>
  );
}
