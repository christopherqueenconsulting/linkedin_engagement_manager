"""Unit tests for city/state geocoding (Nominatim + timezonefinder)."""

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

_MOD = "cqc_lem.utilities.geocoding"


@pytest.fixture(autouse=True)
def _reset_state():
    import cqc_lem.utilities.geocoding as g
    g._cache.clear()
    g._last_call = 0.0
    with patch(f"{_MOD}.time.sleep"):
        yield


def _resp(payload, status=200):
    r = MagicMock()
    r.json.return_value = payload
    r.raise_for_status.side_effect = None if status == 200 else Exception("http")
    return r


class TestGeocodeCity:
    def test_resolves_city_with_tz_and_locale(self):
        from cqc_lem.utilities.geocoding import geocode_city
        payload = [{"lat": "28.5384", "lon": "-81.3789", "address": {"country_code": "us"}}]
        with patch(f"{_MOD}.requests.get", return_value=_resp(payload)) as get:
            geo = geocode_city("Orlando", "Florida", "US")
        assert round(geo["latitude"], 3) == 28.538 and round(geo["longitude"], 3) == -81.379
        assert geo["country"] == "US"
        assert geo["timezone"] == "America/New_York"   # real timezonefinder lookup
        assert geo["locale"] == "en-US"
        # User-Agent header must be set (Nominatim policy)
        assert "User-Agent" in get.call_args.kwargs["headers"]

    def test_nyc_coords_give_eastern(self):
        from cqc_lem.utilities.geocoding import geocode_city
        payload = [{"lat": "40.7128", "lon": "-74.0060", "address": {"country_code": "us"}}]
        with patch(f"{_MOD}.requests.get", return_value=_resp(payload)):
            geo = geocode_city("New York", "NY", "US")
        assert geo["timezone"] == "America/New_York"

    def test_no_match_raises(self):
        from cqc_lem.utilities.geocoding import GeocodeError, geocode_city
        with patch(f"{_MOD}.requests.get", return_value=_resp([])):
            with pytest.raises(GeocodeError):
                geocode_city("Nowhereville", "ZZ", "US")

    def test_request_failure_raises(self):
        from cqc_lem.utilities.geocoding import GeocodeError, geocode_city
        with patch(f"{_MOD}.requests.get", side_effect=Exception("timeout")):
            with pytest.raises(GeocodeError):
                geocode_city("Orlando")

    def test_empty_city_raises_without_calling_network(self):
        from cqc_lem.utilities.geocoding import GeocodeError, geocode_city
        with patch(f"{_MOD}.requests.get") as get:
            with pytest.raises(GeocodeError):
                geocode_city("   ")
        get.assert_not_called()

    def test_cache_short_circuits_second_call(self):
        from cqc_lem.utilities.geocoding import geocode_city
        payload = [{"lat": "28.5", "lon": "-81.3", "address": {"country_code": "us"}}]
        with patch(f"{_MOD}.requests.get", return_value=_resp(payload)) as get:
            geocode_city("Orlando", "FL", "US")
            geocode_city("Orlando", "FL", "US")
        get.assert_called_once()  # second call served from cache

    def test_missing_coordinates_raises(self):
        from cqc_lem.utilities.geocoding import GeocodeError, geocode_city
        with patch(f"{_MOD}.requests.get", return_value=_resp([{"address": {"country_code": "us"}}])):
            with pytest.raises(GeocodeError):
                geocode_city("Orlando", "FL", "US")
