import asyncio,json
from prior_probe import setup,applied,agent_for,MessageBus,InboxWakePump,exists,SessionManager,RecoveryCoordinator

async def main():
    tmp,manager,state,key,session,inbox,cap=await setup('explicit-stop')
    bus=MessageBus();agent,provider=agent_for(tmp,manager,inbox,bus)
    entered=asyncio.Event()
    async def hold(messages,**kwargs):
        provider.calls.append(messages);entered.set();await asyncio.Event().wait()
    provider.chat=hold
    runner=asyncio.create_task(agent.run())
    try:
        await applied(inbox,cap)
        await InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k)).pump_once()
        await asyncio.wait_for(entered.wait(),10)
        before=await state.read(key)
        assert before.metadata.get('pending_user_turn') is True, 'the live turn holds the marker'
        report=await agent.stop_session_work(key)
        settled=await state.read(key)
        print(json.dumps({'stage':'explicit_stop','cancelled':report.cancelled,'pending':settled.metadata.get('pending_user_turn'),'owner':settled.metadata.get('pending_user_turn_owner')}))
        assert settled.metadata.get('pending_user_turn') is None, 'an explicit stop ends the obligation'
        assert settled.metadata.get('pending_user_turn_owner') is None, 'the owner key must not survive'
    finally:
        runner.cancel();await asyncio.gather(runner,return_exceptions=True);await agent.aclose()
    manager2=SessionManager(tmp/'workspace')
    await RecoveryCoordinator(manager2,MessageBus()).scan();after=await manager2.state.read(key)
    print(json.dumps({'stage':'restart','recovery':after.metadata.get('webui_recovery')}))
    assert after.metadata.get('webui_recovery') is None, 'an explicit stop must not offer recovery'
    await manager2.state.aclose()
asyncio.run(main())
