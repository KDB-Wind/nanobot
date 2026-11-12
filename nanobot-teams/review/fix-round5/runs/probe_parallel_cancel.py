import asyncio, json, sys, tempfile, uuid
from pathlib import Path
from datetime import timedelta

REPO = Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp')
sys.path.insert(0, str(REPO))
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import current_request_session_key
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.runs.executor import AgentLoopRunExecutor
from nanobot.runs.service import RunService
from nanobot.runs.store import RunStore
from nanobot.runs.types import RunSubmit, utc_now, InstanceIsolatedError
from nanobot.config.home import set_home_path
from loguru import logger
logger.remove()


def out(**kw):
    print(json.dumps(kw), flush=True)


async def until(check):
    for _ in range(2500):
        if check():
            return
        await asyncio.sleep(.01)
    raise RuntimeError('wait exhausted')


def req(name):
    return RunSubmit(name, 'wf', name, utc_now() + timedelta(seconds=90), str(uuid.uuid4()), 'p')


class Provider(LLMProvider):
    def __init__(self, resist_role):
        super().__init__(provider_name='offline-review')
        self.resist_role = resist_role
        self.roles = {}
        self.calls = {}
        self.child_entered = asyncio.Event()
        self.other_entered = asyncio.Event()
        self.release = asyncio.Event()

    def get_default_model(self):
        return 'offline-review'

    async def chat(self, messages, tools=None, **kw):
        root = any(t.get('function', {}).get('name') == 'subagent' for t in (tools or []))
        if not root:
            self.child_entered.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    pass
            return LLMResponse(content='child stopped')
        key = current_request_session_key()
        role = self.roles.get(key)
        self.calls[key] = self.calls.get(key, 0) + 1
        if role == self.resist_role:
            if self.calls[key] == 1:
                return LLMResponse(
                    content=None,
                    tool_calls=[ToolCallRequest('child', 'subagent', {
                        'action': 'create', 'task': 'resist cancellation',
                    })],
                )
            await asyncio.Event().wait()
        self.other_entered.set()
        await asyncio.Event().wait()

    async def chat_stream(self, messages, tools=None, **kw):
        return await self.chat(messages, tools=tools, **kw)


async def run_order(resist_role):
    other_role = 'b' if resist_role == 'a' else 'a'
    with tempfile.TemporaryDirectory(prefix=f'parallel-{resist_role}-', dir=Path(__file__).parent) as d:
        root = Path(d)
        set_home_path(root / 'home')
        p = Provider(resist_role)
        loop = AgentLoop(bus=MessageBus(), provider=p, workspace=root / 'workspace', max_iterations=3)
        reap_started = asyncio.Event()
        stop_orig = loop.stop_session_work

        async def observe_stop(key):
            if p.roles.get(key) == resist_role:
                reap_started.set()
            report = await stop_orig(key)
            out(probe='typed_stop_report', order=resist_role, role=p.roles.get(key),
                confirmed=report.confirmed, live_subagents=report.live_subagents)
            return report

        loop.stop_session_work = observe_stop
        s = RunStore(root / 'runs.sqlite3')
        s.open()
        v = RunService(s, AgentLoopRunExecutor(loop), max_active=2, cancel_wait_s=.05)
        try:
            await v.start()
            runs = {}
            for name in ('a', 'b'):
                runs[name] = (await v.submit(req(name))).run
                p.roles['run:' + runs[name].run_id] = name
            await asyncio.wait_for(p.child_entered.wait(), 10)
            await asyncio.wait_for(p.other_entered.wait(), 10)

            await v.cancel(runs[resist_role].run_id)
            await asyncio.wait_for(reap_started.wait(), 10)
            await v.cancel(runs[other_role].run_id)
            await until(lambda: s.get(runs[other_role].run_id).terminal)
            other_final = s.get(runs[other_role].run_id)
            out(probe='cooperative_final', order=resist_role, state=other_final.state,
                stop_confirmed=other_final.stop_confirmed,
                effects_uncertain=other_final.effects_uncertain,
                isolation=s.isolation()[0])
            assert other_final.stop_confirmed is True
            assert other_final.effects_uncertain is False
            assert s.isolation()[0] is False

            await until(lambda: s.get(runs[resist_role].run_id).terminal)
            final = s.get(runs[resist_role].run_id)
            live = loop.subagents.get_running_count_by_session('run:' + final.run_id)
            out(probe='resisting_final', order=resist_role, state=final.state,
                stop_confirmed=final.stop_confirmed,
                effects_uncertain=final.effects_uncertain,
                isolation=s.isolation()[0], live_owned_subagents=live)
            assert final.stop_confirmed is False
            assert final.effects_uncertain is True
            assert s.isolation()[0] is True
            assert live > 0

            try:
                nxt = (await v.submit(req('c'))).run
            except InstanceIsolatedError:
                next_state = 'blocked_by_isolation'
            else:
                await until(lambda: s.get(nxt.run_id).terminal)
                next_state = s.get(nxt.run_id).state
            out(probe='next_run', order=resist_role, state=next_state,
                prior_child_alive=loop.subagents.get_running_count_by_session('run:' + final.run_id) > 0)
            assert next_state == 'blocked_by_isolation'
        finally:
            p.release.set()
            await asyncio.sleep(.1)
            await v.aclose()
            await loop.aclose()
            s.close()


async def main():
    out(probe='source', loop=__import__('nanobot.agent.loop', fromlist=['']).__file__,
        executor=__import__('nanobot.runs.executor', fromlist=['']).__file__)
    for order in ('a', 'b'):
        await run_order(order)


asyncio.run(main())
