import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TeamsSettings } from '@/components/teams/TeamsSettings';
import { ClientProvider } from '@/providers/ClientProvider';
import i18n from '@/i18n';

const state = vi.hoisted(() => ({
  mutation: vi.fn(),
  snapshot: {
    protocol: 1, quarantines: {}, tasks: [],
    projects: [{ project_id: 'proj', display_name: 'Main', created_at: '' }],
    bots: [{ bot_id: 'research', display_name: 'Research', role: 'research',
      endpoint: 'http://127.0.0.1:8921', enabled: true, instance_id: 'inst_x',
      capabilities: [], has_open_tasks: false }],
  },
}));
vi.mock('@/lib/teams-api', async (original) => ({
  ...await original<typeof import('@/lib/teams-api')>(),
  fetchTeamsSnapshot: vi.fn(async () => state.snapshot),
}));

async function fillTask() {
  render(<ClientProvider client={{requestMutation: state.mutation} as never}
    token="synthetic" webuiCapabilities={['teams.async.v1']}>
    <TeamsSettings />
  </ClientProvider>);
  await screen.findAllByText('Research');
  fireEvent.change(screen.getByLabelText('Bot'), {target: {value: 'research'}});
  fireEvent.change(screen.getByLabelText(/task input/i), {target: {value: 'one business task'}});
  return screen.getByRole('button', {name: /submit task/i});
}

beforeEach(async () => {
  state.mutation.mockReset();
  await i18n.changeLanguage('en');
});
afterEach(cleanup);

describe('Async workbench submission contract', () => {
  it('keeps one submission identity while the first response is pending', async () => {
    state.mutation.mockImplementation(() => new Promise(() => {}));
    const button = await fillTask();
    fireEvent.click(button);
    fireEvent.click(button);
    await waitFor(() => expect(state.mutation.mock.calls.length).toBeGreaterThan(0));
    const keys = state.mutation.mock.calls.map(call => call[1].operation_key);
    console.log('PENDING_SUBMISSIONS', JSON.stringify({calls: keys.length, distinctKeys: new Set(keys).size}));
    expect(new Set(keys).size, 'one pending business submission must keep one key').toBe(1);
  });

  it('reuses the original key after an acceptance response is lost', async () => {
    state.mutation.mockRejectedValue(new Error('accepted remotely, response lost'));
    const button = await fillTask();
    fireEvent.click(button);
    await screen.findByText('accepted remotely, response lost');
    fireEvent.click(button);
    await waitFor(() => expect(state.mutation).toHaveBeenCalledTimes(2));
    const keys = state.mutation.mock.calls.map(call => call[1].operation_key);
    console.log('RETRY_SUBMISSIONS', JSON.stringify({calls: keys.length, distinctKeys: new Set(keys).size}));
    expect(keys[1], 'a network retry must not create another business operation').toBe(keys[0]);
  });
});


it('binds the draft identity to the project as well as input and bot', async () => {
  state.snapshot.projects.push({project_id: 'second', display_name: 'Second', created_at: ''});
  try {
    state.mutation.mockRejectedValue(new Error('accepted remotely, response lost'));
    const button = await fillTask();
    fireEvent.click(button);
    await screen.findByText('accepted remotely, response lost');
    fireEvent.change(screen.getByLabelText('Project'), {target: {value: 'second'}});
    fireEvent.click(button);
    await waitFor(() => expect(state.mutation).toHaveBeenCalledTimes(2));
    const calls=state.mutation.mock.calls;
    console.log('PROJECT_IDENTITY',JSON.stringify({
      projects:calls.map(call=>call[1].project),
      distinctKeys:new Set(calls.map(call=>call[1].operation_key)).size,
    }));
    expect(calls[1][1].operation_key, 'different project is a different business draft').not.toBe(calls[0][1].operation_key);
  } finally {
    state.snapshot.projects.pop();
  }
});
