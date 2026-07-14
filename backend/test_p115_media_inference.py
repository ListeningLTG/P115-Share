from app.services.p115 import (
    extract_tmdb_id_from_name,
    extract_replacement_title_fragment,
    infer_media_hint_from_items,
    infer_media_hint_from_name,
)


def test_extract_tmdb_id_formats():
    assert extract_tmdb_id_from_name("中央情报局 (2026) {tmdb=289560}") == 289560
    assert extract_tmdb_id_from_name("骨干小队 {tmdbid-202879}") == 202879
    assert extract_tmdb_id_from_name("示例 tmdb: 123456") == 123456
    assert extract_tmdb_id_from_name("无 tmdb 信息") is None


def test_infer_media_hint_from_name():
    assert infer_media_hint_from_name("剧名.S01E01.1080p.mkv") == "tv"
    assert infer_media_hint_from_name("剧名.E04.1080p.mkv") == "tv"
    assert infer_media_hint_from_name("剧名.Ep12.1080p.mkv") == "tv"
    assert infer_media_hint_from_name("电影名.2026.2160p.mkv") == "unknown"


def test_infer_media_hint_from_items_by_season_folder():
    items = [
        {"is_dir": True, "name": "Season 01"},
        {"is_dir": False, "name": "readme.txt"},
    ]
    assert infer_media_hint_from_items(items) == "tv"


def test_infer_media_hint_from_items_by_episode_files():
    items = [
        {"is_dir": False, "name": "星球大战：骨干小队 (2024) S01E01.说不定，这就是一场真正的冒险.mkv"},
        {"is_dir": False, "name": "星球大战：骨干小队 (2024) S01E02.远远飞出屏障之外.mkv"},
        {"is_dir": False, "name": "星球大战：骨干小队 (2024) S01E03.很有意思的宇航问题.mkv"},
    ]
    assert infer_media_hint_from_items(items) == "tv"


def test_infer_media_hint_from_items_by_weak_episode_sequence():
    items = [
        {"is_dir": False, "name": "My.Show.01.1080p.mkv"},
        {"is_dir": False, "name": "My.Show.02.1080p.mkv"},
        {"is_dir": False, "name": "My.Show.03.1080p.mkv"},
    ]
    assert infer_media_hint_from_items(items) == "tv"


def test_extract_replacement_title_fragment_full_chinese_head():
    name1 = "星球大战：骨干小队 (2024) S01E01.说不定，这就是一场真正的冒险.mkv"
    name2 = "孤单又灿烂的神：鬼怪十周年特辑.2026 - S01E04 - 第 4 集 - 1080p.Viu.WEB-DL.{tmdb-325828}.mkv"

    assert extract_replacement_title_fragment(name1) == "星球大战：骨干小队"
    assert extract_replacement_title_fragment(name2) == "孤单又灿烂的神：鬼怪十周年特辑"


if __name__ == "__main__":
    test_extract_tmdb_id_formats()
    test_infer_media_hint_from_name()
    test_infer_media_hint_from_items_by_season_folder()
    test_infer_media_hint_from_items_by_episode_files()
    test_infer_media_hint_from_items_by_weak_episode_sequence()
    test_extract_replacement_title_fragment_full_chinese_head()
    print("All P115 media inference tests passed")
