import asyncio,json,os,sys,tempfile
from pathlib import Path
ROOT=Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp');sys.path.insert(0,str(ROOT/'nanobot-teams/src'))
from aiohttp.test_utils import TestServer,TestClient
from nanobot_teams.coordinator.config import load_coordinator_config,ConfigError
from nanobot_teams.coordinator.http_api import build_app
from nanobot_teams.coordinator.store import CoordinatorStore
async def main():
    path=Path(tempfile.mkdtemp(prefix='scaffold-config-',dir=Path(__file__).parent));os.environ['SC_OP']='synthetic-op';os.environ['SC_PL']='synthetic-pl'
    body={'control_dir':str(path/'control'),'bot_homes_root':str(path/'homes'),'admin_token_env':'SC_OP','planner_token_env':'SC_PL','scaffold_model_preset':'synthetic-preset'};cfgpath=path/'coordinator.json';cfgpath.write_text(json.dumps(body));rejection=''
    try:load_coordinator_config(cfgpath)
    except ConfigError as exc:rejection=str(exc)
    del body['scaffold_model_preset'];cfgpath.write_text(json.dumps(body));config=load_coordinator_config(cfgpath);store=CoordinatorStore(path/'db.sqlite3');store.open()
    async with TestClient(TestServer(build_app(store,config,config.resolve_tokens()))) as client:
        resp=await client.post('/v1/teams/bots/scaffold',headers={'Authorization':'Bearer synthetic-op'},json={'bot_id':'synthetic-bot'});out=await resp.json();print(json.dumps(dict(mode='scaffold_config',setting_preset=rejection,loaded_preset=config.scaffold_model_preset,scaffold_status=resp.status,scaffold_code=out['code'])))
    store.close()
asyncio.run(main())
