"""MCSManager multi-instance operations and read-only AstrBot tools."""

import asyncio
import re
import traceback
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .access import (
    ALL_ALIAS,
    GAME_PLATFORM,
    has_control_chars,
    match_allowed_command,
    normalize_instances,
    pick_instance,
    valid_player_name,
    visible_instances,
)
from .console import (
    CHATTER_PATTERN,
    DEFAULT_LIST_PATTERN,
    DEFAULT_TPS_PATTERN,
    SERVER_DONE_PATTERN,
    SERVER_STOP_PATTERN,
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
DEFAULT_LLM_COMMANDS = ["list", "neoforge tps", "spark tps", "spark health"]


class Plugin(Star):
    """Manage independently scoped MCSManager instances."""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._instances, problems = normalize_instances(config.get("instances", []))
        self._redactor = McsmClient(
            config.get("panel_url", ""), config.get("api_key", ""), "", ""
        )
        for problem in problems:
            logger.warning("Invalid MCSManager instance: %s", self._safe_text(problem))
        self._clients = {
            inst.alias: McsmClient(
                config.get("panel_url", ""),
                config.get("api_key", ""),
                inst.daemon_id,
                inst.instance_uuid,
                timeout=config.get("request_timeout_seconds", 10),
            )
            for inst in self._instances
        }
        self._command_locks = {inst.alias: asyncio.Lock() for inst in self._instances}
        self._operation_active = {inst.alias: False for inst in self._instances}
        self._alert_states = {
            inst.alias: {
                "baseline_set": False,
                "last_status": None,
                "last_datetime": None,
                "failures": 0,
                "crash_alerted": False,
                "unreachable_alerted": False,
            }
            for inst in self._instances
        }
        self._llm_cache = {inst.alias: {} for inst in self._instances}
        self._tasks: set[asyncio.Task] = set()
        self._platform_error_logged = False

    def _gate(self, event, for_llm=False):
        """Return None on success, empty silent denial, or a configuration notice.

        Args:
            event: Incoming platform event.
            for_llm: Allow read-only queries from the game platform.

        Returns:
            Optional denial text.
        """
        platform = event.get_platform_name()
        if platform not in self.config.get("allowed_platforms", ["aiocqhttp"]) and not (
            for_llm and platform == GAME_PLATFORM
        ):
            return ""
        missing = [
            key
            for key in ("panel_url", "api_key")
            if not str(self.config.get(key, "") or "").strip()
        ]
        if not self._instances:
            missing.append("有效实例 instances")
        return "插件未配置：缺少 " + "、".join(missing) if missing else None

    def _safe_text(self, text):
        """Remove credentials from outgoing text.

        Args:
            text: Potentially unsafe text.

        Returns:
            Credential-redacted text.
        """
        return self._redactor.redact(text)

    def _render_lines(self, lines):
        """Redact complete lines before applying display limits.

        Args:
            lines: Cleaned console lines.

        Returns:
            Sanitized text bounded to MAX_OUTPUT_CHARS characters.
        """
        mask = self.config.get("log_mask_ip", True)
        safe = [self._safe_text(line) for line in lines]
        safe = [mask_ip(line) if mask else line for line in safe]
        return "\n".join(line[:MAX_LINE_CHARS] for line in safe)[-MAX_OUTPUT_CHARS:]

    def _error(self, exc):
        """Log a sanitized failure and return a safe Chinese error.

        Args:
            exc: Caught request or internal exception.

        Returns:
            Safe error text.
        """
        if isinstance(exc, McsmError):
            message = self._safe_text(exc)
            if self.config.get("log_mask_ip", True):
                message = mask_ip(message)
            message = message[:200]
            logger.warning("MCSManager request failed: %s", message)
            return f"MCSManager 请求失败：{message}"
        trace = self._safe_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        )
        if self.config.get("log_mask_ip", True):
            trace = mask_ip(trace)
        logger.exception("MCSManager internal error:\n%s", trace, exc_info=False)
        return "内部错误，请查看日志"

    def _arguments(self, event, subcommand):
        """Read the original suffix after a command token.

        Args:
            event: Event with the original message string.
            subcommand: Registered child command name.

        Returns:
            Original suffix with outer whitespace removed.
        """
        match = re.search(
            r"(?:^|\s)mcs\s+" + subcommand + r"(?=\s|$)", event.message_str.lstrip("/")
        )
        return event.message_str.lstrip("/")[match.end() :].strip() if match else ""

    def _command_target(
        self, event, subcommand, summary=False, allow_all=False, raw=None
    ):
        """Apply shared gates and consume an alias without folding argument spaces.

        Args:
            event: Incoming command event.
            subcommand: Registered command name.
            summary: Allow all visible instances when no arguments were supplied.
            allow_all: Permit the reserved whitelist alias.
            raw: Optional suffix already stripped of the whitelist action.

        Returns:
            Selected instances, original remaining arguments, and optional denial.
        """
        gate = self._gate(event)
        if gate is not None:
            return [], "", gate
        visible = visible_instances(
            self._instances,
            event.get_platform_name(),
            event.get_group_id(),
            event.is_admin(),
        )
        if not visible:
            return [], "", "本群未绑定实例" if event.is_admin() else ""
        if has_control_chars(event.message_str):
            return [], "", "拒绝命令：不能包含换行或控制字符"
        raw = self._arguments(event, subcommand) if raw is None else raw
        tokens = raw.split()
        selected, rest, error = pick_instance(
            self._instances, visible, tokens, allow_all=allow_all
        )
        if summary and not tokens and len(visible) > 1:
            return visible, "", None
        if len(rest) < len(tokens):
            suffix = raw.split(maxsplit=1)[1:]
            raw = suffix[0] if suffix else ""
        return selected, raw, error

    async def _read_command(self, alias, command, parser=None, pattern=""):
        """Serialize console capture for one instance and retry a delayed match.

        Args:
            alias: Validated instance alias.
            command: Single-line console command.
            parser: Optional result parser.
            pattern: Parser expression.

        Returns:
            New cleaned lines and whether the log anchor was lost.

        Raises:
            ValueError: The configured command contains control characters.
        """
        if has_control_chars(command):
            raise ValueError("Console commands must not contain control characters")
        async with self._command_locks[alias]:
            client = self._clients[alias]
            size = self.config.get("log_fetch_kb", 64)
            delay = max(0.6, float(self.config.get("command_wait_seconds", 1.5)))
            before = await client.outputlog(size)
            await client.send_command(command)
            await asyncio.sleep(delay)
            lines, rotated = diff_output(before, await client.outputlog(size))
            if parser is not None and parser(lines, pattern) is None:
                await asyncio.sleep(delay)
                lines, rotated = diff_output(before, await client.outputlog(size))
            return lines, rotated

    @filter.command_group("mcs")
    def mcs(self):
        """Query and operate visible Minecraft servers."""

    @mcs.command("status")
    async def status(self, event: AstrMessageEvent):
        """Display selected instance details or command-free summaries."""
        selected, _, error = self._command_target(event, "status", summary=True)
        if error is not None:
            if error:
                yield event.plain_result(self._safe_text(error))
            return
        summaries = []
        for inst in selected:
            try:
                detail = await self._clients[inst.alias].detail()
                info, process, config = (
                    detail.get(key) or {} for key in ("info", "processInfo", "config")
                )
                state = STATUS_NAMES.get(detail.get("status"), "未知")
                online = f"{info.get('currentPlayers', '未知')}/{info.get('maxPlayers', '未知')}"
                if len(selected) > 1:
                    summaries.append(f"{inst.alias}：{state} 在线 {online}")
                    continue
                cpu = process.get("cpu")
                cpu_text = f"{cpu:.1f}%" if isinstance(cpu, (int, float)) else "未知"
                lines = [
                    f"实例：{inst.alias}",
                    f"名称：{config.get('nickname') or '未知'}",
                    f"状态：{state}",
                    f"在线：{online}",
                    f"版本：{info.get('version') or '未知'}",
                    f"CPU：{cpu_text}",
                    f"内存：{format_memory(process.get('memory'))}",
                    f"运行时长：{format_duration(process.get('elapsed'))}",
                ]
                if detail.get("status") == 3 and inst.tps_command:
                    if self._command_locks[inst.alias].locked():
                        lines.append("TPS：繁忙，请稍后再试")
                    else:
                        tps = None
                        try:
                            pattern = inst.tps_regex or DEFAULT_TPS_PATTERN
                            output, _ = await self._read_command(
                                inst.alias, inst.tps_command, parse_tps, pattern
                            )
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
                message = self._error(exc)
                if len(selected) > 1:
                    summaries.append(f"{inst.alias}：获取失败")
                else:
                    yield event.plain_result(self._safe_text(message))
        if summaries:
            yield event.plain_result(self._safe_text("\n".join(summaries)))

    @mcs.command("list")
    async def list_players(self, event: AstrMessageEvent):
        """Read players for one instance or summarize visible player counts."""
        selected, _, error = self._command_target(event, "list", summary=True)
        if error is not None:
            if error:
                yield event.plain_result(self._safe_text(error))
            return
        summaries = []
        for inst in selected:
            try:
                detail = await self._clients[inst.alias].detail()
                if len(selected) > 1:
                    info = detail.get("info") or {}
                    summaries.append(
                        f"{inst.alias}：在线 {info.get('currentPlayers', '未知')}/{info.get('maxPlayers', '未知')}"
                    )
                    continue
                if detail.get("status") != 3:
                    yield event.plain_result(self._safe_text(f"{inst.alias} 未运行"))
                    return
                if self._command_locks[inst.alias].locked():
                    yield event.plain_result(
                        self._safe_text(f"{inst.alias} 服务器正忙，请稍后再试")
                    )
                    return
                pattern = self.config.get("list_regex", DEFAULT_LIST_PATTERN)
                lines, _ = await self._read_command(
                    inst.alias,
                    self.config.get("list_command", "list"),
                    parse_list,
                    pattern,
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
                yield event.plain_result(self._safe_text(self._render_lines([text])))
            except Exception as exc:
                message = self._error(exc)
                if len(selected) > 1:
                    summaries.append(f"{inst.alias}：获取失败")
                else:
                    yield event.plain_result(self._safe_text(message))
        if summaries:
            yield event.plain_result(self._safe_text("\n".join(summaries)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("log")
    async def log(self, event: AstrMessageEvent):
        """Send filtered logs with OneBot merged forwarding."""
        selected, arguments, error = self._command_target(event, "log")
        if error is not None:
            if error:
                yield event.plain_result(self._safe_text(error))
            return
        try:
            tokens = arguments.split(maxsplit=1)
            count = max(1, int(self.config.get("log_default_lines", 20)))
            if tokens and tokens[0].isdecimal() and int(tokens[0]) > 0:
                count = int(tokens[0])
                arguments = tokens[1] if len(tokens) > 1 else ""
            count = min(count, max(1, int(self.config.get("log_max_lines", 100))))
            lines = clean_log(
                await self._clients[selected[0].alias].outputlog(
                    self.config.get("log_fetch_kb", 64)
                )
            )
            lines = [line for line in lines if arguments.casefold() in line.casefold()][
                -count:
            ]
            if not lines:
                yield event.plain_result(self._safe_text("没有匹配的日志"))
                return
            text = self._safe_text(self._render_lines(lines))
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
            yield event.plain_result(self._safe_text(self._error(exc)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("cmd")
    async def cmd(self, event: AstrMessageEvent, command: GreedyStr):
        """Send an administrator command preserving its original whitespace.

        Args:
            event: Administrator event.
            command: Greedy text supplied by AstrBot; raw event text takes precedence.
        """
        selected, raw, error = self._command_target(event, "cmd")
        if error is not None:
            if error:
                yield event.plain_result(self._safe_text(error))
            return
        try:
            if has_control_chars(str(command)) or has_control_chars(raw):
                yield event.plain_result(
                    self._safe_text("拒绝命令：不能包含换行或控制字符")
                )
                return
            raw = raw.removeprefix("/").strip()
            if not raw:
                yield event.plain_result(
                    self._safe_text("用法：/mcs cmd [别名] <命令>")
                )
                return
            lines, rotated = await self._read_command(selected[0].alias, raw)
            text = self._render_lines(lines[-30:]) if lines else "命令已发送，无输出"
            if rotated:
                text += "\n日志已轮转，结果可能不完整"
            yield event.plain_result(self._safe_text(text))
        except Exception as exc:
            yield event.plain_result(self._safe_text(self._error(exc)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("wl")
    async def wl(self, event: AstrMessageEvent):
        """Manage whitelists on visible instances with validated player names."""
        usage = (
            f"用法：/mcs wl add|remove [别名|{ALL_ALIAS}] <玩家名>，/mcs wl list [别名]"
        )
        raw = self._arguments(event, "wl").split(maxsplit=1)
        action = raw[0] if raw else ""
        selected, arguments, error = self._command_target(
            event,
            "wl",
            summary=action not in ("add", "remove", "list"),
            allow_all=action in ("add", "remove"),
            raw=(raw[1] if len(raw) > 1 else "")
            if action in ("add", "remove", "list")
            else "",
        )
        if error is not None:
            if error:
                yield event.plain_result(self._safe_text(error))
            return
        if action not in ("add", "remove", "list") or (action == "list" and arguments):
            yield event.plain_result(self._safe_text(usage))
            return
        if action != "list" and (
            len(arguments.split()) != 1 or not valid_player_name(arguments)
        ):
            yield event.plain_result(
                self._safe_text("玩家名不合法：只能是 3-16 位字母、数字或下划线")
            )
            return
        command = f"whitelist {action}" + (f" {arguments}" if action != "list" else "")
        replies = []
        for inst in selected:
            try:
                if (await self._clients[inst.alias].detail()).get("status") != 3:
                    text = "未运行"
                else:
                    lines, _ = await self._read_command(inst.alias, command)
                    text = (
                        self._render_lines(lines[-10:])
                        if lines
                        else "命令已发送，无输出"
                    )
            except Exception as exc:
                text = self._error(exc)
            replies.append(f"【{inst.alias}】{text}")
        yield event.plain_result(self._safe_text("\n\n".join(replies)))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("start")
    async def start(self, event: AstrMessageEvent):
        """Start one instance and monitor completion in the background."""
        async for result in self._operate(event, "start"):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("stop")
    async def stop(self, event: AstrMessageEvent):
        """Stop one instance after explicit confirmation."""
        async for result in self._operate(event, "stop"):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @mcs.command("restart")
    async def restart(self, event: AstrMessageEvent):
        """Restart one instance after explicit confirmation."""
        async for result in self._operate(event, "restart"):
            yield result

    async def _operate(self, event, operation):
        """Reserve an instance operation and schedule its completion receipt.

        Args:
            event: Administrator event and reply destination.
            operation: Start, stop, or restart.

        Yields:
            Immediate acknowledgement, denial, or error.
        """
        selected, arguments, error = self._command_target(event, operation)
        if error is not None:
            if error:
                yield event.plain_result(self._safe_text(error))
            return
        alias = selected[0].alias
        if operation != "start" and arguments != "确认":
            yield event.plain_result(
                self._safe_text(
                    f"此操作会让在线玩家掉线，确认请发送 /mcs {operation} {alias} 确认"
                )
            )
            return
        if self._operation_active[alias]:
            yield event.plain_result(self._safe_text(f"{alias} 已有启停操作在进行中"))
            return
        self._operation_active[alias] = True
        self._alert_states[alias]["baseline_set"] = False
        task = None
        try:
            client = self._clients[alias]
            if operation == "start" and (await client.detail()).get("status") == 3:
                yield event.plain_result(self._safe_text(f"{alias} 服务器已在运行"))
                return
            started = asyncio.get_running_loop().time()
            await getattr(client, operation)()
            task = asyncio.create_task(
                self._poll(alias, event.unified_msg_origin, operation, started)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            label = {"start": "启动", "stop": "停止", "restart": "重启"}[operation]
            yield event.plain_result(self._safe_text(f"{alias} 已发送{label}指令"))
        except Exception as exc:
            yield event.plain_result(self._safe_text(self._error(exc)))
        finally:
            if task is None:
                self._operation_active[alias] = False

    async def _poll(self, alias, origin, operation, started):
        """Poll an operation to completion without confusing restart transitions.

        Args:
            alias: Validated instance alias.
            origin: Captured unified message destination.
            operation: Start, stop, or restart.
            started: Monotonic request start time.
        """
        loop = asyncio.get_running_loop()
        deadline = started + max(0, float(self.config.get("op_wait_seconds", 240)))
        target = 0 if operation == "stop" else 3
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
                        detail = await asyncio.wait_for(
                            self._clients[alias].detail(), remaining
                        )
                    except asyncio.TimeoutError:
                        break
                    state = detail.get("status")
                    left_running = left_running or state != 3
                    if state == target and left_running:
                        label = {"start": "启动", "stop": "停止", "restart": "重启"}[
                            operation
                        ]
                        text = (
                            f"{alias} 已{label}（耗时 {loop.time() - started:.0f} 秒）"
                        )
                        break
                    if target == 3 and state == 0 and seen_starting:
                        text = f"{alias} 启动失败，服务器已停止，请用 /mcs log {alias} 查看日志"
                        break
                    seen_starting = seen_starting or state in (-1, 2)
                if text is None:
                    text = (
                        f"{alias} 等待超时，当前状态：{STATUS_NAMES.get(state, '未知')}"
                    )
            except Exception as exc:
                text = self._error(exc)
            await self.context.send_message(
                origin, MessageChain().message(self._safe_text(text))
            )
        except Exception as exc:
            self._error(exc)
        finally:
            self._operation_active[alias] = False
            self._alert_states[alias]["baseline_set"] = False

    async def initialize(self):
        """Start the cancellable alert polling task after plugin loading."""
        task = asyncio.create_task(self._alert_loop())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _alert_loop(self):
        """Poll enabled group-bound instances, isolating individual failures."""
        interval = max(10, int(self.config.get("alert_interval_seconds", 30)))
        while True:
            for inst in self._instances:
                if inst.alert_enabled and inst.groups:
                    try:
                        await self._check_alert(inst)
                    except Exception as exc:
                        self._error(exc)
            await asyncio.sleep(interval)

    async def _send_alert(self, inst, text):
        """Resolve the QQ adapter at send time and notify each configured group.

        Args:
            inst: Validated instance containing destinations.
            text: Summary without console logs.
        """
        platform_id = str(self.config.get("platform_id", "") or "").strip()
        if not platform_id:
            adapters = [
                p
                for p in self.context.platform_manager.platform_insts
                if p.meta().name == "aiocqhttp"
            ]
            if len(adapters) != 1:
                if not self._platform_error_logged:
                    logger.error(
                        "Set platform_id: expected exactly one aiocqhttp adapter, found %s",
                        len(adapters),
                    )
                    self._platform_error_logged = True
                return
            platform_id = adapters[0].meta().id
        for group in inst.groups:
            try:
                sent = await self.context.send_message(
                    f"{platform_id}:GroupMessage:{group}",
                    MessageChain().message(self._safe_text(text)),
                )
                if sent is False and not self._platform_error_logged:
                    logger.error("Alert not sent: unknown platform_id %s", platform_id)
                    self._platform_error_logged = True
            except Exception as exc:
                self._error(exc)

    async def _check_alert(self, inst):
        """Advance one instance's alert state from a single observation.

        Args:
            inst: Validated instance to poll.
        """
        alias = inst.alias
        state = self._alert_states[alias]
        active = self._operation_active[alias]
        try:
            detail = await self._clients[alias].detail()
        except Exception as exc:
            self._error(exc)
            if active:
                state["baseline_set"] = False
                return
            state["failures"] += 1
            if state["failures"] >= 3 and not state["unreachable_alerted"]:
                await self._send_alert(
                    inst, f"[MC] 无法获取实例 {alias} 的状态（面板或节点可能离线）"
                )
                state["unreachable_alerted"] = True
            return
        status = detail.get("status")
        last_datetime = (detail.get("config") or {}).get("lastDatetime")
        if active or self._operation_active[alias]:
            state.update(
                baseline_set=False, last_status=status, last_datetime=last_datetime
            )
            return
        state["failures"] = 0
        if state["unreachable_alerted"]:
            await self._send_alert(inst, f"[MC] 已恢复与实例 {alias} 的连接")
            state["unreachable_alerted"] = False
        if not state["baseline_set"]:
            state.update(
                baseline_set=True, last_status=status, last_datetime=last_datetime
            )
            return
        restarted = False
        now = datetime.now().strftime("%H:%M:%S")
        if state["last_status"] == 3 and status == 0:
            lines = clean_log(
                await self._clients[alias].outputlog(
                    self.config.get("log_fetch_kb", 64)
                )
            )
            # Only lines after the last "Done (...)" belong to the run that ended;
            # anchored patterns keep players from faking a graceful stop in chat.
            starts = [
                i for i, line in enumerate(lines) if SERVER_DONE_PATTERN.match(line)
            ]
            lines = lines[starts[-1] + 1 :] if starts else lines
            if not any(SERVER_STOP_PATTERN.match(line) for line in lines):
                await self._send_alert(
                    inst,
                    f"[MC告警] 实例 {alias} 意外停止（{now}）。管理员可用 /mcs log {alias} 查看日志",
                )
                state["crash_alerted"] = True
        # A previously observed stop distinguishes an ordinary subsequent startup
        # from a restart that occurred entirely between two polling samples.
        if (
            state["last_datetime"]
            and last_datetime != state["last_datetime"]
            and status in (2, 3)
            and state["last_status"] not in (0, 1)
        ):
            await self._send_alert(
                inst,
                f"[MC告警] 实例 {alias} 在 {now} 重新启动过（不是通过 /mcs），可能是崩溃后被面板自动重启。管理员可用 /mcs log {alias} 查看日志",
            )
            state["crash_alerted"] = True
            restarted = True
        if state["crash_alerted"] and status == 3 and not restarted:
            await self._send_alert(inst, f"[MC] 实例 {alias} 已恢复运行")
            state["crash_alerted"] = False
        state.update(last_status=status, last_datetime=last_datetime)

    async def _llm_command(self, inst, command, parser=None, pattern="", trusted=False):
        """Execute a whitelisted read command using a nonqueuing lock and cache.

        Args:
            inst: Authorized instance.
            command: Command proposed by the LLM, or a configured command.
            parser: Optional delayed-result parser.
            pattern: Parser expression.
            trusted: True for list/TPS commands taken from the plugin config,
                which skip the LLM whitelist; LLM text is never trusted.

        Returns:
            Sanitized text bounded to 2000 characters, and whether it is console
            output (False for denial, busy and not-running notices).
        """
        allowed = command
        if not trusted:
            allowed_commands = self.config.get(
                "llm_allowed_commands", DEFAULT_LLM_COMMANDS
            )
            allowed = match_allowed_command(command, allowed_commands)
            if allowed is None:
                return self._safe_text(
                    "该命令不在允许列表中，可用命令："
                    + "、".join(map(str, allowed_commands))
                ), False
        alias = inst.alias
        now = asyncio.get_running_loop().time()
        cached = self._llm_cache[alias].get(allowed)
        if cached is not None and now - cached[0] < 30:
            return cached[1], True
        if self._command_locks[alias].locked():
            return self._safe_text(f"{alias} 服务器正忙，请稍后再试"), False
        if (await self._clients[alias].detail()).get("status") != 3:
            return self._safe_text(f"{alias} 未运行"), False
        # The detail request yields; another query may finish or reserve the lock.
        cached = self._llm_cache[alias].get(allowed)
        if cached is not None and asyncio.get_running_loop().time() - cached[0] < 30:
            return cached[1], True
        if self._command_locks[alias].locked():
            return self._safe_text(f"{alias} 服务器正忙，请稍后再试"), False
        lines, _ = await self._read_command(alias, allowed, parser, pattern)
        # The capture window also holds other players' chat and joins (IPs,
        # coordinates, prompt injection); this text reaches other groups and
        # games, so drop chatter and mask IPs regardless of log_mask_ip.
        kept = [
            mask_ip(self._safe_text(line))[:MAX_LINE_CHARS]
            for line in lines
            if not CHATTER_PATTERN.search(line)
        ]
        text = "\n".join(kept)[-2000:] or "命令已发送，无输出"
        self._llm_cache[alias][allowed] = (asyncio.get_running_loop().time(), text)
        return text, True

    async def _llm_query(self, event, instance, mode, command=""):
        """Share authorization and result formatting across the read-only tools.

        Args:
            event: Tool invocation event.
            instance: Optional instance alias.
            mode: Status, players, or command query.
            command: Proposed command for command mode.

        Returns:
            Safe Chinese tool result, never an emitted chat message.
        """
        instance = str(instance or "").strip()
        command = str(command or "")
        gate = self._gate(event, for_llm=True)
        if gate is not None:
            return self._safe_text(gate or "当前平台不支持查询服务器")
        visible = visible_instances(
            self._instances,
            event.get_platform_name(),
            event.get_group_id(),
            event.is_admin(),
            for_llm=True,
        )
        if not visible:
            return self._safe_text("当前会话没有可查询的服务器")
        if instance:
            selected, rest, error = pick_instance(self._instances, visible, [instance])
            if error:
                return self._safe_text(error)
            if rest:
                return self._safe_text("实例不存在：" + instance)
        elif mode == "status":
            selected = visible
        elif len(visible) > 1:
            return self._safe_text(
                "请指定实例：" + "、".join(inst.alias for inst in visible)
            )
        else:
            selected, _, error = pick_instance(self._instances, visible, [])
            if error:
                return self._safe_text(error)
        replies = []
        for inst in selected:
            try:
                if mode == "command":
                    text, _ = await self._llm_command(inst, command)
                elif mode == "players":
                    pattern = self.config.get("list_regex", DEFAULT_LIST_PATTERN)
                    text, from_console = await self._llm_command(
                        inst,
                        self.config.get("list_command", "list"),
                        parse_list,
                        pattern,
                        trusted=True,
                    )
                    parsed = parse_list(clean_log(text), pattern)
                    if from_console and parsed is None:
                        text = "未能解析在线列表"
                    elif parsed is not None:
                        count, maximum, names = parsed
                        text = (
                            f"在线 {count}/{maximum}：{', '.join(names)}"
                            if count
                            else f"当前没有玩家在线（0/{maximum}）"
                        )
                else:
                    detail = await self._clients[inst.alias].detail()
                    if detail.get("status") != 3:
                        replies.append(f"{inst.alias} 未运行")
                        continue
                    info = detail.get("info") or {}
                    text = f"状态：{STATUS_NAMES.get(detail.get('status'), '未知')}\n在线：{info.get('currentPlayers', '未知')}/{info.get('maxPlayers', '未知')}"
                    if inst.tps_command:
                        pattern = inst.tps_regex or DEFAULT_TPS_PATTERN
                        output, from_console = await self._llm_command(
                            inst, inst.tps_command, parse_tps, pattern, trusted=True
                        )
                        tps = parse_tps(clean_log(output), pattern)
                        if tps:
                            text += f"\nTPS：{tps[0]:.3f} / MSPT：{tps[1]:.3f}"
                        else:
                            text += "\nTPS：" + ("获取失败" if from_console else output)
                replies.append(f"【{inst.alias}】{text}")
            except Exception as exc:
                replies.append(f"【{inst.alias}】{self._error(exc)}")
        return self._safe_text("\n\n".join(replies))

    @filter.llm_tool("mc_server_status")
    async def tool_status(self, event: AstrMessageEvent, instance: str = ""):
        """查询 Minecraft 服务器的运行状态、在线人数和 TPS。用户问服务器开着没、卡不卡、几个人在线时调用。

        Args:
            instance(string): 实例别名；留空则查询当前会话可见的全部实例。

        Returns:
            服务器状态摘要。
        """
        return await self._llm_query(event, instance, "status")

    @filter.llm_tool("mc_online_players")
    async def tool_players(self, event: AstrMessageEvent, instance: str = ""):
        """查询 Minecraft 服务器当前在线玩家名单。

        Args:
            instance(string): 实例别名；当前会话可见多个实例时必须指定。

        Returns:
            在线玩家名单或查询失败原因。
        """
        return await self._llm_query(event, instance, "players")

    @filter.llm_tool("mc_run_readonly_command")
    async def tool_readonly(
        self, event: AstrMessageEvent, command: str, instance: str = ""
    ):
        """查询 Minecraft 控制台信息，只能执行管理员配置的只读命令，如 list、neoforge tps、spark tps、spark health，不能附加参数或执行启停。

        Args:
            command(string): 管理员允许列表中的完整只读命令，必须完全匹配。
            instance(string): 实例别名；当前会话可见多个实例时必须指定。

        Returns:
            脱敏的控制台结果或拒绝原因。
        """
        return await self._llm_query(event, instance, "command", command)

    async def terminate(self):
        """Cancel background tasks and close every lazily opened HTTP session."""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._tasks.clear()
            for alias, client in self._clients.items():
                self._operation_active[alias] = False
                await client.close()
            await self._redactor.close()
