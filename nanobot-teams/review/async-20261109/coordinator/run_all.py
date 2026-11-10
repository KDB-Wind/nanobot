import concurrent.futures,json,subprocess,sys
from pathlib import Path
folder=Path(__file__).parent
cases=[('repro_mcp_meta.py',None)]+[('repro_coordinator.py',m) for m in ('cancel_pending','lost_result','scaffold','conflict_isolation','uncertain_isolation','expired_pending','truncation','storage_lost','response_bounds','environment_proxy','quotas')]+[('repro_extra.py',m) for m in ('missing_result','migration','quarantine_existing_outbox')]
def run(case):
    script,mode=case; command=[sys.executable,str(folder/script)]+([mode] if mode else [])
    result=subprocess.run(command,text=True,capture_output=True,timeout=30)
    return dict(command=command,exit_code=result.returncode,stdout=result.stdout,stderr=result.stderr)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(run,cases))
(folder/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
print(json.dumps({'reproductions':len(results),'all_assertions_passed':all(r['exit_code']==0 for r in results),'artifact':str(folder/'results.json')}))
for case,result in zip(cases,results):
    print((case[1] or case[0])+': '+str(result['exit_code']))
raise SystemExit(0 if all(r['exit_code']==0 for r in results) else 1)
