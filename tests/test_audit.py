"""Command audit log, on SQLite and DynamoDB (moto)."""
import pytest

from src.api.audit import MAX_COMMAND_CHARS, AuditLog, _DynamoAudit, _SqliteAudit


class Clock:
    def __init__(self):
        self.now = 1_790_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture(params=["sqlite", "dynamodb"])
def log(request, tmp_path, monkeypatch):
    clock = Clock()
    if request.param == "sqlite":
        yield AuditLog(_SqliteAudit(str(tmp_path / "audit.db")), retention_days=90, clock=clock)
        return
    import boto3
    from moto import mock_aws

    for var, value in {"AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t", "AWS_DEFAULT_REGION": "us-east-1"}.items():
        monkeypatch.setenv(var, value)
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="audit", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "tenant", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "tenant", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
        )
        yield AuditLog(_DynamoAudit("audit", "us-east-1"), retention_days=90, clock=clock)


def run(log, tenant, command, **kw):
    log._clock.now += 1
    return log.record(tenant, session_id="sbx_1", actor="key abc", kind="exec", command=command,
                      exit_code=kw.pop("exit_code", 0), duration_s=0.5, **kw)


def test_entries_come_back_newest_first_with_their_details(log):
    run(log, "acme", "ls")
    run(log, "acme", "python -m pytest", exit_code=1, timed_out=True)
    entries, cursor = log.list("acme")
    assert [e["command"] for e in entries] == ["python -m pytest", "ls"] and cursor is None
    first = entries[0]
    assert first["exit_code"] == 1 and isinstance(first["exit_code"], int)   # not 1.0 from DynamoDB
    assert first["timed_out"] is True and first["actor"] == "key abc"
    assert first["session_id"] == "sbx_1" and first["duration_s"] == 0.5 and first["kind"] == "exec"


def test_tenants_only_see_their_own_entries(log):
    run(log, "acme", "acme-cmd")
    run(log, "globex", "globex-cmd")
    assert [e["command"] for e in log.list("acme")[0]] == ["acme-cmd"]
    assert [e["command"] for e in log.list("globex")[0]] == ["globex-cmd"]
    assert log.list("nobody")[0] == []


def test_pagination_with_a_cursor(log):
    for i in range(5):
        run(log, "acme", f"cmd {i}")
    page1, cursor = log.list("acme", limit=2)
    page2, cursor2 = log.list("acme", limit=2, before=cursor)
    page3, cursor3 = log.list("acme", limit=2, before=cursor2)
    assert [e["command"] for e in page1 + page2 + page3] == ["cmd 4", "cmd 3", "cmd 2", "cmd 1", "cmd 0"]
    assert cursor3 is None


def test_long_commands_are_capped_and_flagged(log):
    run(log, "acme", "x" * (MAX_COMMAND_CHARS + 50))
    entry = log.list("acme")[0][0]
    assert len(entry["command"]) == MAX_COMMAND_CHARS and entry["truncated"] is True


def test_expired_entries_are_hidden_even_before_ttl_deletes_them(log):
    run(log, "acme", "old")
    log._clock.now += 91 * 86400
    run(log, "acme", "new")
    assert [e["command"] for e in log.list("acme")[0]] == ["new"]


def test_from_config():
    assert AuditLog.from_config(None) is None
    with pytest.raises(RuntimeError):
        AuditLog.from_config("redis:x")
