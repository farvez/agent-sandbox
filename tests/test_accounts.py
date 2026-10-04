"""Console account store (users, invites, usage), on SQLite and DynamoDB (moto)."""
import threading

import pytest

from src.api.accounts import AccountStore, _DynamoItems, _SqliteItems, current_period


class Clock:
    def __init__(self):
        self.now = 1_790_000_000.0   # 2026-09

    def __call__(self):
        return self.now


@pytest.fixture(params=["sqlite", "dynamodb"])
def store(request, tmp_path, monkeypatch):
    clock = Clock()
    if request.param == "sqlite":
        yield AccountStore(_SqliteItems(str(tmp_path / "accounts.db")), clock=clock)
        return
    import boto3
    from moto import mock_aws

    for var, value in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(var, value)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="console", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        )
        yield AccountStore(_DynamoItems("console", "us-east-1"), clock=clock)


def test_user_keeps_its_tenant_across_logins(store):
    first = store.save_user(42, "Farvez", "Farvez", "https://a/1.png", "gh-farvez")
    store.save_user(42, "Farvez", "Farvez A.", "https://a/2.png", "gh-somethingelse")
    again = store.get_user(42)
    assert again["tenant_id"] == "gh-farvez" and again["name"] == "Farvez A."
    assert again["created_at"] == first["created_at"]
    assert [u["login"] for u in store.list_users()] == ["Farvez"]


def test_invites_are_case_insensitive(store):
    store.invite("SomeDev", invited_by="farvez")
    assert store.is_invited("somedev") and store.is_invited("SOMEDEV")
    assert [i["login"] for i in store.list_invites()] == ["somedev"]
    store.uninvite("SomeDev")
    assert not store.is_invited("somedev")


def test_usage_counters_accumulate_per_month(store):
    store.record_usage("gh-x", sessions=1)
    store.record_usage("gh-x", commands=1, command_seconds=1.5)
    store.record_usage("gh-x", commands=1, command_seconds=2.0)
    assert store.usage("gh-x") == {"tenant_id": "gh-x", "period": "2026-09", "sessions": 1, "commands": 2,
                                   "command_seconds": 3.5}
    store._clock.now += 40 * 86400                            # next month starts at zero
    assert store.usage("gh-x")["commands"] == 0
    assert store.usage("gh-x", period="2026-09")["commands"] == 2


def test_usage_is_per_tenant(store):
    store.record_usage("gh-a", commands=3)
    assert store.usage("gh-b")["commands"] == 0


def test_unknown_counter_is_refused(store):
    with pytest.raises(ValueError):
        store.record_usage("gh-x", bitcoins=1)


def test_concurrent_increments_are_not_lost(tmp_path):
    store = AccountStore(_SqliteItems(str(tmp_path / "a.db")))
    threads = [threading.Thread(target=lambda: [store.record_usage("t", commands=1) for _ in range(50)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.usage("t")["commands"] == 200


def test_period_format():
    assert current_period(1_790_000_000) == "2026-09"


def test_invite_requests_update_in_place_and_list_oldest_first(store):
    first = store.request_invite(3, "Stranger", "Stranger", "", "agents", "me@example.com")
    store._clock.now += 60
    store.request_invite(4, "other", "", "", "", "")
    store._clock.now += 60
    again = store.request_invite(3, "stranger", "Stranger", "", "agents + evals", "")
    assert again["requested_at"] == first["requested_at"] and again["note"] == "agents + evals"
    assert [r["login"] for r in store.list_requests()] == ["stranger", "other"]
    assert store.get_request("STRANGER")["display_login"] == "stranger"
    store.delete_request("Stranger")
    assert store.get_request("stranger") is None and len(store.list_requests()) == 1


def test_pending_requests_are_capped(store, monkeypatch):
    from src.api import accounts as accounts_module

    monkeypatch.setattr(accounts_module, "MAX_PENDING_REQUESTS", 2)
    store.request_invite(1, "a", "", "", "", "")
    store.request_invite(2, "b", "", "", "", "")
    with pytest.raises(accounts_module.RequestsFull):
        store.request_invite(3, "c", "", "", "", "")
    store.request_invite(1, "a", "", "", "updated", "")   # updating an existing request still works
