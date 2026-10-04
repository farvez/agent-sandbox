from examples.learning.step3_tracing.tracer import parse_strace_summary

SUMMARY_WITH_ERRORS = """\
% time     seconds  usecs/call     calls    errors syscall
------ ----------- ----------- --------- --------- ----------------
 45.00    0.000090           3        30         4 openat
 30.00    0.000060           6        10           write
 25.00    0.000050          50         1         1 connect
------ ----------- ----------- --------- --------- ----------------
100.00    0.000200           4        41         5 total
"""


def test_reads_calls_column_not_errors_column():
    counts = parse_strace_summary(SUMMARY_WITH_ERRORS)
    assert counts == {"openat": 30, "write": 10, "connect": 1}


def test_total_row_is_excluded():
    assert "total" not in parse_strace_summary(SUMMARY_WITH_ERRORS)


def test_empty_or_garbage_input():
    assert parse_strace_summary("") == {}
    assert parse_strace_summary("strace: attach: ptrace(PTRACE_SEIZE): Operation not permitted\n") == {}
