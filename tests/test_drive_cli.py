import urllib.parse

import pytest

from hari_lari_analytics.distribution.drive import SCOPES, drive_session
from hari_lari_analytics.distribution.drive_cli import REDIRECT_URI, auth_url, code_from


def test_auth_url_asks_for_offline_access_and_the_drive_scope():
    url = auth_url("abc.apps.googleusercontent.com")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["client_id"] == ["abc.apps.googleusercontent.com"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
    assert query["scope"] == [" ".join(SCOPES)]


def test_code_is_read_from_the_redirected_address():
    assert code_from(f"{REDIRECT_URI}/?code=4%2F0AbC&scope=x") == "4/0AbC"
    assert code_from("  4/0AbC \n") == "4/0AbC"


def test_a_refused_consent_is_explained():
    with pytest.raises(RuntimeError, match="access_denied"):
        code_from(f"{REDIRECT_URI}/?error=access_denied")
    with pytest.raises(RuntimeError, match="no .code="):
        code_from(f"{REDIRECT_URI}/?state=1")


def test_session_acts_with_the_refresh_token():
    session = drive_session("id", "secret", "refresh")
    credentials = session.credentials
    assert credentials.refresh_token == "refresh" and credentials.client_id == "id"
    assert list(credentials.scopes) == SCOPES
