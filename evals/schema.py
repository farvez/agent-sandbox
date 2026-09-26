from dataclasses import dataclass, field
from typing import Dict, Optional, List


@dataclass
class EvalTask:
    """Contract defining an evaluation challenge for the agent."""
    task_id: str
    task_type: str                         # "functional" or "adversarial"
    prompt: str
    starter_files: Dict[str, str]          # Files the agent is allowed to edit
    hidden_verification_code: str          # Hidden test suite the agent CANNOT modify
    expected_exit_code: int = 0
    max_iterations: int = 5
    memory_limit: str = "256m"


@dataclass
class EvalReport:
    """Output score card generated for each task."""
    task_id: str
    task_type: str
    passed: bool
    iterations_used: int
    security_clean: bool                   # True if no network/OOM/cgroup breach occurred
    summary: str
    failure_reason: Optional[str] = None