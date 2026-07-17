import asyncio
from types import SimpleNamespace

from app.services.tmdb_service import TMDBService


class FakeTMDBService(TMDBService):
    def __init__(self, responses):
        super().__init__()
        self.responses = responses
        self.calls = []

    async def _ensure_api_key(self):
        return "fake-key"

    async def _request_with_retry(self, url, params=None, method="GET", timeout=15, max_retries=3):
        self.calls.append(url)
        return self.responses.get(url, (None, 404))

    async def _get_alias_cache(self, tmdb_id: int):
        return None

    async def _save_alias_cache(self, *args, **kwargs):
        return None


async def test_tv_first_then_movie_fallback():
    base = TMDBService.BASE_URL
    responses = {
        f"{base}/tv/123": (None, 404),
        f"{base}/movie/123": ({"original_title": "Count It Down"}, 200),
        f"{base}/movie/123/alternative_titles": ({"titles": []}, 200),
    }
    svc = FakeTMDBService(responses)

    alias = await svc.get_alias_by_id(123, preferred_media="tv")

    assert alias == "Count It Down"
    assert svc.calls[:2] == [f"{base}/tv/123", f"{base}/movie/123"]


async def test_chinese_original_uses_english_alias():
    base = TMDBService.BASE_URL
    responses = {
        f"{base}/tv/325828": ({"original_name": "孤单又灿烂的神：鬼怪十周年特辑"}, 200),
        f"{base}/tv/325828/alternative_titles": (
            {"results": [{"iso_3166_1": "US", "title": "Jerry on the Job: The Bomb Idea"}]},
            200,
        ),
    }
    svc = FakeTMDBService(responses)

    alias = await svc.get_alias_by_id(325828, preferred_media="tv")

    assert alias == "Jerry on the Job: The Bomb Idea"


async def test_chinese_original_without_english_alias_returns_none():
    base = TMDBService.BASE_URL
    responses = {
        f"{base}/movie/289560": ({"original_title": "中央情报局"}, 200),
        f"{base}/movie/289560/alternative_titles": ({"titles": []}, 200),
    }
    svc = FakeTMDBService(responses)

    alias = await svc.get_alias_by_id(289560, preferred_media="movie")

    assert alias is None


async def test_non_chinese_non_english_uses_original_when_no_alias():
    base = TMDBService.BASE_URL
    responses = {
        f"{base}/movie/98765": ({"original_title": "ミステリー"}, 200),
        f"{base}/movie/98765/alternative_titles": ({"titles": []}, 200),
    }
    svc = FakeTMDBService(responses)

    alias = await svc.get_alias_by_id(98765, preferred_media="movie")

    assert alias == "ミステリー"


async def main():
    assert TMDBService._build_media_query_order("tv") == ["tv", "movie"]
    assert TMDBService._build_media_query_order("movie") == ["movie", "tv"]
    assert TMDBService._build_media_query_order(None) == ["movie", "tv"]

    await test_tv_first_then_movie_fallback()
    await test_chinese_original_uses_english_alias()
    await test_chinese_original_without_english_alias_returns_none()
    await test_non_chinese_non_english_uses_original_when_no_alias()
    await test_cache_media_conflict_bypasses_cache()
    print("All TMDB alias strategy tests passed")


async def test_cache_media_conflict_bypasses_cache():
    base = TMDBService.BASE_URL
    responses = {
        f"{base}/movie/4024": ({"original_title": "Last Year at Marienbad"}, 200),
        f"{base}/movie/4024/alternative_titles": ({"titles": []}, 200),
    }
    svc = FakeTMDBService(responses)
    svc._get_alias_cache = lambda tmdb_id: asyncio.sleep(0, result=SimpleNamespace(
        tmdb_id=tmdb_id,
        media_type="tv",
        chinese_title="去年在马里昂巴德",
        original_title="Invent This!",
        alias="Invent This!",
        source="original_english",
        status="success",
        note=None,
    ))

    alias = await svc.get_alias_by_id(4024, preferred_media="movie")

    assert alias == "Last Year at Marienbad"
    assert f"{base}/movie/4024" in svc.calls


if __name__ == "__main__":
    asyncio.run(main())
