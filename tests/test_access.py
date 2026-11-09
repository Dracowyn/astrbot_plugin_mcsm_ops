"""Access rule tests: instance scope, alias selection and command whitelists."""

from astrbot_plugin_mcsm_ops.access import (
    match_allowed_command,
    normalize_instances,
    pick_instance,
    valid_player_name,
    visible_instances,
)

RAW = [
    {"alias": "生存", "daemon_id": "d", "instance_uuid": "a", "groups": ["1", "2"]},
    {"alias": "创造", "daemon_id": "d", "instance_uuid": "b", "groups": ["2"]},
    {"alias": "ALL", "daemon_id": "d", "instance_uuid": "c"},
    {"alias": "生存", "daemon_id": "d", "instance_uuid": "d"},
    {"alias": "坏 名", "daemon_id": "d", "instance_uuid": "e"},
    {"alias": "空", "daemon_id": "", "instance_uuid": "f"},
]


def test_normalize_skips_reserved_duplicate_and_incomplete_entries():
    instances, problems = normalize_instances(RAW)
    assert [i.alias for i in instances] == ["生存", "创造"]
    assert len(problems) == 4
    assert instances[0].alert_enabled is True


def test_visibility_by_group_private_and_game():
    instances, _ = normalize_instances(RAW)
    survival, creative = instances
    assert visible_instances(instances, "aiocqhttp", "1", True) == [survival]
    assert visible_instances(instances, "aiocqhttp", "2", False) == instances
    assert visible_instances(instances, "aiocqhttp", "9", True) == []
    assert visible_instances(instances, "aiocqhttp", "", True) == instances
    assert visible_instances(instances, "aiocqhttp", "", False) == []
    assert visible_instances(instances, "minecraft", "1", True) == []
    assert visible_instances(instances, "minecraft", "", False, for_llm=True) == (
        instances
    )


def test_pick_rejects_invisible_alias_instead_of_using_default():
    instances, _ = normalize_instances(RAW)
    visible = visible_instances(instances, "aiocqhttp", "1", True)
    picked, rest, error = pick_instance(instances, visible, ["创造", "say", "hi"])
    assert picked == [] and error is not None and "创造" in error
    picked, rest, error = pick_instance(instances, visible, ["say", "hi"])
    assert picked == visible and rest == ["say", "hi"] and error is None


def test_pick_requires_alias_when_several_and_supports_all():
    instances, _ = normalize_instances(RAW)
    visible = visible_instances(instances, "aiocqhttp", "2", False)
    picked, _, error = pick_instance(instances, visible, ["Steve"])
    assert picked == [] and "生存" in error and "创造" in error
    picked, rest, error = pick_instance(instances, visible, ["创造", "Steve"])
    assert picked == [visible[1]] and rest == ["Steve"] and error is None
    picked, rest, _ = pick_instance(instances, visible, ["all", "Steve"], True)
    assert picked == visible and rest == ["Steve"]
    picked, _, error = pick_instance(instances, visible, ["all", "Steve"])
    assert picked == [] and error is not None


def test_llm_command_requires_exact_whitelist_entry():
    allowed = ["list", "spark tps", "spark health"]
    assert match_allowed_command("/Spark   TPS ", allowed) == "spark tps"
    assert match_allowed_command("spark tps extra", allowed) is None
    assert match_allowed_command("spark", allowed) is None
    assert match_allowed_command("spark profiler start", allowed) is None
    assert match_allowed_command("list\nop Steve", allowed) is None
    assert match_allowed_command("list op Steve", allowed) is None
    assert match_allowed_command("", allowed) is None


def test_player_name_validation():
    assert valid_player_name("Steve_01")
    assert not valid_player_name("ab")
    assert not valid_player_name("a" * 17)
    assert not valid_player_name("Steve; op me")
    assert not valid_player_name("Stéve")


def test_game_skips_unbound_instances_and_duplicate_targets_are_rejected():
    raw = RAW[:2] + [
        {"alias": "私服", "daemon_id": "d", "instance_uuid": "p"},
        {"alias": "别名", "daemon_id": "d", "instance_uuid": "a", "groups": ["3"]},
    ]
    instances, problems = normalize_instances(raw)
    assert [i.alias for i in instances] == ["生存", "创造", "私服"]
    assert len(problems) == 1
    visible = visible_instances(instances, "minecraft", "", False, for_llm=True)
    assert [i.alias for i in visible] == ["生存", "创造"]
