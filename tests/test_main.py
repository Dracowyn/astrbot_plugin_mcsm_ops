"""Exercise command gates and console readback without starting AstrBot."""

import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_mcsm_ops.main import Plugin


class FakeEvent:
    """Provide only the event surface used by these handlers."""

    def __init__(self, message="/mcs list", platform="aiocqhttp"):
        self.message_str = message
        self.platform = platform
        self.unified_msg_origin = "test:GroupMessage:123"

    def get_platform_name(self):
        return self.platform

    def plain_result(self, text):
        return text


class FakeClient:
    """Supply deterministic instance state and before/after console output."""

    def __init__(self):
        self.commands = []
        self.stops = 0
        self.logs = iter(
            [
                "old1\nold2\nold3",
                "old1\nold2\nold3\n[Server thread/INFO]: There are 2 of a max of 20 players online: Alice, Bob",
            ]
        )

    def redact(self, text):
        return str(text)

    async def detail(self):
        return {"status": 3}

    async def outputlog(self, size):
        return next(self.logs)

    async def send_command(self, command):
        self.commands.append(command)
        return True

    async def stop(self):
        self.stops += 1


@pytest.fixture
def plugin():
    """Assemble handlers with an injected client and no framework runtime."""
    instance = Plugin.__new__(Plugin)
    instance.config = {
        "panel_url": "https://example.invalid",
        "api_key": "test-credential",
        "daemon_id": "node",
        "instance_uuid": "instance",
        "allowed_platforms": ["aiocqhttp"],
        "command_wait_seconds": 0.6,
    }
    instance.client = FakeClient()
    instance.context = SimpleNamespace()
    instance._command_lock = asyncio.Lock()
    instance._tasks = set()
    instance._operation_active = False
    return instance


async def collect(generator):
    """Collect asynchronous handler replies for synchronous pytest tests."""
    return [result async for result in generator]


def test_disallowed_platform_has_no_reply(plugin):
    event = FakeEvent(platform="mcqq")
    plugin.config = {}
    assert asyncio.run(collect(plugin.list_players(event))) == []
    assert plugin.client.commands == []


def test_missing_configuration_names_fields(plugin):
    plugin.config["api_key"] = ""
    plugin.config["daemon_id"] = ""
    replies = asyncio.run(collect(plugin.status(FakeEvent("/mcs status"))))
    assert replies == ["插件未配置：缺少 api_key、daemon_id"]


def test_multiline_command_is_rejected(plugin):
    event = FakeEvent("/mcs cmd say hello\nstop")
    replies = asyncio.run(collect(plugin.cmd(event, "say hello stop")))
    assert replies == ["拒绝命令：不能包含换行或控制字符"]
    assert plugin.client.commands == []


def test_list_reads_fresh_command_output(plugin):
    replies = asyncio.run(collect(plugin.list_players(FakeEvent())))
    assert replies == ["在线 2/20：Alice, Bob"]
    assert plugin.client.commands == ["list"]


def test_restart_poll_waits_through_stopped_state(plugin, monkeypatch):
    states = iter([3, 1, 0, 2, 3])
    sent = []

    async def detail():
        return {"status": next(states)}

    async def no_sleep(_seconds):
        return None

    async def send_message(origin, chain):
        sent.append(chain.get_plain_text())

    plugin.client.detail = detail
    plugin.context.send_message = send_message
    monkeypatch.setattr("astrbot_plugin_mcsm_ops.main.asyncio.sleep", no_sleep)

    async def run():
        started = asyncio.get_running_loop().time()
        await plugin._poll("origin", "restart", started)

    asyncio.run(run())
    assert len(sent) == 1 and sent[0].startswith("服务器已重启")
    # The initial "running" sample must not end the wait early.
    assert next(states, None) is None


def test_stop_requires_confirmation(plugin):
    replies = asyncio.run(collect(plugin.stop(FakeEvent("/mcs stop"))))
    assert replies == ["此操作会让在线玩家掉线，确认请发送 /mcs stop 确认"]
    assert plugin.client.stops == 0
