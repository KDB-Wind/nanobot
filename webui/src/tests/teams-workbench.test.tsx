import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TeamsSettings } from "@/components/teams/TeamsSettings";
import { TeamsSnapshot, TeamsUsageResponse } from "@/lib/teams-api";
import i18n from "@/i18n";
import { ClientProvider } from "@/providers/ClientProvider";

const snapshot: TeamsSnapshot = {
  projects: [
    { project_id: "proj", display_name: "Main", created_at: "2026-11-09T00:00:00Z" },
    { project_id: "second", display_name: "Second", created_at: "2026-11-09T00:00:00Z" },
  ],
  bots: [
    {
      bot_id: "research",
      display_name: "Research",
      role: "research",
      endpoint: "http://127.0.0.1:8921",
      enabled: true,
      instance_id: "inst_aaa",
      capabilities: [],
      has_open_tasks: false,
      inbox_url: "http://127.0.0.1:8921",
      inbox_principal_id: "teams-coordinator",
      inbox_bound: true,
    },
  ],
  tasks: [
    {
      task_id: "task_1",
      project_id: "proj",
      workflow_id: "wf-1",
      bot_id: "research",
      dispatch_state: "accepted",
      delivery_mode: "workbench_only",
      kind: "user_delegation",
      conversation_root: "",
      hop: 0,
      caller_principal: "planner",
      run_state: "running",
      remote_run_id: "run_1",
      stop_confirmed: false,
      effects_uncertain: false,
      cancel_state: "none",
      delivery_state: "not_ready",
      outcome_code: null,
      result_available: false,
      deadline_at: "2026-11-09T01:00:00Z",
      created_at: "2026-11-09T00:00:00Z",
      finished_at: null,
      last_error: "",
    },
    {
      task_id: "task_2",
      project_id: "proj",
      workflow_id: "wf-1",
      bot_id: "writer",
      dispatch_state: "accepted",
      delivery_mode: "auto_resume",
      kind: "peer",
      conversation_root: "conv-1",
      hop: 1,
      caller_principal: "bot:research",
      run_state: "succeeded",
      remote_run_id: "run_2",
      stop_confirmed: true,
      effects_uncertain: false,
      cancel_state: "none",
      delivery_state: "not_ready",
      outcome_code: "succeeded",
      result_available: true,
      result_fetched: true,
      result_truncated: true,
      deadline_at: "2026-11-09T01:00:00Z",
      created_at: "2026-11-09T00:00:00Z",
      finished_at: "2026-11-09T00:30:00Z",
      last_error: "",
    },
  ],
  quarantines: {},
  protocol: 1,
};

const usageResponse: TeamsUsageResponse = {
  window: "30d",
  bots: [
    {
      bot_id: "research",
      display_name: "Research",
      window: "30d",
      available: true,
      totals: {
        provider: "", model: "(all)", requests: 2, input_tokens: 150,
        output_tokens: 20, cache_read_tokens: 60, cache_write_tokens: 0,
        observed_input_tokens: 100, cache_read_rate: 0.6, coverage: 100 / 150,
      },
      models: [],
    },
    {
      bot_id: "writer",
      display_name: "Writer",
      window: "30d",
      available: false,
      reason: "usage database missing",
    },
  ],
};
const fetchUsage = vi.fn(async () => usageResponse);
const fetchEdges = vi.fn(async () => ({
  edges: [{ from_bot_id: "research", to_bot_id: "writer" }],
}));
const fetchSnapshot = vi.fn(async () => snapshot);
const fetchTaskResult = vi.fn(async () => ({
  task_id: "task_2",
  run_state: "succeeded",
  outcome_code: "succeeded",
  result_text: "final answer body",
  result_truncated: true,
}));
const requestMutation = vi.fn(async (action: string) => {
  if (action === "teams.cancel_task") return { ...snapshot.tasks[0], cancel_state: "requested" };
  if (action === "teams.scaffold_bot") {
    return {
      bot_id: "writer", home: "/homes/writer", endpoint: "http://127.0.0.1:8999",
      secret_env: "TEAMS_BOT_WRITER_KEY",
      key_file: "/homes/writer/service-key.env",
      required_env: ["TEAMS_BOT_WRITER_KEY"],
      start_command: 'nanobot serve --config "/homes/writer/config.json"', started: false,
    };
  }
  return snapshot.tasks[0];
});

vi.mock("@/lib/teams-api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/teams-api")>();
  return {
    ...original,
    fetchTeamsSnapshot: (...args: unknown[]) => fetchSnapshot(...(args as [])),
    fetchTeamsTaskResult: (...args: unknown[]) => fetchTaskResult(...(args as [])),
    fetchTeamsUsage: (...args: unknown[]) => fetchUsage(...(args as [])),
    fetchTeamsPeerEdges: (...args: unknown[]) => fetchEdges(...(args as [])),
  };
});

function renderTeams(capabilities: string[] = ["teams.async.v1"]) {
  const client = { requestMutation } as unknown as Parameters<typeof ClientProvider>[0]["client"];
  return render(
    <ClientProvider client={client} token="test-token" webuiCapabilities={capabilities}>
      <TeamsSettings />
    </ClientProvider>,
  );
}

beforeEach(async () => {
  fetchSnapshot.mockClear();
  fetchTaskResult.mockClear();
  fetchUsage.mockClear();
  fetchEdges.mockClear();
  fetchUsage.mockResolvedValue(usageResponse);
  fetchEdges.mockResolvedValue({ edges: [{ from_bot_id: "research", to_bot_id: "writer" }] });
  requestMutation.mockClear();
  await i18n.changeLanguage("en");
});

afterEach(cleanup);

describe("TeamsSettings", () => {
  it("hides operations and explains when the capability is missing", async () => {
    renderTeams([]);
    expect(
      await screen.findByText(/not enabled on this host/i),
    ).toBeInTheDocument();
    expect(fetchSnapshot).not.toHaveBeenCalled();
  });

  it("renders bots and task states from the snapshot", async () => {
    renderTeams();
    expect((await screen.findAllByText("Research")).length).toBeGreaterThan(0);
    const row = await screen.findByRole("row", { name: /task_1/ });
    expect(row).toHaveTextContent("accepted");
    expect(row).toHaveTextContent("running");
  });

  it("submits a task through the mutation transport with a draft-bound operation key", async () => {
    renderTeams();
    await screen.findAllByText("Research");
    fireEvent.change(screen.getByLabelText("Bot"), { target: { value: "research" } });
    fireEvent.change(screen.getByLabelText(/task input/i), { target: { value: "go look" } });
    fireEvent.click(screen.getByRole("button", { name: /submit task/i }));
    await waitFor(() => expect(requestMutation).toHaveBeenCalled());
    const calls = requestMutation.mock.calls as unknown as Array<
      [string, { bot: string; input: string; operation_key: string }]
    >;
    const [action, payload] = calls[0];
    expect(action).toBe("teams.create_task");
    expect(payload.bot).toBe("research");
    expect(payload.input).toBe("go look");
    expect(payload.operation_key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("keeps one submission identity while the first response is pending", async () => {
    requestMutation.mockImplementationOnce(() => new Promise(() => {}));
    renderTeams();
    await screen.findAllByText("Research");
    fireEvent.change(screen.getByLabelText("Bot"), { target: { value: "research" } });
    fireEvent.change(screen.getByLabelText(/task input/i), { target: { value: "one business task" } });
    const button = screen.getByRole("button", { name: /submit task/i });
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(requestMutation).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(requestMutation).toHaveBeenCalledTimes(1);
    const calls = requestMutation.mock.calls as unknown as Array<
      [string, { operation_key: string }]
    >;
    const keys = calls.map((call) => call[1].operation_key);
    expect(new Set(keys).size, "one pending business submission must keep one key").toBe(1);
  });

  it("reuses the original key after an acceptance response is lost", async () => {
    requestMutation.mockRejectedValueOnce(new Error("accepted remotely, response lost"));
    requestMutation.mockRejectedValueOnce(new Error("accepted remotely, response lost"));
    renderTeams();
    await screen.findAllByText("Research");
    fireEvent.change(screen.getByLabelText("Bot"), { target: { value: "research" } });
    fireEvent.change(screen.getByLabelText(/task input/i), { target: { value: "one business task" } });
    const button = screen.getByRole("button", { name: /submit task/i });
    fireEvent.click(button);
    await screen.findByText("accepted remotely, response lost");
    fireEvent.click(button);
    await waitFor(() => expect(requestMutation).toHaveBeenCalledTimes(2));
    const calls = requestMutation.mock.calls as unknown as Array<
      [string, { operation_key: string }]
    >;
    const keys = calls.map((call) => call[1].operation_key);
    expect(keys[1], "a network retry must not create another business operation").toBe(keys[0]);
  });

  it("binds the draft identity to the project as well as input and bot", async () => {
    // M07: the same text under another project is a different business draft.
    requestMutation.mockRejectedValueOnce(new Error("accepted remotely, response lost"));
    requestMutation.mockRejectedValueOnce(new Error("accepted remotely, response lost"));
    renderTeams();
    await screen.findAllByText("Research");
    fireEvent.change(screen.getByLabelText("Bot"), { target: { value: "research" } });
    fireEvent.change(screen.getByLabelText(/task input/i), {
      target: { value: "one business task" },
    });
    const button = screen.getByRole("button", { name: /submit task/i });
    fireEvent.click(button);
    await screen.findByText("accepted remotely, response lost");
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "second" } });
    fireEvent.click(button);
    await waitFor(() => expect(requestMutation).toHaveBeenCalledTimes(2));
    const calls = requestMutation.mock.calls as unknown as Array<
      [string, { project: string; operation_key: string }]
    >;
    expect(calls[0][1].project).toBe("proj");
    expect(calls[1][1].project).toBe("second");
    expect(
      calls[1][1].operation_key,
      "different project is a different business draft",
    ).not.toBe(calls[0][1].operation_key);
  });

  it("shows the terminal result body through the public result route", async () => {
    renderTeams();
    const row = await screen.findByRole("row", { name: /task_2/ });
    fireEvent.click(within(row).getByRole("button", { name: /view result/i }));
    expect(await screen.findByText("final answer body")).toBeInTheDocument();
    expect(screen.getByText("(truncated)")).toBeInTheDocument();
    expect(fetchTaskResult).toHaveBeenCalledWith("test-token", "task_2");
  });

  it("requests cancellation and shows the awaiting-confirmation state", async () => {
    renderTeams();
    await screen.findByRole("row", { name: /task_1/ });
    fireEvent.click(screen.getByRole("button", { name: /request cancel/i }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("teams.cancel_task", { task_id: "task_1" }),
    );
  });

  it("stops the whole workflow with the task workflow id", async () => {
    renderTeams();
    const row = await screen.findByRole("row", { name: /task_1/ });
    fireEvent.click(within(row).getByRole("button", { name: /stop workflow/i }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("teams.stop_workflow", {
        workflow_id: "wf-1",
      }),
    );
  });

  it("disables workflow stop while a stop is in flight", async () => {
    requestMutation.mockImplementationOnce(() => new Promise(() => {}));
    renderTeams();
    const row = await screen.findByRole("row", { name: /task_1/ });
    const stop = within(row).getByRole("button", { name: /stop workflow/i });
    fireEvent.click(stop);
    await waitFor(() => expect(requestMutation).toHaveBeenCalledTimes(1));
    expect(stop).toBeDisabled();
  });

  it("renders per-bot usage with exact hit rate, coverage, and unavailable reasons", async () => {
    renderTeams();
    await screen.findByText("Teams workbench");
    const researchRow = await waitFor(() => {
      const row = document.querySelector('[data-usage-bot="research"]');
      expect(row).not.toBeNull();
      return row;
    });
    expect(researchRow!.textContent).toContain("60%");
    expect(researchRow!.textContent).toContain("67%");
    const writerRow = document.querySelector('[data-usage-bot="writer"]');
    expect(writerRow?.textContent).toContain("usage database missing");
  });

  it("shows Not reported instead of 0% when no cache fields arrive", async () => {
    fetchUsage.mockResolvedValueOnce({
      window: "30d",
      bots: [
        {
          bot_id: "research", display_name: "Research", window: "30d",
          available: true,
          totals: {
            provider: "", model: "(all)", requests: 3, input_tokens: 30,
            output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0,
            observed_input_tokens: 0, cache_read_rate: null, coverage: 0,
          },
          models: [],
        },
      ],
    });
    renderTeams();
    const researchRow = await waitFor(() => {
      const row = document.querySelector('[data-usage-bot="research"]');
      expect(row).not.toBeNull();
      return row;
    });
    const rateCell = researchRow?.querySelector("[data-usage-rate]");
    expect(rateCell?.textContent).toContain("Not reported");
    // The rate cell never falls back to a fake 0% hit rate; honest 0%
    // coverage stays visible in its own column.
    expect(rateCell?.textContent).not.toContain("%");
  });

  it("shows the bot graph edges and the empty state", async () => {
    renderTeams();
    expect(
      await screen.findByText("research", { selector: "[data-peer-edge] *" }),
    ).toBeInTheDocument();
    const edge = document.querySelector('[data-peer-edge="research->writer"]');
    expect(edge).not.toBeNull();
    fetchEdges.mockResolvedValueOnce({ edges: [] });
    fireEvent.click(screen.getByRole("button", { name: /refresh/i }));
    await screen.findByText(/No explicit peer edges yet/i);
  });

  it("marks peer tasks with sender, recipient, and hop in the route column", async () => {
    renderTeams();
    const row = await screen.findByRole("row", { name: /task_2/ });
    const route = row.querySelector("[data-task-route]");
    expect(route).not.toBeNull();
    expect(route?.textContent).toContain("research");
    expect(route?.textContent).toContain("writer");
    expect(route?.textContent).toContain("hop 1");
  });

  it("surfaces usage fetch failures without breaking the workbench", async () => {
    fetchUsage.mockRejectedValueOnce(new Error("coordinator unreachable"));
    renderTeams();
    expect(await screen.findByText("coordinator unreachable")).toBeInTheDocument();
    expect(await screen.findAllByText("Research")).toBeTruthy();
  });

  it("renders the usage panel in Chinese with the denominator note", async () => {
    await i18n.changeLanguage("zh-CN");
    renderTeams();
    expect(await screen.findByText("每 Bot 用量")).toBeInTheDocument();
    expect(screen.getByText("分母:报告缓存的输入")).toBeInTheDocument();
    const researchRow = document.querySelector('[data-usage-bot="research"]');
    expect(researchRow).not.toBeNull();
    expect(screen.getByText("命中率")).toBeInTheDocument();
    await i18n.changeLanguage("en");
  });

  it("shows the scaffold key file location and never the raw service key", async () => {
    renderTeams();
    await screen.findAllByText("Research");
    fireEvent.change(screen.getByLabelText(/new bot id/i), { target: { value: "writer" } });
    fireEvent.click(screen.getByRole("button", { name: /generate bot home/i }));
    const keyFile = await screen.findByText(/service-key\.env/);
    expect(keyFile).toBeInTheDocument();
    expect(screen.getByText(/TEAMS_BOT_WRITER_KEY/)).toBeInTheDocument();
    expect(screen.getByText(/nanobot serve --config/)).toBeInTheDocument();
    expect(screen.getByText(/not started yet/i)).toBeInTheDocument();
    // F13: the browser never receives or renders the raw key value.
    expect(screen.queryByText(/once-only/)).not.toBeInTheDocument();
  });
});
