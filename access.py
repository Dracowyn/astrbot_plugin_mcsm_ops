"""Access rules: which instances a sender may reach and what the LLM may run.

Everything here is security relevant and kept free of I/O so it can be tested
directly. Callers must route every instance lookup through these functions.
"""

import re
import unicodedata
from dataclasses import dataclass

# Platform name registered by the mcqq adapter for in-game chat.
GAME_PLATFORM = "minecraft"
# Reserved alias meaning "every instance visible here" (whitelist add/remove).
ALL_ALIAS = "all"
_CONTROL_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")
_PLAYER_NAME = re.compile(r"[A-Za-z0-9_]{3,16}")


@dataclass(frozen=True)
class Instance:
    """One validated MCSManager instance entry from the plugin config."""

    alias: str
    daemon_id: str
    instance_uuid: str
    groups: tuple[str, ...]
    tps_command: str
    tps_regex: str
    alert_enabled: bool


def has_control_chars(text: str) -> bool:
    """Report characters that could split or alter one console line.

    Args:
        text: Text that will be written to the server console.

    Returns:
        True when the text contains control, format or line separator chars.
    """
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in text)


def normalize_instances(raw: object) -> tuple[list[Instance], list[str]]:
    """Validate the ``instances`` config list into immutable entries.

    Invalid entries are skipped instead of guessed at, so a typo can never
    widen access. Alias comparison is case-insensitive.

    Args:
        raw: The ``instances`` value from the plugin config.

    Returns:
        The valid instances and one human-readable problem per skipped entry.
    """
    instances: list[Instance] = []
    problems: list[str] = []
    seen: set[str] = set()
    targets: set[tuple[str, str]] = set()
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        label = f"instances[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{label} 不是对象")
            continue
        alias = str(item.get("alias") or "").strip()
        daemon_id = str(item.get("daemon_id") or "").strip()
        instance_uuid = str(item.get("instance_uuid") or "").strip()
        if not alias or not daemon_id or not instance_uuid:
            problems.append(f"{label} 缺少 alias、daemon_id 或 instance_uuid")
            continue
        if any(char.isspace() for char in alias) or has_control_chars(alias):
            problems.append(f"{label} 别名不能包含空白或控制字符")
            continue
        if alias.casefold() == ALL_ALIAS or alias.casefold() in seen:
            problems.append(f"{label} 别名 {alias} 是保留字或与其他实例重复")
            continue
        # Two aliases for one instance would get separate command locks and
        # overlapping capture windows, mixing one session's output into another.
        if (daemon_id, instance_uuid) in targets:
            problems.append(f"{label} 与其他条目指向同一个实例")
            continue
        raw_groups = item.get("groups")
        groups = tuple(
            dict.fromkeys(
                str(group).strip()
                for group in (raw_groups if isinstance(raw_groups, list) else [])
                if str(group).strip().isascii() and str(group).strip().isdecimal()
            )
        )
        seen.add(alias.casefold())
        targets.add((daemon_id, instance_uuid))
        instances.append(
            Instance(
                alias=alias,
                daemon_id=daemon_id,
                instance_uuid=instance_uuid,
                groups=groups,
                tps_command=str(item.get("tps_command") or "").strip(),
                tps_regex=str(item.get("tps_regex") or "").strip(),
                alert_enabled=bool(item.get("alert_enabled", True)),
            )
        )
    return instances, problems


def visible_instances(
    instances: list[Instance],
    platform: str,
    group_id: str,
    is_admin: bool,
    for_llm: bool = False,
) -> list[Instance]:
    """Return the instances a sender may address from this conversation.

    Rules: a QQ group sees only the instances bound to it, for admins too; a
    private chat sees everything for admins and nothing otherwise; in-game chat
    (mcqq) sees every instance bound to at least one group, and only through
    the read-only LLM tools. Unbound instances stay admin-private-only.

    Args:
        instances: Validated instances.
        platform: ``event.get_platform_name()``.
        group_id: ``event.get_group_id()``; empty for private chats.
        is_admin: ``event.is_admin()``.
        for_llm: True for the read-only LLM tools, False for ``/mcs`` commands.

    Returns:
        Visible instances in config order.
    """
    if platform == GAME_PLATFORM:
        return [i for i in instances if i.groups] if for_llm else []
    group = str(group_id or "").strip()
    if group:
        return [instance for instance in instances if group in instance.groups]
    return list(instances) if is_admin else []


def pick_instance(
    instances: list[Instance],
    visible: list[Instance],
    tokens: list[str],
    allow_all: bool = False,
) -> tuple[list[Instance], list[str], str | None]:
    """Resolve the optional leading alias of a command's arguments.

    A leading token naming any configured alias is always consumed, so that an
    alias the sender cannot see is rejected instead of being sent to the
    console of the only visible instance.

    Args:
        instances: All validated instances.
        visible: Result of ``visible_instances`` for this sender.
        tokens: Whitespace-split arguments after the subcommand.
        allow_all: Accept ``all`` as every visible instance.

    Returns:
        Selected instances, remaining tokens, and an error message (selection
        is empty whenever the error is set).
    """
    first = tokens[0].casefold() if tokens else ""
    if allow_all and first == ALL_ALIAS:
        if not visible:
            return [], tokens[1:], "当前会话没有可操作的实例"
        return list(visible), tokens[1:], None
    named = next((i for i in instances if i.alias.casefold() == first), None)
    if named is not None:
        if named not in visible:
            return [], tokens[1:], f"当前会话无权操作实例 {named.alias}"
        return [named], tokens[1:], None
    if len(visible) == 1:
        return list(visible), tokens, None
    if not visible:
        return [], tokens, "当前会话没有可操作的实例"
    aliases = "、".join(instance.alias for instance in visible)
    return [], tokens, f"请指定实例别名：{aliases}"


def match_allowed_command(command: str, allowed: list[str]) -> str | None:
    """Match an LLM-requested console command against the exact whitelist.

    Matching ignores case, a leading slash and repeated whitespace, but never
    accepts extra arguments. The configured spelling is returned so that the
    text sent to the console always comes from the config, not from the LLM.

    Args:
        command: Command text proposed by the LLM.
        allowed: ``llm_allowed_commands`` from the plugin config.

    Returns:
        The configured command to send, or None when it is not whitelisted.
    """
    if has_control_chars(command):
        return None
    wanted = " ".join(command.strip().removeprefix("/").split()).casefold()
    if not wanted:
        return None
    for entry in allowed if isinstance(allowed, list) else []:
        text = str(entry)
        canonical = " ".join(text.strip().removeprefix("/").split())
        if not has_control_chars(text) and canonical.casefold() == wanted:
            return canonical
    return None


def valid_player_name(name: str) -> bool:
    """Check a Java Edition player name before it reaches the console.

    Args:
        name: Player name argument from a whitelist command.

    Returns:
        True for 3-16 ASCII letters, digits or underscores.
    """
    return _PLAYER_NAME.fullmatch(name) is not None
