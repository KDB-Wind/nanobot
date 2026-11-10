import asyncio, gzip, json, sys, tempfile, uuid
from pathlib import Path
from dataclasses import replace
from aiohttp import web
from aiohttp.test_utils import TestServer
ROOT=Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp')
sys.path[:0]=[str(ROOT/'nanobot-teams/src'),str(ROOT/'nanobot-teams/review/async-20261109/coordinator')]
from repro_coordinator import newstore, admit, FakeBot
import repro_coordinator; repro_coordinator.ROOT=Path(__file__).parent
from nanobot_teams.coordinator.worker import DispatchWorker,ClientFactory
from nanobot_teams.coordinator.models import Bot
from nanobot_teams.coordinator.store import AdmissionError
from nanobot_teams.async_mcp import CoordinatorClient, CoordinatorHttpError

def emit(mode, **kw): print(json.dumps(dict(mode=mode,**kw)))
async def cancel_race():
    store,path=newstore('cancel-race'); fake=FakeBot(); started=asyncio.Event(); release=asyncio.Event()
    async def submit(req):
        started.set(); await release.wait(); return await fake.submit(req)
    app=fake.app(); app=web.Application();app.router.add_get('/v1/agent-capabilities',fake.capabilities);app.router.add_post('/v1/agent-runs',submit);app.router.add_get('/v1/agent-runs/{run_id}',fake.get_run);app.router.add_post('/v1/agent-runs/{run_id}/cancel',fake.cancel)
    factory=ClientFactory({'KEY':'synthetic'})
    async with TestServer(app) as server:
        bot=Bot('a','A','r',str(server.make_url('/')).rstrip('/'),'KEY',instance_id='inst_a');store.upsert_bot(bot);task=admit(store,bot)
        pump=asyncio.create_task(DispatchWorker(store,factory).pump_once());await asyncio.wait_for(started.wait(),3)
        before=store.dispatch_entry_for_task(task.task_id);cancel=store.request_cancel(task.task_id);release.set();await pump
        current=store.get_task(task.task_id)
        emit('cancel_race',attempts_while_submit_in_flight=before.attempts,cancel_response=cancel.cancel_state.value,stop_claim_while_in_flight=cancel.stop_confirmed,submits=fake.submits,cancels=fake.cancels,final_cancel=current.cancel_state.value,final_run=current.run_state,final_dispatch=current.dispatch_state.value,observable=len(store.tasks_to_observe()))
    await factory.aclose();store.close()
async def uncertainty():
    store,path=newstore('uncertainty');fake=FakeBot();fake.uncertain=True;factory=ClientFactory({'KEY':'synthetic'})
    async with TestServer(fake.app()) as server:
        bot=Bot('a','A','r',str(server.make_url('/')).rstrip('/'),'KEY',instance_id='inst_a');store.upsert_bot(bot);task=admit(store,bot);worker=DispatchWorker(store,factory);await worker.pump_once()
        snap=store.quota_snapshot(workflow_id='wf',caller_principal='planner',bot_id='a');first=store.get_task(task.task_id)
        store.request_cancel(task.task_id); cancelled=store.get_task(task.task_id)
        store.resolve_quarantine('inst_a','operator','accept risk; keep unknown reservation')
        nexttask=admit(store,bot,workflow_id='new-wf');fake.uncertain=False;fake.terminal=True;await worker.pump_once();final=store.get_task(task.task_id)
        emit('uncertainty',effects_uncertain=first.effects_uncertain,stop_confirmed=first.stop_confirmed,principal_open=snap['principal_open'],reserved_bytes=snap['workflow_result_bytes'],cancel_after_terminal_uncertain=cancelled.cancel_state.value,old_observations_remaining=len([t for t in store.tasks_to_observe() if t.task_id==task.task_id]),old_run_after_remote_resolved=final.run_state,new_task_admitted_after_ack=bool(nexttask))
    await factory.aclose();store.close()
def migration_bytes():
    store,path=newstore('migration-bytes');bot=Bot('a','A','r','http://127.0.0.1:9','KEY',instance_id='inst_a');store.upsert_bot(bot)
    store.record_caller_usage('planner',delegations_used=1,result_bytes_used=32768);store.confirm_caller_quota('planner',delegations_limit=100,result_bytes_limit=32768,actor='operator')
    try:task=admit(store,bot);admitted=bool(task)
    except AdmissionError:admitted=False
    emit('migration_bytes',imported_result_used=store.caller_quota('planner')['result_bytes_used'],confirmed_result_limit=store.caller_quota('planner')['result_bytes_limit'],new_task_admitted=admitted);store.close()
async def mcp_bounds():
    async def get(req):return web.Response(body=gzip.compress(json.dumps({'bots':[{'payload':'x'*1048576}]}).encode()),headers={'Content-Encoding':'gzip'})
    app=web.Application();app.router.add_get('/v1/teams/bots',get)
    async with TestServer(app) as server:
        client=CoordinatorClient(str(server.make_url('/')).rstrip('/'),'synthetic')
        try:
            result=await client.list_bots()
        except CoordinatorHttpError as exc:
            emit('mcp_bounds',gzip_accepted=False,code=exc.code,expected='compressed and oversize replies rejected')
        else:
            emit('mcp_bounds',gzip_accepted=True,decoded_bytes=len(result[0]['payload']))
        finally:
            await client.aclose()
async def main():
    for fn in (cancel_race,uncertainty,mcp_bounds):await fn()
    migration_bytes()
asyncio.run(main())

