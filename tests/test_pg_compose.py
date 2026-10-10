"""The PG* component vars must compose a connection string that special
characters cannot break (the failure mode seen in the first Railway deploys)."""

from __future__ import annotations


def test_pg_components_compose_a_safe_url(monkeypatch):
    monkeypatch.setenv("PGHOST", "ep-abc123.eu-central-1.aws.neon.tech")
    monkeypatch.setenv("PGUSER", "neondb_owner")
    monkeypatch.setenv("PGPASSWORD", "p@ss:w/rd#1+2%3")  # every URL-killer at once
    monkeypatch.setenv("PGDATABASE", "neondb")

    from realign.config import Settings

    st = Settings()
    assert not st.is_sqlite
    assert st.database_url.startswith("postgresql+asyncpg://neondb_owner:")
    for token in ("%40", "%3A", "%2F", "%23", "%2B", "%25"):
        assert token in st.database_url, (token, st.database_url)
    assert st.database_url.endswith(
        "@ep-abc123.eu-central-1.aws.neon.tech:5432/neondb?sslmode=require"
    )


def test_pg_password_already_encoded_is_normalised(monkeypatch):
    # Pasted straight out of a Neon connection string (percent-encoded).
    monkeypatch.setenv("PGHOST", "ep-x.neon.tech")
    monkeypatch.setenv("PGPASSWORD", "p%40ss%23w")

    from realign.config import Settings

    st = Settings()
    assert "%2540" not in st.database_url  # not double-encoded
    assert ":p%40ss%23w@" in st.database_url


def test_no_pg_vars_keeps_database_url(monkeypatch):
    for var in ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.delenv(var, raising=False)

    from realign.config import Settings

    assert Settings().is_sqlite  # untouched default


def test_vercel_env_enables_serverless(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("SERVERLESS", raising=False)

    from realign.config import Settings

    assert Settings().serverless is True


def test_explicit_serverless_false_wins_over_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("SERVERLESS", "false")

    from realign.config import Settings

    assert Settings().serverless is False
