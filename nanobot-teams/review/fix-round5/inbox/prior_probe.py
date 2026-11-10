import asyncio, json, sys, tempfile
from copy import deepcopy
from loguru import logger
logger.remove()
from pathlib import Path
ROOT = Path(r'C:\Users\KDB\orca\workspaces\nanobot\nanobot-teams-mvp')
sys.path.insert(0, str(ROOT))
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import Config
from nanobot.providers.unconfigured_provider import UnconfiguredProvider
from nanobot.providers.base import LLMResponse
from nanobot.session.external_inbox import ExternalInbox, InboxDelivery, delivery_body_digest
from nanobot.session.inbox_wake import InboxWakePump
from nanobot.session.manager import SessionManager
from nanobot.cli.gateway_runtime import mint_teams_meta
from nanobot.webui.recovery import RecoveryCoordinator
from nanobot.agent.tools.context import RequestContext, request_context, current_request_context
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nanobot.api.agent_inbox import register_inbox_routes
from nanobot.config.schema import ApiRunsConfig

BASE = Path(__file__).parent
class FakeProvider(UnconfiguredProvider):
    def __init__(self):
        super().__init__('fake-review-model')
        self.calls=[]
        self.contexts=[]
    async def chat(self, messages, **kwargs):
        self.calls.append(deepcopy(messages))
        ctx=current_request_context()
        self.contexts.append(None if ctx is None else dict(source_workflow=ctx.source_workflow, generation=ctx.session_generation, original_user_text=ctx.original_user_text))
        return LLMResponse(content='Reviewed fake response', finish_reason='stop')

async def setup(name):
    tmp=Path(tempfile.mkdtemp(prefix=name+'-',dir=BASE))
    manager=SessionManager(tmp/'workspace')
    state=manager.state
    key='websocket:review-chat'
    await seed(state,key,'research then write')
    session=await state.read(key)
    inbox=ExternalInbox(state)
    cap=inbox.mint_capability(session_key=key,session_generation=session.generation,workflow_id='wf-root',operation_key='op-1',bot_id='research',instance_id='research-instance',principal_id='teams-coordinator')
    return tmp,manager,state,key,session,inbox,cap
async def seed(state,key,text):
    await state._execute(key,lambda:state._change(key,lambda s:s.add_message('user',text),create_if_missing=True),operation_name='seed')
def delivery(cap,**changes):
    args=dict(delivery_id='del-1',session_key=cap.session_key,session_generation=cap.session_generation,workflow_id=cap.workflow_id,task_id='task-1',bot_id='research',instance_id='research-instance',run_id='run-1',terminal_revision=1,outcome_code='succeeded',result_text='REVIEW_EXTERNAL_DATA',result_truncated=False)
    args.update(changes)
    args['body_digest']=delivery_body_digest(**{k:args[k] for k in ('task_id','bot_id','instance_id','run_id','terminal_revision','outcome_code','result_text')})
    return InboxDelivery(**args)
def agent_for(tmp,manager,inbox,bus):
    provider=FakeProvider()
    agent=AgentLoop.from_config(Config.model_validate({'agents':{'defaults':{'workspace':str(tmp/'workspace')}}}),bus,provider=provider,session_manager=manager,tool_registry=ToolRegistry())
    agent.external_inbox=inbox
    return agent,provider
async def exists(state,key):return await state.read(key) is not None
async def applied(inbox,cap):
    inbox.receive(delivery(cap),capability_digest=cap.digest)
    return await inbox.apply_pending('del-1')

async def lost_queued():
    tmp,manager,state,key,session,inbox,cap=await setup('lost-queued')
    bus=MessageBus()
    await applied(inbox,cap)
    pump=InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k))
    queued=await pump.pump_once()
    before=dict(queued=queued,bus_size=bus.inbound_size,due=inbox.wake_due_deliveries())
    await state.aclose()
    manager2=SessionManager(tmp/'workspace')
    inbox2=ExternalInbox(manager2.state)
    bus2=MessageBus()
    agent,provider=agent_for(tmp,manager2,inbox2,bus2)
    task=asyncio.create_task(agent.run())
    try:
        recovery=RecoveryCoordinator(manager2,bus2)
        await recovery.scan()
        recovered_session=await manager2.state.read(key)
        recover_pending=inbox2.pending_deliveries()
        for d in recover_pending:await inbox2.apply_pending(d)
        retry=await InboxWakePump(inbox2,bus2.publish_inbound,lambda k:exists(manager2.state,k)).pump_once()
        await asyncio.sleep(.15)
        print('LOST_QUEUED',json.dumps(dict(before=before,recovery_metadata=recovered_session.metadata.get('webui_recovery'),runtime_checkpoint=recovered_session.metadata.get('runtime_checkpoint'),recovery_pending=recover_pending,recovered_wakes=retry,planner_calls=len(provider.calls),receipt=inbox2.get_receipt('del-1').state,expected='durable wake remains recoverable until processing is committed')))
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True);await agent.aclose()

async def replaced_generation():
    tmp,manager,state,key,session,inbox,cap=await setup('replaced')
    await applied(inbox,cap)
    await state.delete(key)
    await seed(state,key,'NEW UNRELATED INTENT')
    replacement=await state.read(key)
    bus=MessageBus()
    agent,provider=agent_for(tmp,manager,inbox,bus)
    try:
        queued=await InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k)).pump_once()
        msg=await bus.consume_inbound() if bus.inbound_size else None
        queued_generation=None if msg is None else msg.session_generation
        if msg:await agent._process_message(msg)
        print('REPLACED_GENERATION',json.dumps(dict(old_generation=session.generation,new_generation=replacement.generation,queued=queued,message_generation_before_processing=queued_generation,planner_calls=len(provider.calls),saw_new_intent=any('NEW UNRELATED INTENT' in str(call) for call in provider.calls),saw_old_report=any('REVIEW_EXTERNAL_DATA' in str(call) for call in provider.calls),contexts=provider.contexts,receipt=inbox.get_receipt('del-1').state,expected='orphaned receipt and no planner call in replacement generation')))
    finally:await agent.aclose()

async def revoked_after_inject():
    tmp,manager,state,key,session,inbox,cap=await setup('revoked-after-inject')
    await applied(inbox,cap)
    bus=MessageBus()
    agent,provider=agent_for(tmp,manager,inbox,bus)
    try:
        await agent._process_message(InboundMessage(channel='websocket',sender_id='user',chat_id='review-chat',content='NEW UNRELATED INTENT',input_role='user'))
        user_calls=len(provider.calls)
        queued=await InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k)).pump_once()
        if bus.inbound_size:await agent._process_message(await bus.consume_inbound())
        print('REVOKED_AFTER_INJECT',json.dumps(dict(grant=inbox.workflow_allows_auto_continue('wf-root'),user_calls=user_calls,queued=queued,extra_planner_calls=len(provider.calls)-user_calls,contexts=provider.contexts,receipt=inbox.get_receipt('del-1').state,expected='new real user turn revokes old wake; zero extra planner calls')))
    finally:await agent.aclose()

async def happy_path():
    tmp,manager,state,key,session,inbox,cap=await setup('happy')
    bus=MessageBus();agent,provider=agent_for(tmp,manager,inbox,bus)
    task=asyncio.create_task(agent.run())
    try:
        await applied(inbox,cap)
        await InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k)).pump_once()
        for _ in range(100):
            if provider.calls:break
            await asyncio.sleep(.01)
        await asyncio.sleep(.05)
        replay=str(provider.calls[-1]) if provider.calls else ''
        print('HAPPY_PATH',json.dumps(dict(planner_calls=len(provider.calls),source_workflow=provider.contexts[-1] if provider.contexts else None,non_authority_envelope='external data' in replay and 'not a user instruction or approval' in replay,receipt=inbox.get_receipt('del-1').state)))
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True);await agent.aclose()

async def closures():
    tmp,manager,state,key,session,inbox,cap=await setup('closures')
    bus=MessageBus();agent,provider=agent_for(tmp,manager,inbox,bus)
    app=web.Application();register_inbox_routes(app,inbox,ApiRunsConfig(enabled=True,inbox_enabled=True,api_key='synthetic-review'))
    client=TestClient(TestServer(app));await client.start_server()
    try:
        ctx=RequestContext(channel='websocket',chat_id='review-chat',session_key=key,session_generation=session.generation,turn_id='report-turn',source_workflow='wf-root')
        with request_context(ctx):root=mint_teams_meta(inbox,'teams-coordinator')['reply_workflow']
        with request_context(RequestContext(channel='api',chat_id='api',session_key=key,session_generation=session.generation,persist_session=False,can_receive_background_results=False)):incapable=mint_teams_meta(inbox,'teams-coordinator')
        await inbox.revoke_session_workflows_async(key)
        payload=dict(delivery_id='del-late',reply_capability=cap.token,operation_key='op-1',bot_id='research',instance_id='research-instance',task_id='task-1',run_id='run-1',terminal_revision=1,outcome={'code':'succeeded'},result_text='LATE REPORT',result_truncated=False,workflow_id='wf-root')
        r=await client.post('/v1/agent-inbox',json=payload,headers={'Authorization':'Bearer synthetic-review'});late=await r.json()
        r2=await client.post('/v1/agent-inbox',json={**payload,'delivery_id':'del-other','task_id':'OTHER','run_id':'OTHER'},headers={'Authorization':'Bearer synthetic-review'});binding=await r2.json()
        print('CLOSURES',json.dumps(dict(continued_root=root,incapable=incapable,late_http=r.status,late=late,rebind_http=r2.status,rebind=binding)))
    finally:await client.close();await agent.aclose()


async def active_result():
    tmp,manager,state,key,session,inbox,cap=await setup('active-result')
    bus=MessageBus();agent,provider=agent_for(tmp,manager,inbox,bus)
    entered=asyncio.Event();release=asyncio.Event();second=asyncio.Event()
    original_chat=provider.chat
    meta={}
    async def blocked_chat(messages,**kwargs):
        result=await original_chat(messages,**kwargs)
        if len(provider.calls)==1:
            meta.update(mint_teams_meta(inbox,'teams-coordinator'))
            entered.set()
            await release.wait()
        else:second.set()
        return result
    provider.chat=blocked_chat
    task=asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(InboundMessage(channel='websocket',sender_id='user',chat_id='review-chat',content='research then write',input_role='user'))
        await asyncio.wait_for(entered.wait(),10)
        active_cap=inbox.validate_capability(token=meta['reply_capability'],workflow_id=meta['reply_workflow'],operation_key='op-1',bot_id='research',instance_id='research-instance',principal_id='teams-coordinator')
        await applied(inbox,active_cap)
        await InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k)).pump_once()
        for _ in range(100):
            if bus.inbound_size==0:break
            await asyncio.sleep(.01)
        release.set()
        await asyncio.wait_for(second.wait(),10)
        for _ in range(100):
            if not agent._pending_queues:break
            await asyncio.sleep(.01)
        current=await state.read(key)
        print('ACTIVE_RESULT',json.dumps(dict(planner_calls=len(provider.calls),first_saw_report='REVIEW_EXTERNAL_DATA' in str(provider.calls[0]),second_saw_report='REVIEW_EXTERNAL_DATA' in str(provider.calls[1]),second_saw_wake='Automated continuation' in str(provider.calls[1]),second_context=provider.contexts[1],history_report_count=sum('REVIEW_EXTERNAL_DATA' in str(m.get('content')) for m in current.messages),due=inbox.wake_due_deliveries(),receipt=inbox.get_receipt('del-1').state,expected='followup sees actual report under original workflow and report remains in history')))
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);await agent.aclose()

async def malformed_revision():
    tmp,manager,state,key,session,inbox,cap=await setup('malformed')
    app=web.Application();register_inbox_routes(app,inbox,ApiRunsConfig(enabled=True,inbox_enabled=True,api_key='synthetic-review'))
    client=TestClient(TestServer(app));await client.start_server()
    try:
        payload=dict(delivery_id='del-late',reply_capability=cap.token,operation_key='op-1',bot_id='research',instance_id='research-instance',task_id='task-1',run_id='run-1',terminal_revision='bad',outcome={'code':'succeeded'},result_text='REPORT',result_truncated=False,workflow_id='wf-root')
        r=await client.post('/v1/agent-inbox',json=payload,headers={'Authorization':'Bearer synthetic-review'})
        print('MALFORMED_REVISION',json.dumps(dict(http=r.status,content_type=r.content_type,body=await r.text(),expected='structured 400 invalid_field')))
    finally:await client.close();await state.aclose()


async def queued_child():
    tmp,manager,state,key,session,inbox,cap=await setup('kill-queued')
    bus=MessageBus()
    await applied(inbox,cap)
    queued=await InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k)).pump_once()
    print(json.dumps(dict(tmp=str(tmp),key=key,queued=queued,bus_size=bus.inbound_size,due=inbox.wake_due_deliveries())),flush=True)
    await asyncio.Event().wait()

async def crash_queued():
    import subprocess
    child=subprocess.Popen([sys.executable,str(Path(__file__)),'queued_child'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
    try:
        line=await asyncio.wait_for(asyncio.to_thread(child.stdout.readline),15)
        before=json.loads(line)
    finally:
        child.kill();await asyncio.to_thread(child.wait)
    tmp=Path(before['tmp']);key=before['key']
    manager=SessionManager(tmp/'workspace');state=manager.state;inbox=ExternalInbox(state);bus=MessageBus()
    await RecoveryCoordinator(manager,bus).scan()
    recover_pending=inbox.pending_deliveries()
    for d in recover_pending:await inbox.apply_pending(d)
    recovered=await InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k)).pump_once()
    agent,provider=agent_for(tmp,manager,inbox,bus)
    task=asyncio.create_task(agent.run())
    try:
        await asyncio.sleep(.15)
        session=await state.read(key)
        print('CRASH_QUEUED',json.dumps(dict(before=before,child_exit=child.returncode,recovery_pending=recover_pending,recovered_wakes=recovered,planner_calls=len(provider.calls),receipt=inbox.get_receipt('del-1').state,recovery_metadata=session.metadata.get('webui_recovery'),runtime_checkpoint=session.metadata.get('runtime_checkpoint'),expected='obligation still visible or explicit recovery offered after process dies before dequeue')))
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True);await agent.aclose()


async def injected_user_revocation():
    tmp,manager,state,key,session,inbox,cap=await setup('injected-user')
    bus=MessageBus();agent,provider=agent_for(tmp,manager,inbox,bus)
    entered=asyncio.Event();release=asyncio.Event();second=asyncio.Event();meta={}
    original_chat=provider.chat
    async def blocked_chat(messages,**kwargs):
        result=await original_chat(messages,**kwargs)
        if len(provider.calls)==1:
            meta.update(mint_teams_meta(inbox,'teams-coordinator'))
            entered.set();await release.wait()
        else:second.set()
        return result
    provider.chat=blocked_chat
    task=asyncio.create_task(agent.run())
    try:
        await bus.publish_inbound(InboundMessage(channel='websocket',sender_id='user',chat_id='review-chat',content='research then write',input_role='user'))
        await asyncio.wait_for(entered.wait(),10)
        active_cap=inbox.validate_capability(token=meta['reply_capability'],workflow_id=meta['reply_workflow'],operation_key='op-1',bot_id='research',instance_id='research-instance',principal_id='teams-coordinator')
        await bus.publish_inbound(InboundMessage(channel='websocket',sender_id='user',chat_id='review-chat',content='NEW UNRELATED INTENT',input_role='user'))
        for _ in range(100):
            if bus.inbound_size==0:break
            await asyncio.sleep(.01)
        release.set();await asyncio.wait_for(second.wait(),10)
        for _ in range(100):
            if not agent._pending_queues:break
            await asyncio.sleep(.01)
        receipt=inbox.receive(delivery(active_cap),capability_digest=active_cap.digest)
        result=await inbox.apply_pending('del-1')
        print('INJECTED_USER_REVOCATION',json.dumps(dict(planner_calls=len(provider.calls),second_saw_new_intent='NEW UNRELATED INTENT' in str(provider.calls[1]),old_workflow_grant=inbox.workflow_allows_auto_continue(active_cap.workflow_id),late_receive=receipt.state,late_apply=result,wake_due=len(inbox.wake_due_deliveries()),expected='every accepted genuine new user input revokes old workflow, late result card_only')))
    finally:
        release.set();task.cancel();await asyncio.gather(task,return_exceptions=True);await agent.aclose()

async def main():
    names=sys.argv[1:] or ['happy_path','lost_queued','replaced_generation','revoked_after_inject','closures']
    for name in names:await globals()[name]()
if __name__=='__main__':asyncio.run(main())


