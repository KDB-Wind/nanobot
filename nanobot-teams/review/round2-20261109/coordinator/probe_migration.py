import asyncio,json,sys,tempfile
from dataclasses import replace
from pathlib import Path
from aiohttp.test_utils import TestServer,TestClient
ROOT=Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp')
sys.path[:0]=[str(ROOT/'nanobot-teams/src'),str(ROOT/'nanobot-teams/review/async-20261109/coordinator')]
from repro_coordinator import FakeBot,admit
from nanobot_teams.coordinator.store import CoordinatorStore,utc_now
from nanobot_teams.coordinator.worker import DispatchWorker,ClientFactory
from nanobot_teams.coordinator.config import CoordinatorConfig
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.service import CoordinatorService
from nanobot_teams.ledger import Ledger,TaskRecord
from nanobot_teams.config import Budget
BASE=Path(__file__).parent

def oldrecord(endpoint):return TaskRecord(task_id='old_unknown',root_task_id=None,parent_task_id=None,caller='planner',target='research',hop=0,status='outcome_unknown',error_code=None,truncated=False,http_sent=True,request_bytes=10,response_bytes=0,result_bytes=0,started_at=utc_now().isoformat(),finished_at=None,duration_ms=None,team_id='p',endpoint=endpoint)
async def unbound():
    path=Path(tempfile.mkdtemp(prefix='migration-unbound-',dir=BASE));store=CoordinatorStore(path/'coordinator.sqlite3');store.open();store.create_project('p','P');factory=ClientFactory({'KEY':'synthetic'});fake=FakeBot()
    async with TestServer(fake.app()) as server:
        endpoint=str(server.make_url('/')).rstrip('/');config=CoordinatorConfig(control_dir=path)
        async with TestClient(TestServer(build_app(store,config,{'operator':'operator-token','planner':'planner-token'}))) as client:
            resp=await client.post('/v1/teams/bots',headers={'Authorization':'Bearer operator-token'},json={'bot_id':'research','endpoint':endpoint,'secret_env':'KEY'});assert resp.status==201
            before=store.get_bot('research');ledger=Ledger(path/'legacy.sqlite3');ledger.admit(oldrecord(endpoint),Budget(delegations=1,result_bytes=32768),32768,60,continuing=False);ledger.close()
            service=CoordinatorService(config,store);report=service.import_04_ledger(teams_config={'team_id':'p'},ledger_path=path/'legacy.sqlite3',caller_id='planner')
            resp=await client.post('/v1/teams/quota/planner/confirm',headers={'Authorization':'Bearer operator-token'},json={'delegations':10,'result_bytes':100000});assert resp.status==200
            resp=await client.post('/v1/teams/tasks',headers={'Authorization':'Bearer planner-token'},json={'project':'p','bot':'research','operation_key':'11111111-1111-4111-8111-111111111111','input':'synthetic'});body=await resp.json();await DispatchWorker(store,factory).pump_once()
            print(json.dumps(dict(mode='migration_registered_via_http',registered_instance=before.instance_id,unknown_isolated=report['unknown_isolated'],quarantines=list(store.active_quarantines()),admission_status=resp.status,remote_submits=fake.submits,bound_instance_after_dispatch=store.get_bot('research').instance_id)))
    await factory.aclose();store.close()
asyncio.run(unbound())
