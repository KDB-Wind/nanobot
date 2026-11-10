import asyncio, json, os, sys, tempfile
from pathlib import Path
from aiohttp.test_utils import TestServer
from nanobot_teams.coordinator.config import CoordinatorConfig
from nanobot_teams.coordinator.service import CoordinatorService
from nanobot_teams.coordinator.store import CoordinatorStore, utc_now
from nanobot_teams.coordinator.worker import ClientFactory, DispatchWorker
from repro_coordinator import FakeBot, admit

BASE = Path(__file__).parent
OLD_SOURCE = Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-pr16-review-e904936')


def out(**kw):
    print(json.dumps(kw), flush=True)


async def main():
    path = Path(tempfile.mkdtemp(prefix='upgrade-alias-', dir=BASE))
    fake = FakeBot()
    factory = ClientFactory({'KEY': 'synthetic'})
    async with TestServer(fake.app()) as server:
        endpoint = str(server.make_url('/')).rstrip('/')
        env = dict(
            os.environ,
            PYTHONPATH=str(OLD_SOURCE) + ';' + str(OLD_SOURCE / 'nanobot-teams/src'),
        )
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(BASE / 'old_import.py'), str(path), endpoint,
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        assert proc.returncode == 0, stderr.decode()
        print(stdout.decode().strip())

        store = CoordinatorStore(path / 'coord.sqlite3')
        store.open()
        guards = store.active_legacy_guards()
        out(probe='upgrade', source=__import__(
            'nanobot_teams.coordinator.store', fromlist=['x']).__file__,
            guards=len(guards),
            endpoint=guards[0].endpoint if guards else None,
            secret_env=guards[0].secret_env if guards else None,
            quarantines=list(store.active_quarantines()))
        assert len(guards) == 1
        assert guards[0].endpoint == endpoint
        assert guards[0].secret_env == 'KEY'
        assert guards[0].instance_id is None
        guard_key = guards[0].guard_key

        before_quota = store._conn.execute(
            'SELECT delegations_used, result_bytes_used, confirmed FROM caller_quotas '
            'WHERE caller_principal=?', ('planner',)
        ).fetchone()
        again = CoordinatorService(
            CoordinatorConfig(control_dir=path), store
        ).import_04_ledger(
            teams_config={'team_id': 'p'}, ledger_path=path / 'legacy.sqlite3',
            caller_id='planner',
        )
        after_quota = store._conn.execute(
            'SELECT delegations_used, result_bytes_used, confirmed FROM caller_quotas '
            'WHERE caller_principal=?', ('planner',)
        ).fetchone()
        out(probe='reimport', result=again, guards=len(store.active_legacy_guards()),
            quota_before=before_quota, quota_after=after_quota)
        assert again['imported'] is False
        assert len(store.active_legacy_guards()) == 1
        assert after_quota == before_quota

        store.close()
        store = CoordinatorStore(path / 'coord.sqlite3')
        store.open()
        reopened = store.active_legacy_guards()
        out(probe='reopen', guards=len(reopened),
            guard_key=reopened[0].guard_key if reopened else None,
            endpoint=reopened[0].endpoint if reopened else None,
            secret_env=reopened[0].secret_env if reopened else None)
        assert len(reopened) == 1
        assert reopened[0].guard_key == guard_key
        assert reopened[0].endpoint == endpoint
        assert reopened[0].secret_env == 'KEY'

        store.confirm_caller_quota(
            'planner', delegations_limit=100, result_bytes_limit=1000000,
            actor='operator',
        )
        bot = store.get_bot('alias')
        task = admit(store, bot)
        await DispatchWorker(store, factory).pump_once()
        blocked = store.get_task(task.task_id)
        out(probe='alias_before_resolve', dispatch_state=blocked.dispatch_state.value,
            alias_instance=store.get_bot('alias').instance_id,
            remote_submits=fake.submits,
            guards=len(store.active_legacy_guards()))
        assert fake.submits == 0
        assert store.get_bot('alias').instance_id is None
        assert blocked.dispatch_state.value == 'pending'

        store.resolve_quarantine(guard_key, 'operator', 'verified and cleared')
        entry = store.dispatch_entry_for_task(task.task_id)
        store.defer_dispatch(entry.entry_id, utc_now(), 'probe: retry after resolution')
        await DispatchWorker(store, factory).pump_once()
        resumed = store.get_task(task.task_id)
        out(probe='alias_after_resolve', dispatch_state=resumed.dispatch_state.value,
            alias_instance=store.get_bot('alias').instance_id,
            remote_submits=fake.submits,
            guards=len(store.active_legacy_guards()))
        assert fake.submits == 1
        assert store.get_bot('alias').instance_id == 'inst_a'
        assert resumed.dispatch_state.value == 'accepted'
        assert not store.active_legacy_guards()
        store.close()
    await factory.aclose()


asyncio.run(main())
