import asyncio,json,tempfile
from pathlib import Path
from aiohttp.test_utils import TestServer,TestClient
from nanobot_teams.coordinator.config import CoordinatorConfig
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.models import Bot
from nanobot_teams.coordinator.worker import ClientFactory,DispatchWorker
from nanobot_teams.coordinator.store import CoordinatorStore
from repro_coordinator import FakeBot,admit
BASE=Path(__file__).parent
class OtherBot(FakeBot):
    async def capabilities(self,req):
        from aiohttp import web
        return web.json_response({'protocol':1,'instance_id':'inst_b','execution':True})
    def status(self):
        result=super().status(); result['instance_id']='inst_b'; return result
async def main():
    path=Path(tempfile.mkdtemp(prefix='mixed-stop-',dir=BASE)); store=CoordinatorStore(path/'coord.sqlite3');store.open();store.create_project('p','P')
    bot=Bot('a','A','r','http://127.0.0.1:9','KEY');store.upsert_bot(bot)
    planner=admit(store,bot);operator=admit(store,bot,caller_principal='operator')
    async with TestClient(TestServer(build_app(store,CoordinatorConfig(control_dir=path),{'operator':'admin','planner':'planner'}))) as client:
        response=await client.post('/v1/teams/workflows/wf/stop',headers={'Authorization':'Bearer planner'})
        afterp=store.get_task(planner.task_id);aftero=store.get_task(operator.task_id)
        print(json.dumps({'case':'mixed_ownership','status':response.status,'planner_cancel':afterp.cancel_state.value,'operator_cancel':aftero.cancel_state.value}))
        assert response.status==200 and afterp.cancel_state.value=='confirmed' and aftero.cancel_state.value=='none'
    store.close()
    path=Path(tempfile.mkdtemp(prefix='distinct-instance-',dir=BASE));store=CoordinatorStore(path/'coord.sqlite3');store.open();store.create_project('p','P')
    old=FakeBot();other=OtherBot();factory=ClientFactory({'KEY':'synthetic'})
    async with TestServer(old.app()) as original, TestServer(other.app()) as unrelated:
        endpoint=str(original.make_url('/')).rstrip('/');newendpoint=str(unrelated.make_url('/')).rstrip('/')
        store.quarantine_legacy_endpoint(endpoint,'review unknown',secret_env='KEY')
        bot=Bot('other','Other','r',newendpoint,'KEY');store.upsert_bot(bot);task=admit(store,bot)
        await DispatchWorker(store,factory).pump_once()
        print(json.dumps({'case':'distinct_instance','guard_mapping':store.active_legacy_guards()[0].instance_id,'remote_submits':other.submits,'local_dispatch':store.get_task(task.task_id).dispatch_state.value}))
        assert other.submits==1 and store.active_legacy_guards()[0].instance_id=='inst_a'
    await factory.aclose();store.close()
asyncio.run(main())
