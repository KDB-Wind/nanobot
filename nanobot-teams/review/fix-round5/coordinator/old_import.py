import json, sys
from pathlib import Path
from nanobot_teams.coordinator.store import CoordinatorStore, utc_now
from nanobot_teams.coordinator.config import CoordinatorConfig
from nanobot_teams.coordinator.service import CoordinatorService
from nanobot_teams.coordinator.models import Bot
from nanobot_teams.ledger import Ledger, TaskRecord
from nanobot_teams.config import Budget
path=Path(sys.argv[1]); endpoint=sys.argv[2]
store=CoordinatorStore(path/'coord.sqlite3'); store.open(); store.create_project('p','Project')
for bid,url in [('original',endpoint),('alias',endpoint.replace('127.0.0.1','localhost'))]:
    store.upsert_bot(Bot(bid,bid,'r',url,'KEY'))
rec=TaskRecord(task_id='old_unknown',root_task_id=None,parent_task_id=None,caller='planner',target='original',hop=0,status='outcome_unknown',error_code=None,truncated=False,http_sent=True,request_bytes=10,response_bytes=0,result_bytes=0,started_at=utc_now().isoformat(),finished_at=None,duration_ms=None,team_id='p',endpoint=endpoint)
ledger=Ledger(path/'legacy.sqlite3'); ledger.admit(rec,Budget(delegations=1,result_bytes=32768),32768,60,continuing=False); ledger.close()
result=CoordinatorService(CoordinatorConfig(control_dir=path),store).import_04_ledger(teams_config={'team_id':'p'},ledger_path=path/'legacy.sqlite3',caller_id='planner')
print(json.dumps({'source':__import__('nanobot_teams.coordinator.store',fromlist=['x']).__file__,'imported':result,'quarantines':list(store.active_quarantines())}))
store.close()
