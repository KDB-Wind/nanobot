import asyncio,json,sys,tempfile
from pathlib import Path
ROOT=Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp')
sys.path[:0]=[str(ROOT/'nanobot-teams/src'),str(ROOT/'nanobot-teams/review/async-20261109/coordinator')]
from aiohttp.test_utils import TestServer
from repro_coordinator import FakeBot,admit
from nanobot_teams.coordinator.store import CoordinatorStore
from nanobot_teams.coordinator.worker import DispatchWorker,ClientFactory
from nanobot_teams.coordinator.models import Bot
async def main():
    path=Path(tempfile.mkdtemp(prefix='repeat-quarantine-',dir=Path(__file__).parent));store=CoordinatorStore(path/'coordinator.sqlite3');store.open();store.create_project('p','P');fake=FakeBot();fake.uncertain=True;factory=ClientFactory({'KEY':'synthetic'})
    async with TestServer(fake.app()) as server:
        bot=Bot('a','A','r',str(server.make_url('/')).rstrip('/'),'KEY',instance_id='inst_a');store.upsert_bot(bot);worker=DispatchWorker(store,factory)
        first=admit(store,bot);await worker.pump_once();first_isolated='inst_a' in store.active_quarantines();store.resolve_quarantine('inst_a','operator','first incident investigated')
        second=admit(store,bot);await worker.pump_once();new_isolated='inst_a' in store.active_quarantines();third=admit(store,bot)
        print(json.dumps(dict(mode='repeat_quarantine',first_isolated=first_isolated,second_effects_uncertain=store.get_task(second.task_id).effects_uncertain,second_stop_confirmed=store.get_task(second.task_id).stop_confirmed,second_isolated=new_isolated,third_task_admitted=bool(third),remote_submits=fake.submits)))
    await factory.aclose();store.close()
asyncio.run(main())
