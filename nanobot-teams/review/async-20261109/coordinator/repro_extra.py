from __future__ import annotations
import asyncio,json,sys,uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from aiohttp.test_utils import TestClient,TestServer
from repro_coordinator import newstore,admit,FakeBot
from nanobot_teams.coordinator.store import utc_now
from nanobot_teams.coordinator.models import Bot
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.config import CoordinatorConfig
from nanobot_teams.coordinator.service import CoordinatorService
from nanobot_teams.coordinator.worker import ClientFactory,DispatchWorker
from nanobot_teams.ledger import Ledger,TaskRecord
from nanobot_teams.config import Budget

async def missing_result():
    store,path=newstore('missing-result'); bot=Bot('a','A','research','http://127.0.0.1:9','KEY',instance_id='inst_a');store.upsert_bot(bot)
    task=admit(store,bot);store.update_after_submit(task.task_id,remote_run_id='run_a',remote_instance_id='inst_a')
    store.record_observation(task.task_id,revision=2,run_state='succeeded',stop_confirmed=True,result_text='RESULT-MARKER-7af8',result_truncated=False)
    async with TestClient(TestServer(build_app(store,CoordinatorConfig(control_dir=path),{'operator':'synthetic-admin','planner':'synthetic-planner'}))) as client:
        for role,token in [('planner','synthetic-planner'),('operator','synthetic-admin')]:
            response=await client.get('/v1/teams/tasks/'+task.task_id,headers={'Authorization':'Bearer '+token});body=await response.json()
            result=await client.get('/v1/teams/tasks/'+task.task_id+'/result',headers={'Authorization':'Bearer '+token})
            snapshot=await client.get('/v1/teams/snapshot',headers={'Authorization':'Bearer '+token});snapshot_text=await snapshot.text()
            print(json.dumps(dict(role=role,detail_status=response.status,result_available=body['result_available'],detail_contains_result_marker='RESULT-MARKER-7af8' in json.dumps(body),result_route_status=result.status,snapshot_contains_result_marker='RESULT-MARKER-7af8' in snapshot_text)))
            assert body['result_available'] and 'result_text' not in body and result.status==404
    store.close()

async def migration():
    store,path=newstore('migration');ledger_path=path/'legacy.sqlite3';endpoint='http://127.0.0.1:9999'
    entry=TaskRecord(task_id='old_unknown',root_task_id=None,parent_task_id=None,caller='planner',target='a',hop=0,status='pending',error_code=None,truncated=False,http_sent=False,request_bytes=10,response_bytes=0,result_bytes=0,started_at=utc_now().isoformat(),finished_at=None,duration_ms=None,team_id='t',endpoint=endpoint)
    ledger=Ledger(ledger_path);ledger.admit(entry,Budget(delegations=1,result_bytes=32768),32768,60,continuing=False);usage=ledger.usage_for('planner');ledger.close()
    bot=Bot('a','A','research',endpoint,'KEY',instance_id='inst_a');store.upsert_bot(bot)
    service=CoordinatorService(CoordinatorConfig(control_dir=path),store)
    report=service.import_04_ledger(teams_config={'team_id':'t','budgets':{'planner':{'delegations':1,'result_bytes':32768}}},ledger_path=ledger_path,caller_id='planner')
    newtask=admit(store,bot,workflow_id='brand-new-workflow')
    print(json.dumps(dict(legacy_usage=usage,imported_delegations=report['usage_delegations'],imported_unknown=report['unknown_isolated'],active_quarantines=list(store.active_quarantines()),new_task_admitted_without_quota_confirmation=bool(newtask),same_endpoint=bot.endpoint==entry.endpoint,legacy_delegation_limit=1)))
    assert newtask and report['unknown_isolated']==1
    store.close()

async def quarantine_existing_outbox():
    store,path=newstore('quarantine-outbox');fake=FakeBot();factory=ClientFactory({'KEY':'synthetic'})
    async with TestServer(fake.app()) as server:
        bot=Bot('a','A','research',str(server.make_url('/')).rstrip('/'),'KEY',instance_id='inst_a');store.upsert_bot(bot)
        task=admit(store,bot);store.quarantine_instance('inst_a','unconfirmed prior execution')
        await DispatchWorker(store,factory).pump_once()
        print(json.dumps(dict(quarantine_active='inst_a' in store.active_quarantines(),submit_requests=fake.submits,task_dispatch=store.get_task(task.task_id).dispatch_state)))
        assert fake.submits==1
    await factory.aclose();store.close()

async def main():
    mode=sys.argv[1]
    if mode=='missing_result': await missing_result()
    elif mode=='migration': await migration()
    elif mode=='quarantine_existing_outbox':await quarantine_existing_outbox()
    print('REPRODUCED '+mode)
if __name__=='__main__':asyncio.run(main())
