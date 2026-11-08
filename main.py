"""MCSManager instance operations exposed through AstrBot commands."""

import asyncio
import re
import traceback
import unicodedata

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .console import (
    DEFAULT_LIST_PATTERN,
    DEFAULT_TPS_PATTERN,
    clean_log,
    diff_output,
    format_duration,
    format_memory,
    mask_ip,
    parse_list,
    parse_tps,
)
from .mcsm_client import McsmClient, McsmError

STATUS_NAMES = {-1: "忙碌", 0: "已停止", 1: "停止中", 2: "启动中", 3: "运行中"}
MAX_LINE_CHARS = 500
MAX_OUTPUT_CHARS = 4000


class Plugin(Star):
    """Manage one configured MCSManager instance."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = McsmClient(
            config.get("panel_url", ""),
            config.get("api_key", ""),
            config.get("daemon_id", ""),
            config.get("instance_uuid", ""),
            timeout=config.get("request_timeout_seconds", 10),
        )
        self._command_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._operation_active = False

    def _gate(self, event):
        """Return None when allowed, an empty silent denial, or a configuration notice.

        Args:
            event: Incoming platform event.

        Returns:
            Optional denial text; an empty string must not produce a reply.
        """
        if event.get_platform_name() not in self.config.get(
            "allowed_platforms", ["aiocqhttp"]
        ):
            return ""
        missing = [
            key
            for key in ("panel_url", "api_key", "daemon_id", "instance_uuid")
            if not str(self.config.get(key, "") or "").strip()
        ]
        return "插件未配置：缺少 " + "、".join(missing) if missing else None

    def _safe_text(self, text):
        """Remove the configured credential from any outgoing text.

        Args:
            text: Console, panel, or exception text.

        Returns:
            Text with credential occurrences removed.
        """
        return self.client.redact(text)

    def _error(self, exc):
        """Log a sanitized failure and return the corresponding user message.

        Args:
            exc: Caught request or internal exception.

        Returns:
            Safe Chinese error text.
        """
        if isinstance(exc, McsmError):
            message = self._safe_text(exc)
            logger.warning("MCSManager request failed: %s", message)
            return f"MCSManager 请求失败：{message}"
        # Format without locals, redact, and suppress automatic raw exception logging.
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        logger.exception(
            "MCSManager internal error:\n%s", self._safe_text(trace), exc_info=False
        )
        return "内部错误，请查看日志"

    def _arguments(self, event, subcommand):
        """Read unnormalized arguments after an exact command token.

        Args:
            event: Event containing the original message string.
            subcommand: The registered child command name.

        Returns:
            Original argument suffix, with outer whitespace removed.
        """
        match = re.search(
            r"(?:^|\s)mcs\s+" + subcommand + r"(?=\s|$)", event.message_str.lstrip("/")
        )
        return event.message_str.lstrip("/")[match.end() :].strip() if match else ""

    async def _read_command(self, command, parser=None, pattern=""):
        """Serialize command output capture and optionally retry one delayed match.

        Args:
            command: Single console command.
            parser: Optional list or TPS parser used to detect delayed output.
            pattern: User-configured parser expression.

        Returns:
            New cleaned lines and whether the anchor was lost.
        """
        async with self._command_lock:
            size = self.config.get("log_fetch_kb", 64)
            delay = max(0.6, float(self.config.get("command_wait_seconds", 1.5)))
            before = await self.client.outputlog(size)
            await self.client.send_command(command)
            await asyncio.sleep(delay)
            lines, rotated = diff_output(before, await self.client.outputlog(size))
            if parser is not None and parser(lines, pattern) is None:
                await asyncio.sleep(delay)
                lines, rotated = diff_output(before, await self.client.outputlog(size))
            return lines, rotated

    @filter.command_group("mcs")
    def mcs(self):
        """Query and operate the configured Minecraft server."""

    @mcs.command("status")
    async def status(self, event: AstrMessageEvent):
        """Display instance details and optional TPS."""
        gate = self._gate(event)
        if gate is not None:
            if gate:
                yield event.plain_result(gate)
            return
        try:
            detail = await self.client.detail()
            info, process, config = (
                detail.get(key) or {} for key in ("info", "processInfo", "config")
            )
            cpu = process.get("cpu")
            cpu_text = f"{cpu:.1f}%" if isinstance(cpu, (int, float)) else "未知"
            lines = [
                f"名称：{config.get('nickname') or '未知'}",
                f"状态：{STATUS_NAMES.get(detail.get('status'), '未知')}",
                f"在线：{info.get('currentPlayers', '未知')}/{info.get('maxPlayers', '未知')}",
                f"版本：{info.get('version') or '未知'}",
                f"CPU：{cpu_text}",
                f"内存：{format_memory(process.get('memory'))}",
                f"运行时长：{format_duration(process.get('elapsed'))}",
            ]
            command = self.config.get("tps_command", "neoforge tps").strip()
            if detail.get("status") == 3 and command and self._command_lock.locked():
                # Public commands must not queue behind each other or an admin command.
                lines.append("TPS：繁忙，请稍后再试")
            elif detail.get("status") == 3 and command:
                tps = None
                try:
                    pattern = self.config.get("tps_regex", DEFAULT_TPS_PATTERN)
                    output, _ = await self._read_command(command, parse_tps, pattern)
                    tps = parse_tps(output, pattern)
                except Exception as exc:
                    self._error(exc)
                lines.append(
                    f"TPS：{tps[0]:.3f} / MSPT：{tps[1]:.3f}"
                    if tps
                    else "TPS：获取失败"
                )
            yield event.plain_result(self._safe_text("\n".join(lines)))
        except Exception as exc:
            yield event.plain_result(self._error(exc))

    @mcs.command("list")
    async def list_players(self, event: AstrMessageEvent):
        """Read the online player list from fresh console output."""
        gate = self._gate(event)
        if gate is not None:
            if gate:
                yield event.plain_result(gate)
            return
        try:
            if (await self.client.detail()).get("status") != 3:
                yield event.plain_result("服务器未运行")
                return
            if self._command_lock.locked():
                yield event.plain_result("服务器正忙，请稍后再试")
                return
            pattern = self.config.get("list_regex", DEFAULT_LIST_PATTERN)
            lines, _ = await self._read_command(
                self.config.get("list_command", "list"), parse_list, pattern
            )
            parsed = parse_list(lines, pattern)
            if parsed is None:
                text = "未能解析在线列表"
            else:
                count, maximum, names = parsed
                text = (
                    f"在线 {count}/{maximum}：{', '.join(names)}"
                    if count
                    else f"当前没有玩家在线（0/{maximum}）"
                )
            yield event.plain_result(self._safe_text(text))
        except Exception as exc:
            yield event.plain_result(self._error(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("log")
    async def log(self, event: AstrMessageEvent):
        """Send filtered console logs, using merged forwarding on OneBot."""
        gate = self._gate(event)
        if gate is not None:
            if gate:
                yield event.plain_result(gate)
            return
        try:
            arguments = self._arguments(event, "log")
            tokens = arguments.split(maxsplit=1)
            count = max(1, int(self.config.get("log_default_lines", 20)))
            if tokens and tokens[0].isdecimal() and int(tokens[0]) > 0:
                count = int(tokens[0])
                arguments = tokens[1] if len(tokens) > 1 else ""
            count = min(count, max(1, int(self.config.get("log_max_lines", 100))))
            lines = clean_log(
                await self.client.outputlog(self.config.get("log_fetch_kb", 64))
            )
            lines = [line for line in lines if arguments.casefold() in line.casefold()][
                -count:
            ]
            if not lines:
                yield event.plain_result("没有匹配的日志")
                return
            # Bound line and total length so long stack traces stay under QQ limits.
            text = "\n".join(line[:MAX_LINE_CHARS] for line in lines)
            text = self._safe_text(text[-MAX_OUTPUT_CHARS:])
            if self.config.get("log_mask_ip", True):
                text = mask_ip(text)
            if event.get_platform_name() == "aiocqhttp":
                node = Comp.Node(
                    uin=event.get_self_id(),
                    name="服务器日志",
                    content=[Comp.Plain(text)],
                )
                yield event.chain_result([Comp.Nodes([node])])
            else:
                yield event.plain_result(text)
        except Exception as exc:
            yield event.plain_result(self._error(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("cmd")
    async def cmd(self, event: AstrMessageEvent, command: GreedyStr):
        """Send a single-line command and capture new console output.

        Args:
            event: Incoming administrator event.
            command: Greedy command text supplied by AstrBot.
        """
        gate = self._gate(event)
        if gate is not None:
            if gate:
                yield event.plain_result(gate)
            return
        try:
            command = self._arguments(event, "cmd") or str(command).strip()
            # Control, format and line/paragraph separator characters could split
            # or alter the console line (e.g. in PTY mode), so reject them all.
            # The raw message is checked too: GreedyStr folds newlines into spaces.
            if any(
                unicodedata.category(char) in ("Cc", "Cf", "Zl", "Zp")
                for char in command + event.message_str
            ):
                yield event.plain_result("拒绝命令：不能包含换行或控制字符")
                return
            command = command.removeprefix("/").strip()
            if not command:
                yield event.plain_result("用法：/mcs cmd <命令>")
                return
            lines, rotated = await self._read_command(command)
            text = (
                self._safe_text(
                    "\n".join(line[:MAX_LINE_CHARS] for line in lines[-30:])[
                        -MAX_OUTPUT_CHARS:
                    ]
                )
                if lines
                else "命令已发送，无输出"
            )
            if self.config.get("log_mask_ip", True):
                text = mask_ip(text)
            if rotated:
                text += "\n日志已轮转，结果可能不完整"
            yield event.plain_result(text)
        except Exception as exc:
            yield event.plain_result(self._error(exc))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("start")
    async def start(self, event: AstrMessageEvent):
        """Start the instance and monitor completion in the background."""
        async for result in self._operate(event, "start"):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("stop")
    async def stop(self, event: AstrMessageEvent):
        """Stop the instance only after explicit confirmation."""
        async for result in self._operate(event, "stop"):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("restart")
    async def restart(self, event: AstrMessageEvent):
        """Restart the instance only after explicit confirmation."""
        async for result in self._operate(event, "restart"):
            yield result

    async def _operate(self, event, operation):
        """Gate and reserve one operation before making any request.

        Args:
            event: Administrator event and completion reply destination.
            operation: One of start, stop, restart.

        Yields:
            Immediate acknowledgement, denial, or request error.
        """
        gate = self._gate(event)
        if gate is not None:
            if gate:
                yield event.plain_result(gate)
            return
        if operation != "start" and self._arguments(event, operation) != "确认":
            yield event.plain_result(
                f"此操作会让在线玩家掉线，确认请发送 /mcs {operation} 确认"
            )
            return
        if self._operation_active:
            yield event.plain_result("已有启停操作在进行中")
            return
        self._operation_active = True
        task = None
        try:
            if operation == "start" and (await self.client.detail()).get("status") == 3:
                yield event.plain_result("服务器已在运行")
                return
            started = asyncio.get_running_loop().time()
            await getattr(self.client, operation)()
            task = asyncio.create_task(
                self._poll(event.unified_msg_origin, operation, started)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            label = {"start": "启动", "stop": "停止", "restart": "重启"}[operation]
            yield event.plain_result(f"已发送{label}指令")
        except Exception as exc:
            yield event.plain_result(self._error(exc))
        finally:
            if task is None:
                self._operation_active = False

    async def _poll(self, origin, operation, started):
        """Poll to a terminal state and send one completion notification.

        Args:
            origin: Unified message destination captured before returning.
            operation: Start, stop, or restart operation.
            started: Monotonic request start time.
        """
        loop = asyncio.get_running_loop()
        deadline = started + max(0, float(self.config.get("op_wait_seconds", 240)))
        target = 0 if operation == "stop" else 3
        # A restart passes through 1 -> 0 -> 2 -> 3, so "running" only counts
        # after the instance has left the running state, and "stopped" only
        # means failure once it has been seen starting again.
        left_running = operation != "restart"
        seen_starting = False
        state, text = None, None
        try:
            try:
                while loop.time() < deadline:
                    await asyncio.sleep(min(3, max(0, deadline - loop.time())))
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        detail = await asyncio.wait_for(self.client.detail(), remaining)
                    except asyncio.TimeoutError:
                        break
                    state = detail.get("status")
                    left_running = left_running or state != 3
                    if state == target and left_running:
                        label = {"start": "启动", "stop": "停止", "restart": "重启"}[
                            operation
                        ]
                        text = f"服务器已{label}（耗时 {loop.time() - started:.0f} 秒）"
                        break
                    if target == 3 and state == 0 and seen_starting:
                        text = "启动失败，服务器已停止，请用 /mcs log 查看日志"
                        break
                    seen_starting = seen_starting or state in (-1, 2)
                if text is None:
                    text = f"等待超时，当前状态：{STATUS_NAMES.get(state, '未知')}"
            except Exception as exc:
                text = self._error(exc)
            await self.context.send_message(
                origin, MessageChain().message(self._safe_text(text))
            )
        except Exception as exc:
            self._error(exc)
        finally:
            self._operation_active = False

    async def terminate(self):
        """Cancel background operations before closing the lazy HTTP session."""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.clear()
            self._operation_active = False
            await self.client.close()
