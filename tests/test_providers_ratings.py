"""Rating tests restored from c49e93f (removed by mistake in ca10134)."""


"""Tests for the provider registry (S9 §C, step 2)."""






import pytest


from fastapi.testclient import TestClient




from finmeta.api.state import AppContext




CONFIG_YAML = """\
dry_run: true
server:
  url: http://fake:8096
  api_key_env: JELLYFIN_API_KEY
path_mappings:
  - jellyfin: /media
    local: /tmp/fake-media
libraries:
  Movies:
    type: movies
    metadata:
      enabled: true
    artwork:
      poster: true
    theme_song: false
    overlays: []
"""


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("FINMETA_AUTH", "off")
    monkeypatch.delenv("TMDB_KEY", raising=False)
    monkeypatch.delenv("FANART_KEY", raising=False)
    monkeypatch.delenv("OMDB_KEY", raising=False)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "config.yml").write_text(CONFIG_YAML)
    return AppContext(str(cfg_dir / "config.yml"), config_dir=str(cfg_dir))


@pytest.fixture
def client(ctx):
    from finmeta.api.app import create_app
    app = create_app(ctx)
    with TestClient(app) as c:
        yield c


def test_field_sources_defaults(ctx):
    """field_sources has the correct defaults."""
    cfg = ctx.config
    fs = cfg.sources.field_sources
    assert fs["rating_imdb"] == ["mdblist", "omdb", "imdb_datasets", "jellyfin"]
    assert fs["rating_rt_critic"] == ["mdblist", "omdb", "jellyfin"]
    assert fs["rating_rt_audience"] == ["mdblist"]
    assert fs["rating_metacritic"] == ["mdblist", "omdb"]
    assert fs["rating_tmdb"] == ["mdblist", "tmdb"]
    assert fs["rating_trakt"] == ["mdblist"]
    assert fs["rating_letterboxd"] == ["mdblist"]


def test_field_sources_rejects_unknown_field():
    from finmeta.config.models import SourcesConfig
    import pytest
    with pytest.raises(Exception):
        SourcesConfig(
            field_sources={"bogus_field": ["omdb"]},
            tmdb=SourcesConfig().tmdb,
            fanart=SourcesConfig().fanart,
            local=SourcesConfig().local,
            omdb=SourcesConfig().omdb,
            precedence=["local", "fanart", "tmdb"],
        )


def test_field_sources_rejects_unknown_provider():
    from finmeta.config.models import SourcesConfig
    import pytest
    with pytest.raises(Exception):
        SourcesConfig(
            field_sources={"rating_imdb": ["bogus_provider"]},
            tmdb=SourcesConfig().tmdb,
            fanart=SourcesConfig().fanart,
            local=SourcesConfig().local,
            omdb=SourcesConfig().omdb,
            precedence=["local", "fanart", "tmdb"],
        )


def test_compute_ratings_jellyfin_only(ctx):
    """When omdb is not in sources, ratings come from jellyfin item."""
    from finmeta.engine.plan_builder import _compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(
        id="1", name="Test", type="Movie",
        community_rating=8.5, critic_rating=72.0,
    )
    # no omdb in sources
    sources = {}
    ratings = _compute_ratings(item, None, sources, ctx.config)
    # rating_imdb: [mdblist, omdb, imdb_datasets, jellyfin] → jellyfin community_rating=8.5
    assert ratings["imdb"] == 8.5
    # rating_rt_critic: [mdblist, omdb, jellyfin] → jellyfin critic_rating=72.0
    assert ratings["rt_critic"] == 72.0
    # rating_metacritic: [mdblist, omdb] → no source, so None
    assert ratings["metacritic"] is None


def test_compute_ratings_omdb_overrides_jellyfin(ctx):
    """When omdb has a value, it wins over jellyfin (rating_imdb precedence)."""
    from finmeta.engine.plan_builder import _compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(
        id="1", name="Test", type="Movie",
        provider_ids={"Imdb": "tt0111161"},
        community_rating=8.5, critic_rating=72.0,
    )

    class FakeOmdb:
        def get_ratings(self, imdb_id):
            return {"imdb": 9.3, "rt_critic": 98.0, "metacritic": 82.0}

    sources = {"omdb": FakeOmdb()}
    ratings = _compute_ratings(item, None, sources, ctx.config)
    # rating_imdb: [mdblist, omdb, imdb_datasets, jellyfin] → omdb=9.3 wins
    assert ratings["imdb"] == 9.3
    # rating_rt_critic: [mdblist, omdb, jellyfin] → omdb=98.0 wins (omdb is first)
    assert ratings["rt_critic"] == 98.0
    # rating_metacritic: [mdblist, omdb] → omdb=82.0
    assert ratings["metacritic"] == 82.0


def test_compute_ratings_tmdb_detail(ctx):
    """TMDb rating is included when details are available."""
    from finmeta.engine.plan_builder import _compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(id="1", name="Test", type="Movie")
    details = {"vote_average": 7.8}
    ratings = _compute_ratings(item, details, {}, ctx.config)
    assert ratings["tmdb"] == 7.8


def test_compute_ratings_missing_values_are_none(ctx):
    """Missing jellyfin ratings → None in output."""
    from finmeta.engine.plan_builder import _compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(id="1", name="Test", type="Movie")  # no ratings
    ratings = _compute_ratings(item, None, {}, ctx.config)
    assert ratings["imdb"] is None
    assert ratings["rt_critic"] is None
    assert ratings["metacritic"] is None


def test_compute_ratings_fallback_same_scale(ctx):
    """Falling back across providers yields values on the SAME canonical scale.

    mdblist supplies imdb (0-10) but no rt_critic; omdb supplies rt_critic
    (0-100). The fallback must keep each field on its own scale.
    """
    from finmeta.engine.plan_builder import _compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(
        id="1", name="Test", type="Movie",
        provider_ids={"Imdb": "tt0111161"},
        community_rating=None, critic_rating=None,  # jellyfin has nothing
    )

    class FakeMdblist:
        def get_ratings(self, imdb_id):
            return {"imdb": 8.9, "rt_critic": None, "metacritic": None}

    class FakeOmdb:
        def get_ratings(self, imdb_id):
            return {"imdb": 9.3, "rt_critic": 95.0, "metacritic": 78.0}

    sources = {"mdblist": FakeMdblist(), "omdb": FakeOmdb()}
    ratings = _compute_ratings(item, None, sources, ctx.config)
    # rating_imdb: [mdblist, omdb, ...] -> mdblist wins (8.9), on 0-10 scale
    assert ratings["imdb"] == 8.9
    # rating_rt_critic: [mdblist, omdb, ...] -> mdblist None, falls back to omdb 95.0 (0-100)
    assert ratings["rt_critic"] == 95.0
    # Both fields are on their canonical scales: imdb <= 10, rt_critic <= 100
    assert 0 <= ratings["imdb"] <= 10
    assert 0 <= ratings["rt_critic"] <= 100


def test_compute_ratings_skips_disabled_provider(ctx):
    """A provider listed in field_sources but absent from sources is skipped."""
    from finmeta.engine.plan_builder import _compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(
        id="1", name="Test", type="Movie",
        provider_ids={"Imdb": "tt0111161"},
        community_rating=7.5, critic_rating=None,
    )

    class FakeMdblist:
        def get_ratings(self, imdb_id):
            return {"imdb": 9.9, "rt_critic": None}

    # mdblist is NOT in sources (disabled/unconfigured) -> skipped; falls to jellyfin
    sources = {}
    ratings = _compute_ratings(item, None, sources, ctx.config)
    # rating_imdb: [mdblist(skipped), omdb(absent), imdb_datasets(absent), jellyfin] -> 7.5
    assert ratings["imdb"] == 7.5


def test_compute_ratings_normalizes_provider_scale(ctx):
    """Provider values are normalised to the canonical field scale."""
    from finmeta.engine.plan_builder import _compute_ratings, _to_scale
    from finmeta.jellyfin_client.types import Item

    # string forms
    assert _to_scale("94%", "rt_critic") == 94.0
    assert _to_scale("82/100", "metacritic") == 82.0
    assert _to_scale(4, "letterboxd") == 4.0       # already 0-5
    assert _to_scale(8.7, "imdb") == 8.7           # already 0-10
    assert _to_scale(None, "imdb") is None

    item = Item(id="1", name="Test", type="Movie", provider_ids={"Imdb": "tt0111161"})

    class FakeOmdb:
        def get_ratings(self, imdb_id):
            return {"imdb": 9.1, "rt_critic": 96.0, "metacritic": None}

    sources = {"omdb": FakeOmdb()}
    ratings = _compute_ratings(item, None, sources, ctx.config)
    assert ratings["imdb"] == 9.1
    assert ratings["rt_critic"] == 96.0


def test_compute_ratings_records_source_per_field(ctx):
    """Each value records WHICH enabled source supplied it (e.g. imdb: mdblist)."""
    from finmeta.engine.plan_builder import compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(id="1", name="Test", type="Movie",
                provider_ids={"Imdb": "tt0111161"},
                community_rating=8.5, critic_rating=72.0)

    class FakeMdb:
        def get_ratings(self, imdb_id):
            return {"imdb": 9.1, "rt_critic": 94.0}

    sources = {"mdblist": FakeMdb()}
    out = compute_ratings(item, None, sources, ctx.config)
    assert out["values"]["imdb"] == 9.1
    assert out["sources"]["imdb"] == "mdblist"
    # rt_critic: mdblist first in the chain -> wins over jellyfin's critic_rating.
    assert out["values"]["rt_critic"] == 94.0
    assert out["sources"]["rt_critic"] == "mdblist"


def test_compute_ratings_source_none_when_no_value(ctx):
    """No enabled source with a value -> None in BOTH values and sources (no badge)."""
    from finmeta.engine.plan_builder import compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(id="1", name="Test", type="Movie")  # no imdb id, no jellyfin ratings
    out = compute_ratings(item, None, {}, ctx.config)
    for key in ("imdb", "rt_critic", "metacritic"):
        assert out["values"][key] is None
        assert out["sources"][key] is None


def test_compute_ratings_jellyfin_source_label(ctx):
    """Jellyfin CommunityRating/CriticRating map to source 'jellyfin'."""
    from finmeta.engine.plan_builder import compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(id="1", name="Test", type="Movie",
                community_rating=8.5, critic_rating=72.0)
    out = compute_ratings(item, None, {}, ctx.config)
    assert out["values"]["imdb"] == 8.5 and out["sources"]["imdb"] == "jellyfin"
    assert out["values"]["rt_critic"] == 72.0 and out["sources"]["rt_critic"] == "jellyfin"


def test_compute_ratings_legacy_wrapper_returns_values(ctx):
    """_compute_ratings keeps its old Dict[str, Optional[float]] contract."""
    from finmeta.engine.plan_builder import _compute_ratings
    from finmeta.jellyfin_client.types import Item

    item = Item(id="1", name="Test", type="Movie", community_rating=8.5)
    ratings = _compute_ratings(item, None, {}, ctx.config)
    assert ratings == {"imdb": 8.5, "rt_critic": None, "rt_audience": None,
                       "metacritic": None, "tmdb": None, "trakt": None,
                       "letterboxd": None}


def test_compute_ratings_calls_each_provider_at_most_once(ctx):
    """2026-10-23: field_sources lists mdblist first for 6 of 7 fields, and
    one get_ratings() call already returns every field a provider has --
    without memoizing within this call, a title mdblist has no data for
    (which its OWN cache deliberately never caches, so a transient failure
    can retry) re-hit the live call once per field, each serialized behind
    the client's 1s rate-limit throttle: ~6s for one item, found while the
    operator reported overlay-preview rendering was "extremely slow"."""
    from finmeta.engine.plan_builder import compute_ratings
    from finmeta.jellyfin_client.types import Item

    class CountingMdblist:
        def __init__(self):
            self.calls = 0

        def get_ratings(self, imdb_id):
            self.calls += 1
            return {"imdb": None, "rt_critic": None, "rt_audience": None,
                    "metacritic": None, "tmdb": None, "trakt": None, "letterboxd": None}

    item = Item(id="1", name="Test", type="Movie", provider_ids={"Imdb": "tt0000001"})
    mdblist = CountingMdblist()
    compute_ratings(item, None, {"mdblist": mdblist}, ctx.config)
    # mdblist is first in field_sources for imdb/rt_audience/rt_critic/
    # metacritic/tmdb/trakt/letterboxd (6-7 fields) -- without the memo this
    # would be 6+ calls for the exact same (provider, imdb_id).
    assert mdblist.calls == 1
