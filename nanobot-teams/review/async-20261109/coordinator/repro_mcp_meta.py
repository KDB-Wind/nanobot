import asyncio,json,os,sys,uuid
from pathlib import Path
from aiohttp import web
from aiohttp.test_utils import TestServer
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters,stdio_client

async def main():
    received=[]
    async def submit(req):
        received.append(await req.json());return web.json_response({'task_id':'task_meta_proof'},status=201)
    app=web.Application();app.router.add_post('/v1/teams/tasks',submit)
    async with TestServer(app) as server:
        env=dict(os.environ);env['AUDIT_PLANNER_TOKEN']='synthetic-planner';env['PYTHONPATH']=str(Path.cwd()/'nanobot-teams'/'src')
        params=StdioServerParameters(command=sys.executable,args=['-m','nanobot_teams','async-mcp','--url',str(server.make_url('/')),'--token-env','AUDIT_PLANNER_TOKEN'],env=env)
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                result=await session.call_tool('submit_task',{'project':'p','bot':'research','input':'synthetic','operation_key':str(uuid.uuid4()),'delivery_mode':'auto_resume'},meta={'reply_capability':'synthetic-host-minted-capability','reply_workflow':'trusted-workflow','coordinator_principal':'teams-coordinator'})
                print(json.dumps(dict(is_error=result.isError,mcp_reply=[x.text for x in result.content if hasattr(x,'text')],coordinator_submit_requests=len(received))))
                assert result.isError and len(received)==0
    print('REPRODUCED valid_mcp_meta_lost')
asyncio.run(main())
