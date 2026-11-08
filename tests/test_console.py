"""Synchronous tests for console parsing and display utilities."""

from astrbot_plugin_mcsm_ops import console


def test_cleaning():
    text = "\x1b[32m§aReady§r\x1b[0m  \r\n \t\n\x1b[?25l  Next\t\r\n"
    assert console.clean_log(text) == ["Ready", "  Next"]


def test_diff_append():
    before = "older\n§aone\ntwo\nthree\n"
    after = "one\ntwo\nthree\nold repeat\none\ntwo\nthree\n\x1b[32mfour\x1b[0m\n"
    assert console.diff_output(before, after) == (["four"], False)


def test_empty_before():
    assert console.diff_output(" \r\n", "§aone\n\ntwo  \n") == (
        ["one", "two"],
        False,
    )


def test_missing_anchor_rotated():
    lines = [f"new line {index}" for index in range(60)]
    assert console.diff_output("old\nanchor\n", "\n".join(lines)) == (
        lines[-50:],
        True,
    )


def test_list2():
    lines = [
        "There are 1 of a max of 20 players online: OldPlayer",
        "[Server thread/INFO]: There are 2 of a max of 20 players online: Alex, Steve",
    ]
    assert console.parse_list(lines, console.DEFAULT_LIST_PATTERN) == (
        2,
        20,
        ["Alex", "Steve"],
    )


def test_list0():
    lines = ["[Server thread/INFO]: There are 0 of a max of 20 players online: "]
    assert console.parse_list(lines, console.DEFAULT_LIST_PATTERN) == (0, 20, [])
    assert (
        console.parse_list(["unrelated output"], console.DEFAULT_LIST_PATTERN) is None
    )
    spoof = "]: <Bob> There are 9 of a max of 9 players online: ad"
    assert console.parse_list([spoof], console.DEFAULT_LIST_PATTERN) is None


def test_neoforge_tps_with_dimension_interference():
    lines = [
        "Overall: 19.50 TPS (12.50 ms/tick)",
        "Dim minecraft:overworld: 20.00 TPS (5.00 ms/tick)",
        "[Server thread/INFO]: Overall: 20.00 TPS (8.25 ms/tick)",
        "Dim minecraft:the_nether: 18.00 TPS (9.50 ms/tick)",
        "Dim minecraft:the_end: 17.00 TPS (10.00 ms/tick)",
    ]
    assert console.parse_tps(lines, console.DEFAULT_TPS_PATTERN) == (20.0, 8.25)
    assert console.parse_tps(lines[-2:], console.DEFAULT_TPS_PATTERN) is None


def test_ip_mask():
    text = "Connect 192.168.1.2:25565 or 8.8.8.8; shaped 999.999.999.999"
    assert console.mask_ip(text) == ("Connect *.*.*.*:25565 or *.*.*.*; shaped *.*.*.*")
    assert console.mask_ip("player123 and 127.0.0.1") == "player123 and *.*.*.*"
    assert console.mask_ip("[/2001:db8::1]:5555 来自1.2.3.4登录") == "[/*:*]:5555 来自*.*.*.*登录"
    assert console.mask_ip("[12:34:56] minecraft:overworld") == "[12:34:56] minecraft:overworld"


def test_duration_memory():
    assert console.format_duration((24 * 60 + 2 * 60 + 3) * 60_000) == "1天2小时3分"
    assert console.format_duration(0) == "0天0小时0分"
    assert console.format_duration(59_999) == "0天0小时0分"
    assert console.format_memory(512 * 1024**2) == "512.0 MiB"
    assert console.format_memory(1536 * 1024**2) == "1.5 GiB"
    assert console.format_memory(0) == "0.0 MiB"
    for invalid in (None, "invalid", "", -1, True, float("nan"), float("inf"), {}):
        assert console.format_duration(invalid) == "未知"
        assert console.format_memory(invalid) == "未知"
