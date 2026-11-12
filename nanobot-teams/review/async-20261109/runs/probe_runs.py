import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

REPO = Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp')
sys.path.insert(0, str(REPO))
from nanobot.runs.service import RunService
from nanobot.runs.store import RunStore, canonical_request_digest
from nanobot.runs.types import ExecutionResult, OutcomeCode, RunState, RunSubmit, utc_now
from nanobot.config.home import set_home_path
from loguru import logger
logger.remove()


def req(task='task', deadline=600):
    return RunSubmit(task, 'wf', 'do the work', utc_now()+timedelta(seconds=deadline), str(uuid.uuid4()), 'principal')

class Executor:
    def __init__(self, hold=False, result=None):
        self.calls=[]
        self.gate=asyncio.Event()
        if not hold:
            self.gate.set()
        self.result=result or ExecutionResult(OutcomeCode.SUCCEEDED, text='ok')
    async def execute(self, run):
        self.calls.append(run.run_id)
        await self.gate.wait()
        return self.result

async def wait_state(service, rid, state):
    for _ in range(300):
        run=await service.get(rid)
        if run.state == state:
            return run
        await asyncio.sleep(.01)
    raise AssertionError(f'expected {state}, actual {run.state}')

async def recovery(root):
    db=root/'recovery.sqlite3'
    store=RunStore(db); store.open()
    service=RunService(store, Executor())
    first=(await service.submit(req('crashed'))).run
    store.transition(first.run_id, RunState.QUEUED, RunState.RUNNING)
    queued=(await service.submit(req('queued-before-crash'))).run
    store.close()
    store2=RunStore(db); store2.open(); executor=Executor()
    recovered=RunService(store2,executor)
    await recovered.start()
    await wait_state(recovered,queued.run_id,RunState.SUCCEEDED)
    prior=await recovered.get(first.run_id)
    print(json.dumps({'probe':'recovery','crashed_state':prior.state,'crashed_effects_uncertain':prior.effects_uncertain,'isolation':store2.isolation(),'queued_executed':queued.run_id in executor.calls}))
    await recovered.aclose();store2.close()

async def two_writers(root):
    import subprocess
    db=root/'writers.sqlite3'
    child=subprocess.Popen([sys.executable,'-u',__file__,'writer-child',str(db)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        line=await asyncio.wait_for(asyncio.to_thread(child.stdout.readline),10)
        original=json.loads(line)
        store=RunStore(db);store.open();executor=Executor()
        service=RunService(store,executor)
        interruptions=await service.start()
        next_run=(await service.submit(req('second-process'))).run
        await wait_state(service,next_run.run_id,RunState.SUCCEEDED)
        print(json.dumps({'probe':'two-writers','second_open_accepted':True,'same_instance':service.instance_id==original['instance'],'first_process_still_alive':child.poll() is None,'first_run_interrupted_by_second':original['run_id'] in [r.run_id for r in interruptions],'second_executed':next_run.run_id in executor.calls}))
        await service.aclose();store.close()
    finally:
        child.terminate();child.wait(timeout=5)

async def writer_child(db):
    store=RunStore(db);store.open();executor=Executor(hold=True);service=RunService(store,executor)
    await service.start();run=(await service.submit(req('first-process'))).run
    while not executor.calls: await asyncio.sleep(.01)
    print(json.dumps({'instance':service.instance_id,'run_id':run.run_id}),flush=True)
    await asyncio.Event().wait()

async def stopped_false(root):
    store=RunStore(root/'stopfalse.sqlite3');store.open()
    service=RunService(store,Executor(result=ExecutionResult(OutcomeCode.CANCELLED,stop_confirmed=False)))
    await service.start();run=(await service.submit(req())).run
    final=await wait_state(service,run.run_id,RunState.CANCELLED)
    print(json.dumps({'probe':'false-stop','executor_stop_confirmed':False,'stored_state':final.state,'stored_stop_confirmed':final.stop_confirmed,'isolated':store.isolation()[0]}))
    await service.aclose();store.close()

async def real_loop(root, kind):
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.providers.base import LLMProvider,LLMResponse,ToolCallRequest
    from nanobot.runs.executor import AgentLoopRunExecutor
    from nanobot.agent.hook import AgentHook
    class Provider(LLMProvider):
        def __init__(self):
            super().__init__(provider_name='offline-review');self.calls=0;self.tools=None
        def get_default_model(self):return 'offline-review'
        async def chat(self,messages,tools=None,**kwargs):
            self.calls+=1;self.tools=tools
            if kind=='empty':return LLMResponse(content='')
            if self.calls==1:
                return LLMResponse(content=None,tool_calls=[ToolCallRequest('sleep','exec',{'command':f'& "{sys.executable}" -c "import time; time.sleep(60)"','shell':'pwsh','login':False,'yield_time_ms':0})])
            return LLMResponse(content='Done')
        async def chat_stream(self,messages,tools=None,on_content_delta=None,**kwargs):
            return await self.chat(messages,tools,**kwargs)
    class Capture(AgentHook):
        async def after_run(self,context):self.stop=context.stop_reason;self.error=context.error
    provider=Provider();capture=Capture()
    loop=AgentLoop(bus=MessageBus(),provider=provider,workspace=root/'workspace',hooks=[capture],max_iterations=3)
    store=RunStore(root/f'{kind}.sqlite3');store.open();service=RunService(store,AgentLoopRunExecutor(loop))
    try:
        await service.start();run=(await service.submit(req())).run
        for _ in range(500):
            final=await service.get(run.run_id)
            if final.terminal:break
            await asyncio.sleep(.01)
        else:raise AssertionError('real loop did not finish')
        live=await loop._exec_session_manager.list(owner_session_key=f'run:{run.run_id}')
        next_run=(await service.submit(req('next-run'))).run
        next_final=await wait_state(service,next_run.run_id,RunState.SUCCEEDED)
        cancel=await service.cancel(run.run_id)
        live=await loop._exec_session_manager.list(owner_session_key=f'run:{run.run_id}')
        print(json.dumps({'next_run_state':next_final.state,'cancelled_original_state':cancel.state,'probe':kind,'state':final.state,'stop_confirmed':final.stop_confirmed,'effects_uncertain':final.effects_uncertain,'outcome':final.outcome.code,'text':final.outcome.partial,'live_owned_processes':[{'session_id':s.session_id,'returncode':s.returncode} for s in live],'provider_calls':provider.calls}))
    finally:
        await service.aclose();await loop.aclose();store.close()

async def http(root):
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    from nanobot.api.agent_runs import register_runs_routes
    from nanobot.config.schema import ApiRunsConfig
    store=RunStore(root/'http.sqlite3');store.open();service=RunService(store,Executor())
    app=web.Application();register_runs_routes(app,service,ApiRunsConfig(enabled=True,api_key='dedicated',execution_enabled=True,principal_id='principal'))
    client=TestClient(TestServer(app));await client.start_server()
    headers={'Authorization':'Bearer dedicated','Idempotency-Key':str(uuid.uuid4())}
    try:
        for label,body in [('invalid-json','{'),('duplicate-key','{"protocol":1,"protocol":1}')]:
            response=await client.post('/v1/agent-runs',data=body,headers=headers)
            print(json.dumps({'probe':label,'status':response.status,'content_type':response.content_type,'body':await response.text()}))
        run=(await service.submit(req())).run
        await service._pump_once()
        await wait_state(service,run.run_id,RunState.SUCCEEDED)
        store._conn.execute('UPDATE run_events SET at=?',((utc_now()-timedelta(days=31)).isoformat(),));store.cleanup()
        response=await client.get('/v1/agent-runs/events?after=0',headers=headers)
        print(json.dumps({'probe':'expired-sse','status':response.status,'body':await response.text()}))
        store._conn.execute('UPDATE runs SET finished_at=?',((utc_now()-timedelta(days=15)).isoformat(),));store.cleanup()
        response=await client.get(f'/v1/agent-runs/{run.run_id}',headers=headers);status=await response.json()
        result_response=await client.get(f'/v1/agent-runs/{run.run_id}/result',headers=headers)
        print(json.dumps({'probe':'cleaned-result','status_result_available':status['result_available'],'result_http_status':result_response.status}))
    finally:
        await client.close();await service.aclose();store.close()

async def retention(root):
    store=RunStore(root/'retention.sqlite3');store.open();service=RunService(store,Executor())
    run=(await service.submit(req())).run
    store.transition(run.run_id,RunState.QUEUED,RunState.RUNNING)
    store.recover()
    counts=store.cleanup(now=utc_now()+timedelta(days=100))
    print(json.dumps({'probe':'retention-unconfirmed','deleted_unconfirmed_run':store.get(run.run_id) is None,'counts':counts}))
    store.close()

async def main():
    if len(sys.argv)>1 and sys.argv[1]=='writer-child':
        await writer_child(Path(sys.argv[2]));return
    mode=sys.argv[1] if len(sys.argv)>1 else 'all'
    with tempfile.TemporaryDirectory(prefix='run-probe-',dir=Path(__file__).parent) as tmp:
        root=Path(tmp);set_home_path(root/'home')
        print(json.dumps({'source':__import__('nanobot.runs.service',fromlist=['']).__file__,'python':sys.executable}))
        if mode in ('all','recovery'):await recovery(root)
        if mode in ('all','two-writers'):await two_writers(root)
        if mode in ('all','false-stop'):await stopped_false(root)
        if mode in ('all','empty'):await real_loop(root,'empty')
        if mode in ('all','background'):await real_loop(root,'background')
        if mode in ('all','http'):await http(root)
        if mode in ('all','retention'):await retention(root)

if __name__=='__main__':asyncio.run(main())

