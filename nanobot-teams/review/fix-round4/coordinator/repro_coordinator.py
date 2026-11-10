from __future__ import annotations
import asyncio, gzip, json, os, sys, tempfile, uuid
from datetime import timedelta
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestServer, TestClient
from nanobot_teams.coordinator.store import CoordinatorStore, utc_now, StoreError
from nanobot_teams.coordinator.models import Bot, DeliveryMode
from nanobot_teams.coordinator.worker import DispatchWorker, ClientFactory
from nanobot_teams.coordinator.client import BotRunClient
from nanobot_teams.coordinator.config import CoordinatorConfig
from nanobot_teams.coordinator.http_api import build_app

ROOT = Path(__file__).parent

def newstore(label):
    path = Path(tempfile.mkdtemp(prefix=label+'-', dir=ROOT))
    store = CoordinatorStore(path/'coord.sqlite3'); store.open(); store.create_project('p','Project')
    return store, path

def admit(store, bot, **kwargs):
    args = dict(caller_principal='planner',project_id='p',workflow_id='wf',bot_id=bot.bot_id,
                operation_key=str(uuid.uuid4()),input_text='synthetic task',deadline_at=None,
                delivery_mode=DeliveryMode.WORKBENCH_ONLY,bot=bot)
    args.update(kwargs)
    return store.admit_task(**args)[0]

class FakeBot:
    def __init__(self):
        self.submits=0; self.results=0; self.cancels=0; self.fail_first_result=False; self.conflict_first=False
        self.terminal=False; self.uncertain=False; self.storage_lost=False; self.truncated=False
    async def capabilities(self, req):
        return web.json_response({'protocol':1,'instance_id':'inst_a','execution':True})
    async def submit(self, req):
        self.submits+=1
        if self.conflict_first and self.submits == 1:
            return web.json_response({'code':'idempotency_conflict'},status=409)
        return web.json_response(self.status(),status=202)
    def status(self):
        return dict(protocol=1,instance_id='inst_a',run_id='run_a',task_id='task_synthetic',
                    state='interrupted' if self.uncertain else ('succeeded' if self.terminal else 'running'),
                    revision=2 if self.terminal or self.uncertain else 1, stop_confirmed=not self.uncertain and self.terminal,
                    effects_uncertain=self.uncertain,outcome={'code':'interrupted' if self.uncertain else 'succeeded'} if self.terminal or self.uncertain else None,
                    result_available=self.terminal)
    async def get_run(self,req):
        if self.storage_lost: return web.json_response({'code':'run_not_found'},status=404)
        return web.json_response(self.status())
    async def get_result(self,req):
        self.results+=1
        if self.fail_first_result and self.results == 1: return web.json_response({'code':'temporary_failure'},status=503)
        return web.json_response({'run_id':'run_a','outcome':{'code':'succeeded','text':'retained answer','partial':False,'truncated':self.truncated}})
    async def cancel(self,req):
        self.cancels+=1
        return web.json_response({'state':'cancelling'},status=202)
    def app(self):
        app=web.Application();app.router.add_get('/v1/agent-capabilities',self.capabilities)
        app.router.add_post('/v1/agent-runs',self.submit)
        app.router.add_get('/v1/agent-runs/{run_id}/result',self.get_result)
        app.router.add_get('/v1/agent-runs/{run_id}',self.get_run)
        app.router.add_post('/v1/agent-runs/{run_id}/cancel',self.cancel)
        return app

async def bot_case(mode):
    fake=FakeBot();store,path=newstore(mode);factory=ClientFactory({'TEST_BOT_KEY':'synthetic-key'})
    async with TestServer(fake.app()) as server:
        bot=Bot('research','Research','research',str(server.make_url('/')).rstrip('/'),'TEST_BOT_KEY',instance_id='inst_a')
        store.upsert_bot(bot);worker=DispatchWorker(store,factory)
        if mode=='cancel_pending':
            task=admit(store,bot);store.request_cancel(task.task_id)
            print('before_dispatch cancel_state='+store.get_task(task.task_id).cancel_state.value)
            await worker.pump_once();current=store.get_task(task.task_id)
            print(json.dumps(dict(submit_requests=fake.submits,cancel_requests=fake.cancels,dispatch=current.dispatch_state,run_state=current.run_state,cancel_state=current.cancel_state)))
            assert fake.submits==1 and fake.cancels==0 and current.cancel_state.value=='confirmed'
        elif mode=='lost_result':
            fake.fail_first_result=True;fake.terminal=True;task=admit(store,bot)
            await worker.pump_once();await worker.pump_once();await worker.pump_once()
            current=store.get_task(task.task_id)
            print(json.dumps(dict(run_state=current.run_state,result_available=current.result_available,result_text=current.result_text,result_fetch_requests=fake.results,remaining_observations=len(store.tasks_to_observe()))))
            assert current.run_state=='succeeded' and not current.result_available and fake.results==1
        elif mode=='conflict_isolation':
            fake.conflict_first=True;t1=admit(store,bot);t2=admit(store,bot)
            await worker.pump_once()
            t3=admit(store,bot,workflow_id='new-workflow')
            print(json.dumps(dict(first_state=store.get_task(t1.task_id).dispatch_state,second_state=store.get_task(t2.task_id).dispatch_state,submit_requests=fake.submits,quarantines=list(store.active_quarantines()),third_task_admitted=bool(t3),open_count=store.quota_snapshot(workflow_id='wf',caller_principal='planner',bot_id=bot.bot_id)['principal_open'])))
            assert fake.submits==2 and not store.active_quarantines()
        elif mode=='uncertain_isolation':
            fake.uncertain=True;t1=admit(store,bot);await worker.pump_once()
            snap=store.quota_snapshot(workflow_id='wf',caller_principal='planner',bot_id=bot.bot_id)
            t2=admit(store,bot);await worker.pump_once()
            print(json.dumps(dict(first_state=store.get_task(t1.task_id).run_state,effects_uncertain=store.get_task(t1.task_id).effects_uncertain,stop_confirmed=store.get_task(t1.task_id).stop_confirmed,open_after_uncertain=snap['principal_open'],quarantines=list(store.active_quarantines()),new_task_admitted=bool(t2),submit_requests=fake.submits)))
            assert not store.active_quarantines() and fake.submits==2 and snap['principal_open']==0
        elif mode=='expired_pending':
            task=admit(store,bot,deadline_at=utc_now()-timedelta(seconds=10))
            await worker.pump_once();await worker.pump_once()
            entry=store.pending_dispatch(utc_now())[0];current=store.get_task(task.task_id)
            print(json.dumps(dict(dispatch=current.dispatch_state,run_state=current.run_state,due_entries=len(store.pending_dispatch(utc_now())),attempts=entry.attempts,error=entry.last_error,submit_requests=fake.submits)))
            assert current.run_state=='unobserved' and entry.last_error=='' and entry.attempts==0
        elif mode=='truncation':
            fake.terminal=True;fake.truncated=True;task=admit(store,bot);await worker.pump_once()
            current=store.get_task(task.task_id)
            print(json.dumps(dict(remote_truncated=True,coordinator_truncated=current.result_truncated,result_text=current.result_text)))
            assert not current.result_truncated
        elif mode=='storage_lost':
            task=admit(store,bot);await worker.pump_once();fake.storage_lost=True
            await worker.pump_once();await worker.pump_once();current=store.get_task(task.task_id)
            print(json.dumps(dict(dispatch=current.dispatch_state,run_state=current.run_state,quarantines=list(store.active_quarantines()),submit_requests=fake.submits)))
            assert current.dispatch_state.value=='accepted' and not store.active_quarantines()
    await factory.aclose();store.close()

async def scaffold():
    from nanobot.config.loader import load_config
    store,path=newstore('scaffold');config=CoordinatorConfig(control_dir=path,bot_homes_root=path/'homes')
    async with TestClient(TestServer(build_app(store,config,{'operator':'synthetic-admin','planner':'synthetic-planner'}))) as client:
        response=await client.post('/v1/teams/bots/scaffold',headers={'Authorization':'Bearer synthetic-admin'},json={'bot_id':'new-bot'})
        body=await response.json(); home=Path(body['home']); text=(home/'nanobot.toml').read_text()
        print(json.dumps(dict(http_status=response.status,browser_response_has_raw_api_key=bool(body.get('api_key')),config_contains_raw_key=body['api_key'] in text,contains_literal_f_string_segments='f"' in text,config_name='nanobot.toml')))
        try:
            load_config(home/'nanobot.toml')
        except Exception as exc:
            print('load_config='+type(exc).__name__+': '+str(exc))
        else:
            raise AssertionError('Expected malformed config failure')
    store.close()

async def response_bounds():
    big='x'*(1024*1024)
    async def caps(req):
        return web.Response(body=gzip.compress(json.dumps({'protocol':1,'instance_id':'inst_a','extra':big}).encode()),headers={'Content-Encoding':'gzip','Content-Type':'application/json'})
    app=web.Application();app.router.add_get('/v1/agent-capabilities',caps)
    async with TestServer(app) as server:
        client=BotRunClient(str(server.make_url('/')),'synthetic-key')
        response=await client.capabilities()
        print(json.dumps(dict(status=response.status,accepted_compressed_response=True,accepted_payload_text_bytes=len(response.payload['extra']),contract_raw_limit=256*1024)))
        assert len(response.payload['extra'])==1024*1024
        await client.aclose()

async def environment_proxy():
    seen=[]
    async def proxy(req):
        seen.append(req.headers.get('Authorization',''))
        return web.json_response({'protocol':1,'instance_id':'proxy'})
    app=web.Application();app.router.add_route('*','/{tail:.*}',proxy)
    async with TestServer(app) as server:
        names=('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY');saved={n:os.environ.get(n) for n in names}
        try:
            for n in names: os.environ.pop(n,None)
            os.environ['HTTP_PROXY']=str(server.make_url('/'));os.environ['NO_PROXY']=''
            client=BotRunClient('http://127.0.0.1:9','synthetic-bot-credential')
            response=await client.capabilities();await client.aclose()
            print(json.dumps(dict(status=response.status,proxy_received_request=bool(seen),proxy_received_service_bearer=seen==['Bearer synthetic-bot-credential'])))
            assert seen==['Bearer synthetic-bot-credential']
        finally:
            for n,v in saved.items():
                if v is None: os.environ.pop(n,None)
                else: os.environ[n]=v

def quotas():
    store,path=newstore('quotas')
    bot=Bot('a','A','research','http://127.0.0.1:9999','KEY',instance_id='same-instance');alias=Bot('b','B','research','http://localhost:9999','KEY',instance_id='same-instance')
    store.upsert_bot(bot);store.upsert_bot(alias)
    for i in range(16): admit(store,bot,workflow_id='wf-a')
    alias_task=admit(store,alias,workflow_id='wf-b')
    before=store.quota_snapshot(workflow_id='wf-a',caller_principal='planner',bot_id='a')
    tasks=store.list_tasks(workflow_id='wf-a')
    for t in tasks: store.mark_indeterminate(t.task_id,'unknown execution')
    after=store.quota_snapshot(workflow_id='wf-a',caller_principal='planner',bot_id='a')
    print(json.dumps(dict(alias_task_admitted_with_16_open_on_same_instance=bool(alias_task),pending_result_bytes=before['workflow_result_bytes'],required_pending_reservation=16*32768,principal_open_before=before['principal_open'],principal_open_after_unknown=after['principal_open'])))
    assert alias_task and before['workflow_result_bytes']==0 and after['principal_open']==1
    store.close()

async def main():
    mode=sys.argv[1]
    if mode in {'cancel_pending','lost_result','conflict_isolation','uncertain_isolation','expired_pending','truncation','storage_lost'}: await bot_case(mode)
    elif mode=='scaffold': await scaffold()
    elif mode=='response_bounds': await response_bounds()
    elif mode=='environment_proxy': await environment_proxy()
    elif mode=='quotas': quotas()
    else: raise SystemExit('unknown mode')
    print('REPRODUCED '+mode)

if __name__=='__main__': asyncio.run(main())

