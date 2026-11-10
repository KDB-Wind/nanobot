import asyncio,json,os,sys,tempfile
from pathlib import Path
ROOT=Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp');sys.path.insert(0,str(ROOT/'nanobot-teams/src'))
from aiohttp.test_utils import TestServer,TestClient
from nanobot_teams.coordinator.config import load_coordinator_config,ConfigError
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.store import CoordinatorStore
async def main():
    path=Path(tempfile.mkdtemp(prefix='scaffold-config-',dir=Path(__file__).parent));os.environ['SC_OP']='synthetic-op';os.environ['SC_PL']='synthetic-pl'
    user=path/'user.json';user.write_text(json.dumps({'modelPresets':{'synthetic-preset':{'model':'synthetic-model'}}}))
    base={'control_dir':str(path/'control'),'bot_homes_root':str(path/'homes'),'admin_token_env':'SC_OP','planner_token_env':'SC_PL','scaffold_user_config':str(user)}
    cfgpath=path/'coordinator.json'
    cfgpath.write_text(json.dumps({**base,'scaffold_model_preset':'synthetic-preset'}))
    rejection=''
    try:
        config=load_coordinator_config(cfgpath)
    except ConfigError as exc:
        rejection=str(exc);config=None
    store=CoordinatorStore(path/'db.sqlite3');store.open()
    async with TestClient(TestServer(build_app(store,config,config.resolve_tokens()))) as client:
        resp=await client.post('/v1/teams/bots/scaffold',headers={'Authorization':'Bearer synthetic-op'},json={'bot_id':'synthetic-bot'});out=await resp.json()
        with_preset=dict(setting_preset=rejection,loaded_preset=config.scaffold_model_preset if config else '',scaffold_status=resp.status,scaffold_code=out.get('code',''))
    cfgpath.write_text(json.dumps(base))
    config2=load_coordinator_config(cfgpath)
    async with TestClient(TestServer(build_app(store,config2,config2.resolve_tokens()))) as client:
        resp=await client.post('/v1/teams/bots/scaffold',headers={'Authorization':'Bearer synthetic-op'},json={'bot_id':'other-bot'});out=await resp.json()
        without_preset=dict(loaded_preset=config2.scaffold_model_preset,scaffold_status=resp.status,scaffold_code=out.get('code',''))
    print(json.dumps(dict(mode='scaffold_config',with_preset=with_preset,without_preset=without_preset,expected='preset config loads and scaffolds; missing preset is 422')))
    store.close()
asyncio.run(main())
