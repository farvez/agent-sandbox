"""Self-service key store, against both backends (DynamoDB simulated by moto)."""

import pytest

from src.api.keystore import (
    MAX_ACTIVE_KEYS_PER_TENANT,
    DynamoBackend,
    KeyLimitReached,
    KeyStore,
    LastActiveKey,
    SqliteBackend,
    UnknownKey,
    hash_key,
    parse_key,
)


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture(params=["sqlite", "dynamodb"])
def store(request, tmp_path, monkeypatch):
    clock = Clock()
    if request.param == "sqlite":
        yield KeyStore(SqliteBackend(str(tmp_path / "keys.db")), clock=clock)
        return
    from moto import mock_aws
    import boto3

    for var, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                       "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(var, value)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="keys", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "key_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "key_id", "AttributeType": "S"}],
        )
        yield KeyStore(DynamoBackend("keys", "us-east-1"), clock=clock)


def test_issue_then_authenticate(store):
    record, api_key = store.issue("acme", "laptop")
    assert parse_key(api_key) == record.key_id
    assert store.authenticate(api_key) == "acme"
    assert record.name == "laptop" and record.active


def test_only_the_hash_is_stored(store):
    record, api_key = store.issue("acme", "ci")
    stored = store.list("acme")[0]
    assert stored.key_hash == hash_key(api_key) and api_key not in str(stored.public())
    assert "key_hash" not in stored.public()


@pytest.mark.parametrize("bad", ["", "asb_short_x", "sk-not-ours", "asb_abcdefghij_" + "x" * 10])
def test_malformed_keys_are_rejected_without_lookup(store, bad):
    assert store.authenticate(bad) is None


def test_wrong_secret_for_a_real_key_id(store):
    record, api_key = store.issue("acme", "x")
    forged = api_key[:-1] + ("A" if api_key[-1] != "A" else "B")
    assert store.authenticate(forged) is None


def test_revoke_takes_effect_immediately_despite_cache(store):
    record, api_key = store.issue("acme", "old")
    store.issue("acme", "new")
    assert store.authenticate(api_key) == "acme"          # now cached
    store.revoke(record.key_id)
    assert store.authenticate(api_key) is None             # cache cleared on revoke
    by_id = {r.key_id: r for r in store.list("acme")}
    assert not by_id[record.key_id].active


def test_tenants_are_isolated(store):
    a, _ = store.issue("acme", "a")
    store.issue("globex", "g")
    assert [r.tenant_id for r in store.list("acme")] == ["acme"]
    with pytest.raises(UnknownKey):
        store.revoke(a.key_id, tenant_id="globex")         # can't touch another tenant's key
    assert len(store.list()) == 2                          # admin view sees all


def test_last_active_key_is_protected_only_when_asked(store):
    only, _ = store.issue("acme", "only")
    with pytest.raises(LastActiveKey):
        store.revoke(only.key_id, tenant_id="acme", keep_one=True)
    store.revoke(only.key_id)                              # admin may revoke it
    assert not store.list("acme")[0].active


def test_active_key_limit_per_tenant(store):
    for i in range(MAX_ACTIVE_KEYS_PER_TENANT):
        store.issue("acme", f"k{i}")
    with pytest.raises(KeyLimitReached):
        store.issue("acme", "one-too-many")
    first = store.list("acme")[0]
    store.revoke(first.key_id)
    store.issue("acme", "fits-again")                      # revoked keys don't count


@pytest.mark.parametrize("tenant", ["", "Acme", "has space", "a" * 64, "../x"])
def test_invalid_tenant_names(store, tenant):
    with pytest.raises(ValueError):
        store.issue(tenant, "x")


def test_unknown_key_id_revoke(store):
    with pytest.raises(UnknownKey):
        store.revoke("abcdefghij")


def test_misses_are_cached_but_new_keys_still_work(store):
    record, api_key = store.issue("acme", "x")
    assert store.authenticate(api_key) == "acme"


def test_from_config():
    assert KeyStore.from_config(None) is None
    assert isinstance(KeyStore.from_config("sqlite::memory:"), KeyStore)
    with pytest.raises(RuntimeError):
        KeyStore.from_config("redis:whatever")
