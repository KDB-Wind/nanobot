import asyncio, json, sys, uuid, sqlite3, tempfile
from pathlib import Path
from datetime import timedelta
REPO=Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp')
sys.path.insert(0,str(REPO))
from nanobot.runs.store import RunStore, canonical_request_digest
from nanobot.runs.service import RunService
from nanobot.runs.types import *
from nanobot.config.home import set_home_path
from loguru import logger
logger.remove()
ROOT=Path(__file__).parent

def req(name='task', seconds=300):
    return RunSubmit(name,'wf','input',utc_now()+timedelta(seconds=seconds),str(uuid.uuid4()),'p')
class Executor:
    def __init__(self):self.gate=asyncio.Event();self.calls=[]
    async def execute(self, run):
        self.calls.append(run.run_id)
        await self.gate.wait()
        return ExecutionResult(OutcomeCode.SUCCEEDED,text='PERSIST_ME')
async def wait_until(check):
    for _ in range(500):
        if check():return
        await asyncio.sleep(.01)
    raise RuntimeError('wait exhausted')
def out(probe,**kw): print(json.dumps(dict(probe=probe,**kw)),flush=True)
async def retention(root):
    s=RunStore(root/'retain.sqlite3');s.open();e=Executor();v=RunService(s,e)
    r=(await v.submit(req())).run
    s.transition(r.run_id,RunState.QUEUED,RunState.RUNNING)
    s.recover()
    before=len(s.events_after(0));counts=s.cleanup(now=utc_now()+timedelta(days=100));r=s.get(r.run_id)
    out('unconfirmed_event_retention',counts=counts,row_kept=r is not None,effects_uncertain=r.effects_uncertain,events_before=before,events_after=len(s.events_after(0)))
    s.close()
async def result_persistence(root):
    s=RunStore(root/'result.sqlite3');s.open();e=Executor();v=RunService(s,e)
    r=(await v.submit(req())).run
    await v._pump_once()
    await wait_until(lambda:bool(e.calls))
    s._conn.execute('PRAGMA query_only=1')
    e.gate.set()
    await wait_until(lambda:r.run_id not in v._tasks)
    out('result_write_failure_pending',state=s.get(r.run_id).state,tracked=r.run_id in v._tasks)
    s._conn.execute('PRAGMA query_only=0')
    await v._pump_once()
    r=s.get(r.run_id)
    out('result_write_failure_recovery',state=r.state,outcome=r.outcome.code,text=r.outcome.partial,stop_confirmed=r.stop_confirmed,effects_uncertain=r.effects_uncertain,isolation=s.isolation()[0],executor_calls=len(e.calls))
    await v.aclose();s.close()
async def http_storage(root):
    from aiohttp import web
    from aiohttp.test_utils import TestClient,TestServer
    from nanobot.api.agent_runs import register_runs_routes
    from nanobot.config.schema import ApiRunsConfig
    s=RunStore(root/'http.sqlite3');s.open();v=RunService(s,Executor())
    r=(await v.submit(req())).run
    app=web.Application();register_runs_routes(app,v,ApiRunsConfig(enabled=True,api_key='dedicated',execution_enabled=True,principal_id='p'))
    c=TestClient(TestServer(app));await c.start_server();headers={'Authorization':'Bearer dedicated'}
    try:
        s._conn.set_authorizer(lambda action,*args:sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
        for suffix in ('','/result','/cancel'):
            method=c.post if suffix=='/cancel' else c.get
            resp=await method(f'/v1/agent-runs/{r.run_id}{suffix}',headers=headers)
            out('existing_run_storage_fault',route=suffix or '/status',status=resp.status,body=await resp.text())
    finally:
        s._conn.set_authorizer(None);await c.close();await v.aclose();s.close()
async def main():
    mode=sys.argv[1]
    with tempfile.TemporaryDirectory(prefix=mode+'-',dir=ROOT) as d:
        root=Path(d);set_home_path(root/'home')
        out('source',service=__import__('nanobot.runs.service',fromlist=['']).__file__,python=sys.executable)
        await globals()[mode](root)
if __name__=='__main__':asyncio.run(main())
