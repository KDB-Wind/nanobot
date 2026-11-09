import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TeamsSettings } from "@/components/teams/TeamsSettings";
import { TeamsSnapshot } from "@/lib/teams-api";
import i18n from "@/i18n";
import { ClientProvider } from "@/providers/ClientProvider";

const snapshot: TeamsSnapshot = {
  projects: [{ project_id: "proj", display_name: "Main", created_at: "2026-11-09T00:00:00Z" }],
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
    },
  ],
  tasks: [
    {
      task_id: "task_1",
      project_id: "proj",
      workflow_id: "wf-1",
      bot_id: "research",
      dispatch_state: "accepted",
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
  ],
  quarantines: {},
  protocol: 1,
};

const fetchSnapshot = vi.fn(async () => snapshot);
const requestMutation = vi.fn(async (action: string) => {
  if (action === "teams.cancel_task") return { ...snapshot.tasks[0], cancel_state: "requested" };
  if (action === "teams.scaffold_bot") {
    return {
      bot_id: "writer", home: "/homes/writer", endpoint: "http://127.0.0.1:8999",
      secret_env: "TEAMS_BOT_WRITER_KEY", api_key: "once-only",
      start_command: "nanobot serve --config /homes/writer/nanobot.toml", started: false,
    };
  }
  return snapshot.tasks[0];
});

vi.mock("@/lib/teams-api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/teams-api")>();
  return { ...original, fetchTeamsSnapshot: (...args: unknown[]) => fetchSnapshot(...(args as [])) };
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

  it("submits a task through the mutation transport with a fresh operation key", async () => {
    renderTeams();
    await screen.findAllByText("Research");
    fireEvent.change(screen.getByLabelText("Bot"), { target: { value: "research" } });
    fireEvent.change(screen.getByLabelText(/task input/i), { target: { value: "go look" } });
    fireEvent.click(screen.getByRole("button", { name: /submit task/i }));
    await waitFor(() => expect(requestMutation).toHaveBeenCalled());
    const [action, payload] = requestMutation.mock.calls[0];
    expect(action).toBe("teams.create_task");
    expect(payload.bot).toBe("research");
    expect(payload.input).toBe("go look");
    expect(payload.operation_key).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("requests cancellation and shows the awaiting-confirmation state", async () => {
    renderTeams();
    await screen.findByRole("row", { name: /task_1/ });
    fireEvent.click(screen.getByRole("button", { name: /request cancel/i }));
    await waitFor(() =>
      expect(requestMutation).toHaveBeenCalledWith("teams.cancel_task", { task_id: "task_1" }),
    );
  });

  it("shows scaffold result with manual start command and one-time key", async () => {
    renderTeams();
    await screen.findAllByText("Research");
    fireEvent.change(screen.getByLabelText(/new bot id/i), { target: { value: "writer" } });
    fireEvent.click(screen.getByRole("button", { name: /generate bot home/i }));
    const key = await screen.findByText(/TEAMS_BOT_WRITER_KEY=once-only/);
    expect(key).toBeInTheDocument();
    expect(screen.getByText(/nanobot serve --config/)).toBeInTheDocument();
    expect(screen.getByText(/not started yet/i)).toBeInTheDocument();
  });
});
