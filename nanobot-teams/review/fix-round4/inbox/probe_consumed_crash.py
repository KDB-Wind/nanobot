import asyncio
import json
import subprocess
import sys
from pathlib import Path

from prior_probe import (
    ROOT, SessionManager, ExternalInbox, MessageBus, RecoveryCoordinator,
    InboxWakePump, setup, applied, exists, agent_for,
)

async def child_after_consumption():
    tmp, manager, state, key, session, inbox, cap = await setup('consume-crash')
    bus = MessageBus()
    await applied(inbox, cap)
    agent, provider = agent_for(tmp, manager, inbox, bus)
    reached = asyncio.Event()

    async def hold_before_restore(ctx):
        persisted = await state.read(key)
        print(json.dumps({
            'tmp': str(tmp), 'key': key,
            'due_after_consumption': inbox.wake_due_deliveries(),
            'provider_calls': len(provider.calls),
            'checkpoint': persisted.metadata.get('runtime_checkpoint'),
            'recovery': persisted.metadata.get('webui_recovery'),
            'pending_user_turn': persisted.metadata.get('pending_user_turn'),
        }), flush=True)
        reached.set()
        await asyncio.Event().wait()

    agent._restore_turn = hold_before_restore
    runner = asyncio.create_task(agent.run())
    await InboxWakePump(inbox, bus.publish_inbound, lambda k: exists(state, k)).pump_once()
    await asyncio.wait_for(reached.wait(), 10)
    await runner

async def parent():
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__)), 'child'], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 15)
        before = json.loads(line)
    finally:
        if proc.poll() is None:
            proc.kill()
        await asyncio.to_thread(proc.wait)

    tmp = Path(before['tmp'])
    manager = SessionManager(tmp / 'workspace')
    state = manager.state
    inbox = ExternalInbox(state)
    bus = MessageBus()
    await RecoveryCoordinator(manager, bus).scan()
    pending = inbox.pending_deliveries()
    for delivery in pending:
        await inbox.apply_pending(delivery)
    recovered = await InboxWakePump(inbox, bus.publish_inbound, lambda k: exists(state, k)).pump_once()
    agent, provider = agent_for(tmp, manager, inbox, bus)
    runner = asyncio.create_task(agent.run())
    try:
        await asyncio.sleep(.15)
        session = await state.read(before['key'])
        result = {
            'mode': 'crash_after_wake_consumed_before_restore', 'before': before,
            'child_exit': proc.returncode, 'pending_after_restart': pending,
            'recovered_wakes': recovered, 'provider_calls': len(provider.calls),
            'receipt': inbox.get_receipt('del-1').state,
            'recovery': session.metadata.get('webui_recovery'),
            'checkpoint': session.metadata.get('runtime_checkpoint'),
            'pending_user_turn': session.metadata.get('pending_user_turn'),
        }
        print(json.dumps(result), flush=True)
        assert before['due_after_consumption'] == [] and before['provider_calls'] == 0
        assert before['pending_user_turn'] is True
        assert pending == [] and recovered == 0 and len(provider.calls) == 0
        assert result['recovery'] is not None or result['pending_user_turn'] is True
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        await agent.aclose()

asyncio.run(child_after_consumption() if len(sys.argv) > 1 else parent())
