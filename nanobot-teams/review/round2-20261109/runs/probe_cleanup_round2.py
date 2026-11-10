import asyncio, json, sys, uuid, tempfile
from pathlib import Path
from datetime import timedelta
REPO=Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp');sys.path.insert(0,str(REPO))
from nanobot.agent.loop import AgentLoop
from nanobot.agent.hook import AgentHook
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider,LLMResponse,ToolCallRequest
from nanobot.runs.executor import AgentLoopRunExecutor
from nanobot.runs.service import RunService
from nanobot.runs.store import RunStore
from nanobot.runs.types import *
from nanobot.config.home import set_home_path
from loguru import logger
logger.remove()
ROOT=Path(__file__).parent

def out(probe,**kw):print(json.dumps(dict(probe=probe,**kw)),flush=True)
def req(name='task'):
    return RunSubmit(name,'wf','test cleanup',utc_now()+timedelta(seconds=60),str(uuid.uuid4()),'p')
async def until(check):
    for _ in range(2000):
        if check():return
        await asyncio.sleep(.01)
    raise RuntimeError('wait exhausted')
class Provider(LLMProvider):
    def __init__(self,mode):
        super().__init__(provider_name='offline-review');self.mode=mode;self.root_calls=0;self.child_entered=asyncio.Event();self.release=asyncio.Event();self.cancel_count=0
    def get_default_model(self):return 'offline-review'
    async def chat(self,messages,tools=None,**kw):
        root=any(t.get('function',{}).get('name')=='subagent' for t in (tools or []))
        if self.mode=='resistant' and not root:
            self.child_entered.set()
            while not self.release.is_set():
                try: await self.release.wait()
                except asyncio.CancelledError:self.cancel_count+=1
            return LLMResponse(content='child finished')
        self.root_calls+=1
        if self.root_calls==1:
            if self.mode=='resistant':
                return LLMResponse(content=None,tool_calls=[ToolCallRequest('child','subagent',{'action':'create','task':'resist cancellation'})])
            return LLMResponse(content=None,tool_calls=[ToolCallRequest('sleep','exec',{'command':f'& "{sys.executable}" -c "import time; time.sleep(60)"','shell':'pwsh','login':False,'yield_time_ms':0})])
        return LLMResponse(content='Done')
    async def chat_stream(self,messages,tools=None,**kw):return await self.chat(messages,tools=tools,**kw)
class ExplodingHook(AgentHook):
    def __init__(self):super().__init__(reraise=True)
    async def after_run(self,ctx):raise RuntimeError('integration hook failed after exec')
async def resistant(root,deadline=False):
    p=Provider('resistant');loop=AgentLoop(bus=MessageBus(),provider=p,workspace=root/'workspace',max_iterations=3)
    s=RunStore(root/'resistant.sqlite3');s.open();v=RunService(s,AgentLoopRunExecutor(loop),cancel_wait_s=.05)
    try:
        await v.start();request=req()
        if deadline:
            request=RunSubmit(request.task_id,request.workflow_id,request.input_text,utc_now()+timedelta(seconds=.25),request.operation_key,request.principal_id)
        r=(await v.submit(request)).run
        await asyncio.wait_for(p.child_entered.wait(),10)
        if not deadline: await v.cancel(r.run_id)
        await until(lambda:s.get(r.run_id).terminal)
        final=s.get(r.run_id)
        running=loop.subagents.get_running_count_by_session(f'run:{r.run_id}')
        out('resistant_child_finalized',deadline=deadline,outcome=final.outcome.code,state=final.state,stop_confirmed=final.stop_confirmed,effects_uncertain=final.effects_uncertain,isolation=s.isolation()[0],live_owned_subagents=running,cancellations_seen=p.cancel_count)
        r2=(await v.submit(req('next'))).run
        await until(lambda:s.get(r2.run_id).terminal)
        out('resistant_child_next_run',state=s.get(r2.run_id).state,prior_child_alive=not p.release.is_set() and loop.subagents.get_running_count_by_session(f'run:{r.run_id}')>0)
    finally:
        p.release.set();await asyncio.sleep(.1);await v.aclose();await loop.aclose();s.close()
async def exception(root):
    p=Provider('exception');loop=AgentLoop(bus=MessageBus(),provider=p,workspace=root/'workspace',hooks=[ExplodingHook()],max_iterations=3)
    s=RunStore(root/'exception.sqlite3');s.open();v=RunService(s,AgentLoopRunExecutor(loop))
    try:
        await v.start();r=(await v.submit(req())).run
        await until(lambda:s.get(r.run_id).terminal)
        r=s.get(r.run_id);live=await loop._exec_session_manager.list(owner_session_key=f'run:{r.run_id}')
        out('exception_skipped_reap',state=r.state,code=r.outcome.code,stop_confirmed=r.stop_confirmed,effects_uncertain=r.effects_uncertain,isolation=s.isolation()[0],live_owned_processes=[dict(session_id=x.session_id,returncode=x.returncode) for x in live])
    finally:await v.aclose();await loop.aclose();s.close()
async def deadline(root):
    await resistant(root,deadline=True)
async def main():
    mode=sys.argv[1]
    with tempfile.TemporaryDirectory(prefix=mode+'-',dir=ROOT) as d:
        root=Path(d);set_home_path(root/'home');await globals()[mode](root)
if __name__=='__main__':asyncio.run(main())
