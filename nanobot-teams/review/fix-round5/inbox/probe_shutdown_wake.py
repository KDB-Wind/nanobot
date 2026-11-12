import asyncio,json
from prior_probe import setup,applied,agent_for,MessageBus,InboxWakePump,exists,SessionManager,RecoveryCoordinator

async def main():
    tmp,manager,state,key,session,inbox,cap=await setup('cancelled-wake')
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
        assert before.metadata.get('pending_user_turn') is True
        agent.preserve_inflight_turns_on_shutdown()
        await agent.aclose()
    finally:
        runner.cancel();await asyncio.gather(runner,return_exceptions=True);await agent.aclose()
    manager2=SessionManager(tmp/'workspace')
    persisted=await manager2.state.read(key)
    assert persisted.metadata.get('pending_user_turn') is True, 'shutdown must preserve pending acceptance'
    assert persisted.metadata.get('pending_user_turn_owner') is not None
    await RecoveryCoordinator(manager2,MessageBus()).scan();after=await manager2.state.read(key)
    print(json.dumps({'stage':'restart_after_shutdown','provider_calls':len(provider.calls),'pending_before_shutdown':before.metadata.get('pending_user_turn'),'pending_after_shutdown':persisted.metadata.get('pending_user_turn'),'owner_after_shutdown':persisted.metadata.get('pending_user_turn_owner'),'recovery':after.metadata.get('webui_recovery')}))
    assert after.metadata.get('webui_recovery',{}).get('reason')=='restart_requires_confirmation'
    await manager2.state.aclose()
asyncio.run(main())


