from __future__ import annotations

import asyncio

from userbot.commands import CommandDispatcher


class FakeEvent:
    def __init__(self, text: str, sender_id: int = 1):
        self.raw_text = text
        self.sender_id = sender_id
        self.responses: list[str] = []

    async def respond(self, text: str, **kwargs) -> None:
        self.responses.append(text)


def test_dispatcher_only_accepts_owner_and_routes_alias() -> None:
    async def scenario() -> None:
        dispatcher = CommandDispatcher({1})
        calls: list[str] = []

        async def callback(command) -> None:
            calls.append(command.args)
            await command.respond(f"ok:{command.args}")

        dispatcher.register("test", callback, aliases=("check",))
        owner_event = FakeEvent("/ub check value", sender_id=1)
        await dispatcher.handle_event(owner_event)
        foreign_event = FakeEvent("/ub test value", sender_id=2)
        await dispatcher.handle_event(foreign_event)
        assert calls == ["value"]
        assert owner_event.responses == ["ok:value"]
        assert foreign_event.responses == []

    asyncio.run(scenario())


def test_dispatcher_rejects_conflicting_alias_without_partial_registration() -> None:
    dispatcher = CommandDispatcher({1})

    async def callback(command) -> None:
        return None

    dispatcher.register("first", callback)
    try:
        dispatcher.register("second", callback, aliases=("first",))
    except ValueError:
        pass
    else:
        raise AssertionError("Expected command conflict")
    assert [item.name for item in dispatcher.commands()] == ["first"]
