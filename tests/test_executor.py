"""The safety boundary. If any of these fail, the agent can run arbitrary code."""
import asyncio

import pytest

from sentinel.executor import ApprovalRequired, Executor
from sentinel.models import Anomaly, Diagnosis, Incident, ProposedFix, Signal


def _incident(fix: ProposedFix | None) -> Incident:
    return Incident(
        anomaly=Anomaly(service="s", title="t", signals=[Signal(name="m", observed=1, baseline=0, zscore=4)]),
        diagnosis=Diagnosis(summary="s", root_cause="r", proposed_fix=fix),
    )


def test_unknown_action_is_rejected():
    ex = Executor()
    with pytest.raises(ValueError, match="not an approved remediation"):
        ex.plan(ProposedFix(action="rm -rf /"))


def test_injected_shell_is_not_an_action():
    ex = Executor()
    with pytest.raises(ValueError):
        ex.plan(ProposedFix(action="restart_service; curl evil.example.com"))


def test_unknown_parameters_are_rejected():
    ex = Executor()
    with pytest.raises(ValueError, match="unknown parameters"):
        ex.plan(ProposedFix(action="set_timeout", params={"seconds": 2, "host": "evil.example.com"}))


def test_parameters_are_type_coerced():
    ex = Executor()
    _, params, argv = ex.plan(ProposedFix(action="set_timeout", params={"seconds": "2.5"}))
    assert params["seconds"] == 2.5
    assert argv[0] == "curl"
    assert not any(";" in part or "|" in part for part in argv), "no shell metacharacters reach argv"


def test_execution_requires_approval():
    ex = Executor()
    incident = _incident(ProposedFix(action="collect_diagnostics"))
    with pytest.raises(ApprovalRequired):
        asyncio.run(ex.execute(incident, approved_by=None))


def test_nothing_to_execute_without_a_fix():
    ex = Executor()
    with pytest.raises(ValueError, match="no proposed fix"):
        asyncio.run(ex.execute(_incident(None), approved_by="human"))
