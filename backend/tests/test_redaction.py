import pytest

from marketpulse.core.redaction import REDACTED, redact_headers, redact_text, redact_url

SECRET = "TEST-SECRET-9f8e7d6c5b4a"  # synthetic test value


@pytest.mark.parametrize("name", ["api_key", "API_KEY", "apikey", "token", "subscription-key"])
def test_redact_url_hides_credential_params(name: str) -> None:
    url = f"https://api.example.com/v1/data?series_id=DGS10&{name}={SECRET}&file_type=json"
    redacted = redact_url(url)

    assert SECRET not in redacted
    assert f"{name}={REDACTED}" in redacted
    assert "series_id=DGS10" in redacted
    assert "file_type=json" in redacted


def test_redact_url_extra_param_names() -> None:
    url = f"https://api.example.com/?custom_auth={SECRET}&x=1"
    assert SECRET in redact_url(url)  # not a known name
    assert SECRET not in redact_url(url, extra_param_names=["CUSTOM_AUTH"])


def test_redact_url_hides_userinfo_password() -> None:
    redacted = redact_url(f"https://user:{SECRET}@api.example.com/path")
    assert SECRET not in redacted
    assert redacted == f"https://user:{REDACTED}@api.example.com/path"


def test_redact_url_keeps_eia_style_bracket_params_readable() -> None:
    url = f"https://api.eia.gov/v2/x/data/?facets[series][]=RWTC&api_key={SECRET}"
    redacted = redact_url(url)
    assert "facets[series][]=RWTC" in redacted
    assert SECRET not in redacted


def test_redact_url_without_query_is_unchanged() -> None:
    assert redact_url("https://api.example.com/v1/x") == "https://api.example.com/v1/x"


def test_redact_headers() -> None:
    headers = {"Authorization": f"Bearer {SECRET}", "X-Api-Key": SECRET, "Accept": "json"}
    redacted = redact_headers(headers)
    assert redacted == {"Authorization": REDACTED, "X-Api-Key": REDACTED, "Accept": "json"}
    assert headers["Authorization"] != REDACTED  # input not mutated


def test_redact_text_removes_raw_and_url_encoded_secret_values() -> None:
    secret = "abc/def+ghi=="
    text = f"raw={secret} encoded=abc%2Fdef%2Bghi%3D%3D plus=abc%2Fdef%2Bghi%3D%3D"
    assert "abc" not in redact_text(text, [secret])


def test_redact_text_removes_credential_params_in_free_text() -> None:
    text = f'GET https://api.example.com/x?a=1&api_key={SECRET} "HTTP/1.1 200 OK"'
    redacted = redact_text(text)
    assert SECRET not in redacted
    assert f"api_key={REDACTED}" in redacted
    assert "a=1" in redacted


@pytest.mark.parametrize(
    "text",
    [
        f"request failed: api_key={SECRET} (timeout)",
        f"error, token={SECRET}, retrying",
        f"API_KEY={SECRET}",
        f"failed (api_key={SECRET})",
    ],
)
def test_redact_text_removes_credential_params_outside_urls(text: str) -> None:
    redacted = redact_text(text)
    assert SECRET not in redacted
    assert REDACTED in redacted


@pytest.mark.parametrize("text", ["monkey=banana", "turnkey=yes", "my_api_key=x"])
def test_redact_text_does_not_match_inside_longer_words(text: str) -> None:
    assert redact_text(text) == text


def test_redact_text_ignores_very_short_secrets() -> None:
    assert redact_text("a b c", ["a"]) == "a b c"
