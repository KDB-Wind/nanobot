import asyncio, json, os, sys, tempfile, uuid
from pathlib import Path
from dataclasses import replace
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
import nanobot_teams.coordinator.store as store_module
import nanobot_teams.coordinator.worker as worker_module
from nanobot_teams.coordinator.store import CoordinatorStore, utc_now, AdmissionError
from nanobot_teams.coordinator.worker import DispatchWorker, ClientFactory
from nanobot_teams.coordinator.config import CoordinatorConfig, PlannerBinding
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.service import CoordinatorService
from nanobot_teams.coordinator.models import Bot
from nanobot_teams.ledger import Ledger, TaskRecord
from nanobot_teams.config import Budget
import repro_coordinator as helper
from repro_coordinator import admit, FakeBot
BASE = Path(__file__).parent
helper.ROOT = BASE
EXPECTED = Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp')
assert Path(store_module.__file__).is_relative_to(EXPECTED)
assert Path(worker_module.__file__).is_relative_to(EXPECTED)
print(json.dumps({'modules': [store_module.__file__,worker_module.__file__]}))
def emit(mode, **kw): print(json.dumps(dict(mode=mode,**kw)))

async def terminal_reconcile():
    store,path=helper.newstore('terminal-reconcile'); fake=FakeBot(); fake.uncertain=True
    fake.gets=0; fake.resolved=False
    original_status=fake.status
    def status():
        payload=original_status();payload.update(state='interrupted',revision=3 if fake.resolved else 2,stop_confirmed=fake.resolved,effects_uncertain=not fake.resolved);return payload
    fake.status=status
    original_get=fake.get_run
    async def counted(req): fake.gets+=1; return await original_get(req)
    fake.get_run=counted
    async def cancel(req):
        fake.cancels+=1
        fake.resolved=True
        payload=fake.status()
        return web.json_response(payload)
    fake.cancel=cancel
    factory=ClientFactory({'KEY':'synthetic'})
    async with TestServer(fake.app()) as server:
        bot=Bot('a','A','r',str(server.make_url('/')).rstrip('/'),'KEY',instance_id='inst_a')
        store.upsert_bot(bot); task=admit(store,bot); worker=DispatchWorker(store,factory)
        await worker.pump_once(); initial=store.get_task(task.task_id)
        store.request_cancel(task.task_id)
        for _ in range(3): await worker.pump_once()
        current=store.get_task(task.task_id)
        snap=store.quota_snapshot(workflow_id='wf',caller_principal='planner',bot_id='a')
        emit('terminal_reconcile',initial_state=initial.run_state,remote_stop_confirmed=fake.status()['stop_confirmed'],cancel_requests=fake.cancels,status_requests=fake.gets,local_state=current.run_state,local_revision=current.remote_revision,local_cancel=current.cancel_state.value,local_stop_confirmed=current.stop_confirmed,reserved_bytes=snap['workflow_result_bytes'])
        assert fake.gets>=2 and current.stop_confirmed and current.cancel_state.value=='confirmed' and snap['workflow_result_bytes']==0
    await factory.aclose(); store.close()

async def stop_auth():
    store,path=helper.newstore('stop-auth'); bot=Bot('a','A','r','http://127.0.0.1:9','KEY'); store.upsert_bot(bot)
    task=admit(store,bot,caller_principal='operator',workflow_id='private-operator-wf')
    config=CoordinatorConfig(control_dir=path)
    async with TestClient(TestServer(build_app(store,config,{'operator':'admin','planner':'planner'}))) as client:
        headers={'Authorization':'Bearer planner'}
        direct=await client.post(f'/v1/teams/tasks/{task.task_id}/cancel',headers=headers)
        stop=await client.post('/v1/teams/workflows/private-operator-wf/stop',headers=headers)
        payload=await stop.json(); after=store.get_task(task.task_id)
        emit('stop_auth',direct_status=direct.status,workflow_status=stop.status,workflow_body=payload,operator_task_cancel=after.cancel_state.value)
        assert direct.status==403 and stop.status==403 and after.cancel_state.value=='none'
    store.close()

async def migration_alias():
    store,path=helper.newstore('migration-alias'); fake=FakeBot(); factory=ClientFactory({'KEY':'synthetic'})
    async with TestServer(fake.app()) as server:
        endpoint=str(server.make_url('/')).rstrip('/'); alias=endpoint.replace('127.0.0.1','localhost')
        config=CoordinatorConfig(control_dir=path)
        async with TestClient(TestServer(build_app(store,config,{'operator':'admin','planner':'planner'}))) as client:
            for bot_id,url in [('original',endpoint),('alias',alias)]:
                response=await client.post('/v1/teams/bots',headers={'Authorization':'Bearer admin'},json={'bot_id':bot_id,'endpoint':url,'secret_env':'KEY'})
                assert response.status==201
            rec=TaskRecord(task_id='old_unknown',root_task_id=None,parent_task_id=None,caller='planner',target='original',hop=0,status='outcome_unknown',error_code=None,truncated=False,http_sent=True,request_bytes=10,response_bytes=0,result_bytes=0,started_at=utc_now().isoformat(),finished_at=None,duration_ms=None,team_id='p',endpoint=endpoint)
            ledger=Ledger(path/'legacy.sqlite3'); ledger.admit(rec,Budget(delegations=1,result_bytes=32768),32768,60,continuing=False); ledger.close()
            service=CoordinatorService(config,store)
            imported=service.import_04_ledger(teams_config={'team_id':'p'},ledger_path=path/'legacy.sqlite3',caller_id='planner')
            store.confirm_caller_quota('planner',delegations_limit=100,result_bytes_limit=1000000,actor='operator')
            statuses=[]
            for bot_id in ['original','alias']:
                response=await client.post('/v1/teams/tasks',headers={'Authorization':'Bearer planner'},json={'project':'p','bot':bot_id,'operation_key':str(uuid.uuid4()),'input':'synthetic'})
                statuses.append(response.status)
            await DispatchWorker(store,factory).pump_once()
            emit('migration_alias',original_admission=statuses[0],alias_admission=statuses[1],unknown_isolated=imported['unknown_isolated'],quarantines=list(store.active_quarantines()),alias_bound_instance=store.get_bot('alias').instance_id,remote_submits=fake.submits)
            assert statuses==[409,201] and fake.submits==0 and store.get_bot('alias').instance_id is None
    await factory.aclose(); store.close()

async def unfetched_reservation():
    store,path=helper.newstore('unfetched');fake=FakeBot();fake.terminal=True
    async def unavailable(req): fake.results+=1; return web.json_response({'code':'temporary'},status=503)
    fake.get_result=unavailable;factory=ClientFactory({'KEY':'synthetic'})
    async with TestServer(fake.app()) as server:
        bot=Bot('a','A','r',str(server.make_url('/')).rstrip('/'),'KEY',instance_id='inst_a');store.upsert_bot(bot)
        store.record_caller_usage('planner',delegations_used=0,result_bytes_used=0)
        store.confirm_caller_quota('planner',delegations_limit=100,result_bytes_limit=32769,actor='operator')
        task=admit(store,bot);worker=DispatchWorker(store,factory);await worker.pump_once()
        cur=store.get_task(task.task_id);snap=store.quota_snapshot(workflow_id='wf',caller_principal='planner',bot_id='a')
        try: next_task=admit(store,bot); allowed=True
        except AdmissionError: allowed=False
        emit('unfetched_reservation',result_available=cur.result_available,result_fetched=cur.result_fetched,stop_confirmed=cur.stop_confirmed,result_requests=fake.results,reserved_bytes=snap['workflow_result_bytes'],confirmed_budget=32769,second_task_admitted=allowed)
        assert cur.result_available and not cur.result_fetched and snap['workflow_result_bytes']==32768 and not allowed
    await factory.aclose();store.close()

async def lost_submit_404():
    from nanobot_teams.coordinator.client import BotRunClient
    store,path=helper.newstore('lost-submit-404');fake=FakeBot();started=asyncio.Event();release=asyncio.Event();committed=asyncio.Event()
    async def submit(req):
        started.set()
        try:
            await req.read();await release.wait();response=await FakeBot.submit(fake,req);committed.set();return response
        except BaseException as exc:
            raise
    async def lookup(req):
        return web.json_response(fake.status()) if fake.submits else web.json_response({'code':'run_not_found'},status=404)
    app=web.Application();app.router.add_get('/v1/agent-capabilities',fake.capabilities);app.router.add_post('/v1/agent-runs',submit);app.router.add_get('/v1/agent-runs/lookup',lookup);app.router.add_get('/v1/agent-runs/{run_id}',fake.get_run);app.router.add_post('/v1/agent-runs/{run_id}/cancel',fake.cancel)
    factory=ClientFactory({'KEY':'synthetic'})
    class ProductionSemanticsServer(TestServer):
        async def _make_runner(self, **kwargs):
            kwargs['handler_cancellation']=False
            return web.AppRunner(self.app,**kwargs)
    async with ProductionSemanticsServer(app) as server:
        endpoint=str(server.make_url('/')).rstrip('/');client=BotRunClient(endpoint,'synthetic',timeout_s=0.1);factory._clients[(endpoint,'synthetic')]=client
        bot=Bot('a','A','r',endpoint,'KEY',instance_id='inst_a');store.upsert_bot(bot);task=admit(store,bot);worker=DispatchWorker(store,factory)
        await worker.pump_once(); assert started.is_set() and not committed.is_set()
        entry=store.dispatch_entry_for_task(task.task_id);requested=store.request_cancel(task.task_id)
        store.defer_dispatch(entry.entry_id,utc_now(),'probe: make next reconciliation due')
        await worker.pump_once();after_404=store.get_task(task.task_id)
        release.set();await asyncio.wait_for(committed.wait(),2)
        store.defer_dispatch(entry.entry_id,utc_now(),'probe: make late reconciliation due')
        await worker.pump_once()
        current=store.get_task(task.task_id)
        emit('lost_submit_404',submit_started_persisted=entry.submit_started_at is not None,cancel_response=requested.cancel_state.value,cancel_after_404=after_404.cancel_state.value,stop_after_404=after_404.stop_confirmed,remote_committed_later=fake.submits,remote_state=fake.status()['state'],remote_cancels=fake.cancels,local_dispatch=current.dispatch_state.value,local_run=current.run_state,local_cancel=current.cancel_state.value,observable=len(store.tasks_to_observe()))
        assert fake.submits==1 and fake.cancels>=1
        assert after_404.cancel_state.value=='unconfirmed' and not after_404.stop_confirmed
        assert current.dispatch_state.value=='accepted' and current.cancel_state.value=='unconfirmed'
    await factory.aclose();store.close()
async def main():
    modes={'lost_submit_404':lost_submit_404,'terminal_reconcile':terminal_reconcile,'stop_auth':stop_auth,'migration_alias':migration_alias,'unfetched_reservation':unfetched_reservation}
    for name in sys.argv[1:] or modes: await modes[name]()
asyncio.run(main())







