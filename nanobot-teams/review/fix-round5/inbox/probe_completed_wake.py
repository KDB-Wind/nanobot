import asyncio,json
from prior_probe import setup,applied,agent_for,MessageBus,InboxWakePump,exists,SessionManager,RecoveryCoordinator

async def main():
    tmp,manager,state,key,session,inbox,cap=await setup('completed-wake')
    bus=MessageBus();agent,provider=agent_for(tmp,manager,inbox,bus)
    runner=asyncio.create_task(agent.run())
    try:
        await applied(inbox,cap)
        queued=await InboxWakePump(inbox,bus.publish_inbound,lambda k:exists(state,k)).pump_once()
        for _ in range(200):
            current=await state.read(key)
            if provider.calls and any(m.get('content')=='Reviewed fake response' for m in current.messages) and not agent._pending_queues:
                break
            await asyncio.sleep(.01)
        before=await state.read(key)
        assert len(provider.calls)==1, 'wake must complete one actual provider call'
        assert any(m.get('content')=='Reviewed fake response' for m in before.messages), 'response must be committed before restart'
        assert not agent._pending_queues and not inbox.wake_due_deliveries(), 'completed turn has no active queue or wake obligation'
        assert before.metadata.get('runtime_checkpoint') is None, 'completed turn cleared its model checkpoint'
        print(json.dumps({'stage':'completed','queued':queued,'provider_calls':len(provider.calls),'pending_user_turn':before.metadata.get('pending_user_turn'),'checkpoint':before.metadata.get('runtime_checkpoint'),'due':inbox.wake_due_deliveries(),'active_queues':list(agent._pending_queues),'history':[{'role':m.get('role'),'content':m.get('content')} for m in before.messages]}))
    finally:
        runner.cancel();await asyncio.gather(runner,return_exceptions=True);await agent.aclose()
    manager2=SessionManager(tmp/'workspace');bus2=MessageBus()
    await RecoveryCoordinator(manager2,bus2).scan()
    after=await manager2.state.read(key)
    print(json.dumps({'stage':'restart','pending_user_turn':after.metadata.get('pending_user_turn'),'recovery':after.metadata.get('webui_recovery'),'checkpoint':after.metadata.get('runtime_checkpoint')}))
    await manager2.state.aclose()
    assert before.metadata.get('pending_user_turn') is not True, 'completed wake must release its persisted pending marker'
    assert after.metadata.get('webui_recovery') is None, 'completed wake must not create restart recovery'

asyncio.run(main())


