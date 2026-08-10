"""Path resolution, against synthetic Steam trees only."""

from __future__ import annotations

from pathlib import Path

from modules.gamelog.backend.locate import (
    LAYOUTS,
    _library_paths_from_vdf,
    candidates,
    library_roots,
    locate,
    parse_vdf,
    steam_roots,
)

from .conftest import SteamTree

# -- the VDF parser --------------------------------------------------------------


def test_parses_nested_blocks_and_values():
    parsed = parse_vdf(
        '''
        "libraryfolders"
        {
            "0"
            {
                "path"      "/home/deck/.local/share/Steam"
                "apps"      { "238960" "32000000000" }
            }
        }
        '''
    )
    assert parsed["libraryfolders"]["0"]["path"] == "/home/deck/.local/share/Steam"
    assert parsed["libraryfolders"]["0"]["apps"]["238960"] == "32000000000"


def test_comments_and_conditionals_are_skipped():
    parsed = parse_vdf(
        '''
        "root"          // a trailing comment
        {
            // a whole-line comment
            "key"       "value" [$WIN32]
        }
        '''
    )
    assert parsed == {"root": {"key": "value"}}


def test_escapes_are_decoded():
    parsed = parse_vdf(r'"root" { "path" "D:\\Games\\Path of Exile" "quote" "a\"b" }')
    assert parsed["root"]["path"] == r"D:\Games\Path of Exile"
    assert parsed["root"]["quote"] == 'a"b'


def test_malformed_input_does_not_raise():
    """A vdf we cannot read must cost us one library, not the whole watcher."""
    assert parse_vdf('"libraryfolders" { "0" { "path" ') == {"libraryfolders": {"0": {}}}
    assert parse_vdf("}}}}") == {}
    assert parse_vdf("") == {}


def test_modern_dialect_yields_every_library():
    parsed = parse_vdf(
        '''
        "libraryfolders"
        {
            "0" { "path" "/home/deck/.local/share/Steam" }
            "1" { "path" "/run/media/mmcblk0p1" }
            "contentstatsid" "-123"
        }
        '''
    )
    assert _library_paths_from_vdf(parsed) == [
        "/home/deck/.local/share/Steam",
        "/run/media/mmcblk0p1",
    ]


def test_legacy_dialect_yields_every_library():
    parsed = parse_vdf(
        '''
        "LibraryFolders"
        {
            "TimeNextStatsReport"   "1600000000"
            "1"                     "/run/media/mmcblk0p1"
            "2"                     "/mnt/games"
        }
        '''
    )
    assert _library_paths_from_vdf(parsed) == ["/run/media/mmcblk0p1", "/mnt/games"]


# -- library enumeration ----------------------------------------------------------


def test_library_roots_reads_steamapps_libraryfolders(steam: SteamTree, tmp_path: Path):
    sd_card = steam.add_library(tmp_path / "run/media/mmcblk0p1")
    steam.write_libraryfolders()
    assert library_roots(steam.root) == [steam.root, sd_card]


def test_library_roots_falls_back_to_config_libraryfolders(steam: SteamTree, tmp_path: Path):
    sd_card = steam.add_library(tmp_path / "run/media/mmcblk0p1")
    steam.write_libraryfolders(where="config")
    assert library_roots(steam.root) == [steam.root, sd_card]


def test_library_roots_handles_the_legacy_dialect(steam: SteamTree, tmp_path: Path):
    sd_card = steam.add_library(tmp_path / "run/media/mmcblk0p1")
    steam.write_libraryfolders(legacy=True)
    assert library_roots(steam.root) == [steam.root, sd_card]


def test_the_root_is_a_library_even_with_no_vdf(steam: SteamTree):
    assert library_roots(steam.root) == [steam.root]


def test_an_unreadable_vdf_leaves_the_default_library(steam: SteamTree, tmp_path: Path):
    steam.add_library(tmp_path / "sd")
    vdf = steam.write_libraryfolders()
    vdf.write_bytes(b"\xff\xfe not a vdf at all {{{")
    assert library_roots(steam.root) == [steam.root]


def test_a_library_listed_twice_is_returned_once(steam: SteamTree, tmp_path: Path):
    sd_card = steam.add_library(tmp_path / "sd")
    steam.libraries.append(sd_card)
    steam.write_libraryfolders()
    assert library_roots(steam.root) == [steam.root, sd_card]


def test_steam_roots_skips_what_does_not_exist(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    assert steam_roots([real, tmp_path / "absent"]) == [real]


def test_steam_roots_deduplicates_a_symlink_and_its_target(tmp_path: Path):
    """`~/.steam/steam` is normally a symlink to `~/.local/share/Steam`."""
    target = tmp_path / "Steam"
    target.mkdir()
    link = tmp_path / "dot-steam"
    link.symlink_to(target)
    assert steam_roots([link, target]) == [link]


# -- probing ----------------------------------------------------------------------


def test_finds_the_native_linux_path_first(steam: SteamTree):
    log = steam.install()
    found, _ = locate(roots=[steam.root])
    assert found is not None
    assert found.path == log
    assert found.origin == "library"
    assert found.exists


def test_finds_the_log_on_a_second_library(steam: SteamTree, tmp_path: Path):
    """The Deck's SD card, whose mount point has moved across SteamOS releases."""
    sd_card = steam.add_library(tmp_path / "run/media/mmcblk0p1")
    steam.write_libraryfolders()
    log = steam.install(sd_card)
    found, probed = locate(roots=[steam.root])
    assert found is not None and found.path == log
    assert any(str(steam.root) in str(c.path) for c in probed), "the first library was skipped"


def test_falls_back_to_the_compatdata_my_games_layout(steam: SteamTree):
    log = steam.install(layout="mygames")
    found, _ = locate(roots=[steam.root])
    assert found is not None and found.path == log
    assert found.origin == "compatdata"


def test_falls_back_to_the_compatdata_ggg_layout(steam: SteamTree):
    log = steam.install(layout="ggg")
    found, _ = locate(roots=[steam.root])
    assert found is not None and found.path == log
    assert found.origin == "compatdata"


def test_the_native_path_wins_over_a_compatdata_copy(steam: SteamTree):
    native = steam.install(layout="common")
    steam.install(layout="ggg")
    found, _ = locate(roots=[steam.root])
    assert found is not None and found.path == native


def test_latestclient_is_probed_as_a_sibling(steam: SteamTree):
    """Reported by one source, unconfirmed for PoE 1 — so: second, never first."""
    log = steam.install(name="LatestClient.txt")
    found, _ = locate(roots=[steam.root])
    assert found is not None and found.path == log


def test_client_txt_beats_latestclient_in_the_same_directory(steam: SteamTree):
    client = steam.install(name="Client.txt")
    steam.install(name="LatestClient.txt")
    found, _ = locate(roots=[steam.root])
    assert found is not None and found.path == client


def test_every_layout_is_probed_when_nothing_is_installed(steam: SteamTree):
    found, probed = locate(roots=[steam.root])
    assert found is None
    for _, relative in LAYOUTS:
        assert any(str(c.path).endswith(f"{relative}/Client.txt") for c in probed), relative
    assert any(c.path.name == "LatestClient.txt" for c in probed)


def test_an_installed_game_that_has_never_run_is_waited_for(steam: SteamTree):
    """The log does not exist until Path of Exile has been run once."""
    log = steam.install(create_log=False)
    log.parent.rmdir()  # `logs/` itself may not exist yet either
    found, _ = locate(roots=[steam.root])
    assert found is not None
    assert found.path == log
    assert not found.exists


def test_nothing_anywhere_resolves_to_nothing(steam: SteamTree):
    found, probed = locate(roots=[steam.root])
    assert found is None
    assert probed, "we should still be able to say what we tried"


# -- the manual override ----------------------------------------------------------


def test_an_override_is_used_exclusively(steam: SteamTree, tmp_path: Path):
    steam.install()  # a perfectly good auto-discoverable log, which must be ignored
    manual = tmp_path / "elsewhere" / "Client.txt"
    manual.parent.mkdir()
    manual.write_text("")
    found, probed = locate(roots=[steam.root], override=manual)
    assert found is not None and found.path == manual
    assert found.origin == "override"
    assert [c.path for c in probed] == [manual]


def test_an_override_that_does_not_exist_yet_is_still_honoured(tmp_path: Path):
    """"Not there yet" and "I cannot find it" are different states to a user."""
    manual = tmp_path / "not-yet" / "Client.txt"
    found, _ = locate(roots=[], override=manual)
    assert found is not None
    assert found.path == manual
    assert not found.exists


def test_a_tilde_in_the_override_is_expanded(tmp_path: Path):
    found, _ = locate(roots=[], override="~/Client.txt")
    assert found is not None
    assert "~" not in str(found.path)


def test_candidates_are_unique_and_ordered(steam: SteamTree, tmp_path: Path):
    steam.add_library(tmp_path / "sd")
    steam.write_libraryfolders()
    probed = [c.path for c in candidates(roots=[steam.root])]
    assert len(probed) == len(set(probed))
    assert probed[0].name == "Client.txt"
    assert "common" in str(probed[0]), "the verified layout must be probed first"
