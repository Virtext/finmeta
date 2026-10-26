"""Unit tests for finmeta.jellyfin_client (respx-mocked HTTP)."""
from __future__ import annotations

import pytest
import respx
import httpx

from finmeta.jellyfin_client import (
    JellyfinClient, JellyfinError, ServerResetRequired,
    PathMapper, translate_path,
    Item, Library,
)

BASE = "http://testserver"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _auth_ok():
    """Mock a successful /Users/AuthenticateByName."""
    return respx.post(f"{BASE}/Users/AuthenticateByName").mock(
        return_value=httpx.Response(200, json={
            "AccessToken": "tok123",
            "User": {"Id": "uid-1", "Name": "finmeta"},
        })
    )


def _wizard_not_done():
    return respx.get(f"{BASE}/System/Info/Public").mock(
        return_value=httpx.Response(200, json={
            "ServerName": "test", "Version": "13.0.0",
            "StartupWizardCompleted": False,
        })
    )


def _wizard_done():
    return respx.get(f"{BASE}/System/Info/Public").mock(
        return_value=httpx.Response(200, json={
            "ServerName": "test", "Version": "13.0.0",
            "StartupWizardCompleted": True,
        })
    )


# ---------------------------------------------------------------------------
# system_info
# ---------------------------------------------------------------------------

class TestSystemInfo:
    @respx.mock
    def test_basic(self):
        respx.get(f"{BASE}/System/Info/Public").mock(
            return_value=httpx.Response(200, json={
                "ServerName": "myserver", "Version": "13.0.0",
                "StartupWizardCompleted": False,
            })
        )
        with JellyfinClient(BASE) as c:
            info = c.system_info()
        assert info.server_name == "myserver"
        assert info.version == "13.0.0"
        assert info.startup_wizard_completed is False


# ---------------------------------------------------------------------------
# authenticate
# ---------------------------------------------------------------------------

class TestAuthenticate:
    @respx.mock
    def test_success(self):
        _auth_ok()
        with JellyfinClient(BASE) as c:
            auth = c.authenticate("finmeta", "pass")
        assert auth.access_token == "tok123"
        assert auth.user_id == "uid-1"
        assert auth.user_name == "finmeta"
        assert c.token == "tok123"
        assert c.user_id == "uid-1"

    @respx.mock
    def test_error(self):
        respx.post(f"{BASE}/Users/AuthenticateByName").mock(
            return_value=httpx.Response(400, text="Error processing request.")
        )
        with JellyfinClient(BASE) as c:
            with pytest.raises(JellyfinError, match="400"):
                c.authenticate("finmeta", "bad")


# ---------------------------------------------------------------------------
# complete_wizard
# ---------------------------------------------------------------------------

class TestCompleteWizard:
    @respx.mock
    def test_full_wizard_flow(self):
        """All wizard steps succeed, user is persisted, auth works."""
        # Step 0: check wizard status
        _wizard_not_done()
        # Step 1: configuration
        respx.post(f"{BASE}/Startup/Configuration").mock(
            return_value=httpx.Response(204)
        )
        # Step 2a: GET /Startup/User (initializes)
        # Step 2b: POST /Startup/User
        respx.post(f"{BASE}/Startup/User").mock(
            return_value=httpx.Response(204)
        )
        # Step 3: GET /Startup/User (verify) -> returns the user
        def _get_user(request):
            # First GET (initialize) returns empty, second GET (verify) returns user
            # We need to differentiate by call count
            return httpx.Response(200, json={"Name": "finmeta", "Password": None})
        respx.get(f"{BASE}/Startup/User").mock(side_effect=_get_user)
        # Step 4: remote access + complete
        respx.post(f"{BASE}/Startup/RemoteAccess").mock(
            return_value=httpx.Response(204)
        )
        respx.post(f"{BASE}/Startup/Complete").mock(
            return_value=httpx.Response(204)
        )
        # Step 5: auth
        _auth_ok()

        with JellyfinClient(BASE) as c:
            auth = c.complete_wizard("finmeta", "testpass")
        assert auth.access_token == "tok123"

    @respx.mock
    def test_user_not_persisted_stops(self):
        """POST /Startup/User returns 204 but GET shows Name is empty -> STOP."""
        _wizard_not_done()
        respx.post(f"{BASE}/Startup/Configuration").mock(
            return_value=httpx.Response(204)
        )
        respx.post(f"{BASE}/Startup/User").mock(
            return_value=httpx.Response(204)
        )
        # GET /Startup/User: first call (init) returns {}, second call (verify) returns {}
        call_count = {"n": 0}
        def _get_user(request):
            call_count["n"] += 1
            return httpx.Response(200, json={})
        respx.get(f"{BASE}/Startup/User").mock(side_effect=_get_user)
        # /Startup/Complete should NOT be called
        complete_route = respx.post(f"{BASE}/Startup/Complete").mock(
            return_value=httpx.Response(204)
        )

        with JellyfinClient(BASE) as c:
            with pytest.raises(ServerResetRequired, match="test server needs reset"):
                c.complete_wizard("finmeta", "testpass")

        # /Startup/Complete must not have been called
        assert complete_route.call_count == 0

    @respx.mock
    def test_wizard_done_auth_fails(self):
        """Wizard already completed but auth fails -> ServerResetRequired."""
        _wizard_done()
        respx.post(f"{BASE}/Users/AuthenticateByName").mock(
            return_value=httpx.Response(400, text="Error processing request.")
        )
        with JellyfinClient(BASE) as c:
            with pytest.raises(ServerResetRequired, match="test server needs reset"):
                c.complete_wizard("finmeta", "badpass")

    @respx.mock
    def test_wizard_done_auth_succeeds(self):
        """Wizard already completed and auth works -> returns AuthResult."""
        _wizard_done()
        _auth_ok()
        with JellyfinClient(BASE) as c:
            auth = c.complete_wizard("finmeta", "pass")
        assert auth.access_token == "tok123"


# ---------------------------------------------------------------------------
# path mapping
# ---------------------------------------------------------------------------

class TestPathMapping:
    def test_basic_translate(self):
        entries = [{"jellyfin": "/media", "local": "/work/fixture/library"}]
        local = translate_path("/media/Movies/The Matrix (1999) [tmdbid-603]/The Matrix (1999).mkv", entries)
        assert local == "/work/fixture/library/Movies/The Matrix (1999) [tmdbid-603]/The Matrix (1999).mkv"

    def test_unmapped(self):
        entries = [{"jellyfin": "/media", "local": "/work/fixture/library"}]
        assert translate_path("/opt/other/file.mkv", entries) is None

    def test_none_path(self):
        entries = [{"jellyfin": "/media", "local": "/work/fixture/library"}]
        assert translate_path(None, entries) is None

    def test_empty_path(self):
        entries = [{"jellyfin": "/media", "local": "/work/fixture/library"}]
        assert translate_path("", entries) is None

    def test_exact_prefix(self):
        entries = [{"jellyfin": "/media", "local": "/work/fixture/library"}]
        assert translate_path("/media", entries) == "/work/fixture/library"

    def test_longer_prefix_wins(self):
        entries = [
            {"jellyfin": "/media", "local": "/A"},
            {"jellyfin": "/media/Movies", "local": "/B"},
        ]
        mapper = PathMapper.from_config(entries)
        assert mapper.translate("/media/Movies/x.mkv") == "/B/x.mkv"
        assert mapper.translate("/media/Shows/y.mkv") == "/A/Shows/y.mkv"

    def test_is_mapped(self):
        entries = [{"jellyfin": "/media", "local": "/work/fixture/library"}]
        mapper = PathMapper.from_config(entries)
        assert mapper.is_mapped("/media/Movies/x.mkv")
        assert not mapper.is_mapped("/other/path.mkv")


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

class TestTypes:
    def test_item_from_dict(self):
        d = {
            "Id": "abc",
            "Name": "The Matrix",
            "Type": "Movie",
            "ParentId": "parent1",
            "Path": "/media/Movies/The Matrix/The Matrix.mkv",
            "ProviderIds": {"Tmdb": "603", "Imdb": "tt0133093"},
            "ImageTags": {"Primary": "tag1"},
            "BackdropImageTags": ["tag2"],
        }
        item = Item.from_dict(d)
        assert item.id == "abc"
        assert item.name == "The Matrix"
        assert item.type == "Movie"
        assert item.path == "/media/Movies/The Matrix/The Matrix.mkv"
        assert item.provider_ids == {"Tmdb": "603", "Imdb": "tt0133093"}
        assert item.tmdb_id() == "603"

    def test_item_tmdb_id_none(self):
        d = {"Id": "x", "Name": "X", "Type": "Movie", "ProviderIds": {}}
        item = Item.from_dict(d)
        assert item.tmdb_id() is None

    def test_library_from_dict(self):
        d = {
            "Name": "Movies",
            "ItemId": "lib1",
            "CollectionType": "movies",
            "Locations": ["/media/Movies"],
            "RefreshStatus": "",
            "RefreshProgress": 0.0,
        }
        lib = Library.from_dict(d)
        assert lib.name == "Movies"
        assert lib.item_id == "lib1"
        assert lib.collection_type == "movies"
        assert lib.locations == ["/media/Movies"]


# ---------------------------------------------------------------------------
# _join / _params
# ---------------------------------------------------------------------------

class TestParams:
    def test_join_none(self):
        from finmeta.jellyfin_client.client import _join
        assert _join(None) is None

    def test_join_list(self):
        from finmeta.jellyfin_client.client import _join
        assert _join(["a", "b", "c"]) == "a,b,c"

    def test_join_bool(self):
        from finmeta.jellyfin_client.client import _join
        assert _join(True) == "true"
        assert _join(False) == "false"

    def test_join_scalar(self):
        from finmeta.jellyfin_client.client import _join
        assert _join(42) == "42"
        assert _join("hello") == "hello"

    def test_params_skips_none(self):
        with JellyfinClient(BASE) as c:
            p = c._params(a="x", b=None, c=1)
        assert p == {"a": "x", "c": "1"}


# ---------------------------------------------------------------------------
# get_items (mocked)
# ---------------------------------------------------------------------------

class TestGetItems:
    @respx.mock
    def test_get_items(self):
        respx.get(f"{BASE}/Items").mock(
            return_value=httpx.Response(200, json={
                "Items": [
                    {"Id": "1", "Name": "Movie A", "Type": "Movie",
                     "Path": "/media/Movies/A/A.mkv", "ProviderIds": {"Tmdb": "100"}},
                    {"Id": "2", "Name": "Movie B", "Type": "Movie",
                     "Path": "/media/Movies/B/B.mkv", "ProviderIds": {"Tmdb": "200"}},
                ],
                "TotalRecordCount": 2,
            })
        )
        with JellyfinClient(BASE, token="tok", user_id="uid") as c:
            items = c.get_items()
        assert len(items) == 2
        assert items[0].id == "1"
        assert items[0].tmdb_id() == "100"
        assert items[1].tmdb_id() == "200"

    @respx.mock
    def test_count_items(self):
        respx.get(f"{BASE}/Items").mock(
            return_value=httpx.Response(200, json={
                "Items": [{"Id": "1", "Name": "A", "Type": "Movie"}] * 10,
                "TotalRecordCount": 10,
            })
        )
        with JellyfinClient(BASE, token="tok", user_id="uid") as c:
            count = c.count_items("lib1")
        assert count == 10


# ---------------------------------------------------------------------------
# S9b2: base_path normalization + client prefixing + verify_ssl/timeout
# ---------------------------------------------------------------------------

class TestNormalizeBasePath:
    def test_none_and_empty(self):
        from finmeta.jellyfin_client import normalize_base_path
        assert normalize_base_path(None) == ""
        assert normalize_base_path("") == ""
        assert normalize_base_path("   ") == ""
        assert normalize_base_path("/") == ""

    def test_adds_leading_slash_strips_trailing(self):
        from finmeta.jellyfin_client import normalize_base_path
        assert normalize_base_path("jellyfin") == "/jellyfin"
        assert normalize_base_path("/jellyfin") == "/jellyfin"
        assert normalize_base_path("/jellyfin/") == "/jellyfin"
        assert normalize_base_path("a/b") == "/a/b"
        assert normalize_base_path("a/b/c//") == "/a/b/c"


class TestBasePathPrefixing:
    @respx.mock
    def test_system_info_is_prefixed(self):
        route = respx.get("http://testserver/jellyfin/System/Info/Public").mock(
            return_value=httpx.Response(200, json={
                "ServerName": "s", "Version": "13.0.0", "StartupWizardCompleted": True,
            }))
        with JellyfinClient(BASE, base_path="/jellyfin") as c:
            info = c.system_info()
        assert info.server_name == "s"
        assert route.called

    @respx.mock
    def test_no_base_path_is_unprefixed(self):
        route = respx.get(f"{BASE}/System/Info/Public").mock(
            return_value=httpx.Response(200, json={
                "ServerName": "s", "Version": "13.0.0", "StartupWizardCompleted": True,
            }))
        with JellyfinClient(BASE) as c:
            c.system_info()
        assert route.called

    @respx.mock
    def test_authenticate_prefixed(self):
        route = respx.post("http://testserver/jellyfin/Users/AuthenticateByName").mock(
            return_value=httpx.Response(200, json={
                "AccessToken": "tok", "User": {"Id": "uid", "Name": "u"}}))
        with JellyfinClient(BASE, base_path="/jellyfin") as c:
            auth = c.authenticate("u", "p")
        assert auth.access_token == "tok"
        assert route.called


class TestClientConnectionOptions:
    def test_timeout_base_path_verify_forwarded_to_httpx(self):
        import finmeta.jellyfin_client.client as mod
        from unittest import mock
        with mock.patch.object(mod.httpx, "Client") as fake_client:
            fake_client.return_value = mock.MagicMock()
            c = JellyfinClient("http://testserver", timeout=12.5,
                               base_path="/jellyfin", verify_ssl=False)
        fake_client.assert_called_once()
        kw = fake_client.call_args.kwargs
        assert kw["base_url"] == "http://testserver"
        assert kw["timeout"] == 12.5
        assert kw["verify"] is False
        # base_path is normalized before being stored
        assert c.base_path == "/jellyfin"

    def test_default_timeout_and_verify(self):
        import finmeta.jellyfin_client.client as mod
        from unittest import mock
        with mock.patch.object(mod.httpx, "Client") as fake_client:
            fake_client.return_value = mock.MagicMock()
            c = JellyfinClient("http://testserver")
        kw = fake_client.call_args.kwargs
        assert kw["timeout"] == 30.0
        assert kw["verify"] is True
        assert c.base_path == ""


# ---------------------------------------------------------------------------
# upload_image: real servers Base64-decode the POST body despite the spec
# saying raw binary (confirmed against Jellyfin 13.0.0 -- raw bytes 500 with
# "System.FormatException: One of the identified items was in an invalid
# format."). get_image_bytes (GET) stays raw.
# ---------------------------------------------------------------------------

class TestUploadImage:
    @respx.mock
    def test_body_is_base64_encoded(self):
        import base64
        raw = b"\xff\xd8\xff\xe0not-really-a-jpeg"
        route = respx.post(f"{BASE}/Items/item1/Images/Primary").mock(
            return_value=httpx.Response(204)
        )
        with JellyfinClient(BASE, token="tok", user_id="uid") as c:
            c.upload_image("item1", "Primary", raw, content_type="image/jpeg")
        assert route.called
        sent = route.calls.last.request
        assert sent.content == base64.b64encode(raw)
        assert sent.headers["Content-Type"] == "image/jpeg"

    @respx.mock
    def test_raises_on_non_2xx(self):
        respx.post(f"{BASE}/Items/item1/Images/Primary").mock(
            return_value=httpx.Response(500, text="Error processing request.")
        )
        with JellyfinClient(BASE, token="tok", user_id="uid") as c:
            with pytest.raises(JellyfinError):
                c.upload_image("item1", "Primary", b"abc")
