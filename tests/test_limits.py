import pytest

from src.api.limits import BUILTIN_DEFAULTS, LimitExceeded, LimitTracker, TenantLimits, load_tenant_limits


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def tracker(config=None, clock=None):
    return LimitTracker(load_tenant_limits(config), clock=clock or FakeClock())


# ---------------------------------------------------------------- config


def test_builtin_defaults_when_unconfigured():
    table = load_tenant_limits(None)
    assert table["*"] == TenantLimits(**BUILTIN_DEFAULTS)


def test_star_sets_defaults_and_tenants_override_fields():
    table = load_tenant_limits('{"*": {"max_sessions": 3}, "acme": {"requests_per_minute": 600}}')
    assert table["*"].max_sessions == 3
    assert table["acme"].max_sessions == 3                 # inherited from "*"
    assert table["acme"].requests_per_minute == 600        # overridden
    assert table["acme"].max_concurrent_exec == BUILTIN_DEFAULTS["max_concurrent_exec"]


def test_unlisted_tenant_gets_star_defaults():
    t = tracker('{"*": {"max_sessions": 2}}')
    assert t.for_tenant("unknown-tenant").max_sessions == 2


@pytest.mark.parametrize(
    "raw",
    ['["x"]', '{"acme": 5}', '{"acme": {"max_session": 5}}', '{"acme": {"max_sessions": 0}}',
     '{"acme": {"max_sessions": "5"}}', '{"acme": {"max_sessions": true}}', "not json"],
)
def test_bad_config_is_rejected(raw):
    with pytest.raises((RuntimeError, ValueError)):
        load_tenant_limits(raw)


# ---------------------------------------------------------------- request rate


def test_rate_allows_a_burst_then_refills():
    clock = FakeClock()
    t = tracker('{"*": {"requests_per_minute": 3}}', clock)
    for _ in range(3):
        t.check_rate("acme")
    with pytest.raises(LimitExceeded) as exc:
        t.check_rate("acme")
    assert exc.value.retry_after == 20          # one token per 20 s at 3/min

    clock.now += 20
    t.check_rate("acme")                         # refilled one
    with pytest.raises(LimitExceeded):
        t.check_rate("acme")


def test_rate_limits_are_per_tenant():
    t = tracker('{"*": {"requests_per_minute": 1}}')
    t.check_rate("acme")
    t.check_rate("globex")                       # unaffected by acme
    with pytest.raises(LimitExceeded):
        t.check_rate("acme")


# ---------------------------------------------------------------- sessions


def test_session_limit_and_release():
    t = tracker('{"*": {"max_sessions": 2}}')
    t.open_session("acme")
    t.open_session("acme")
    with pytest.raises(LimitExceeded, match="2 of 2"):
        t.open_session("acme")
    t.open_session("globex")                     # other tenants unaffected
    t.close_session("acme")
    t.open_session("acme")


def test_close_never_goes_negative():
    t = tracker()
    t.close_session("acme")
    t.close_session("acme")
    assert t.usage("acme")["sessions_open"] == 0


# ---------------------------------------------------------------- running commands


def test_concurrent_command_limit_and_release_on_error():
    t = tracker('{"*": {"max_concurrent_exec": 1}}')
    with t.running_command("acme"):
        with pytest.raises(LimitExceeded, match="1 allowed"):
            with t.running_command("acme"):
                pass
    with pytest.raises(RuntimeError):
        with t.running_command("acme"):
            raise RuntimeError("command failed")
    with t.running_command("acme"):              # slot was released despite the error
        pass


def test_usage_report():
    clock = FakeClock()
    t = tracker('{"*": {"requests_per_minute": 10}}', clock)
    t.open_session("acme")
    t.check_rate("acme")
    with t.running_command("acme"):
        report = t.usage("acme")
    assert report["sessions_open"] == 1
    assert report["commands_running"] == 1
    assert report["requests_available"] == 9
    assert report["limits"]["requests_per_minute"] == 10
