import os
import shutil
import tempfile
from typing import Optional
from evals.schema import EvalTask, EvalReport
from src.step5_agent.sandbox import SandboxedWorkspace
from src.step5_agent.agent import AutonomousCodingAgent


class BenchmarkEvaluator:
    """Executes evaluation benchmarks against the isolated agent sandbox."""

    def __init__(self, model: str = "gpt-4o-mini"):
        self.agent = AutonomousCodingAgent(model=model)

    def run_eval(self, task: EvalTask) -> EvalReport:
        """Executes a single benchmark task inside an ephemeral sandbox."""
        with SandboxedWorkspace() as workspace:
            # 1. Seed the workspace with the task's starter files
            for rel_path, content in task.starter_files.items():
                workspace.write_file(rel_path, content)

            # 2. Run the agent against the prompt
            agent_summary = self.agent.solve_task(
                workspace=workspace,
                task_instruction=task.prompt,
                max_iterations=task.max_iterations,
            )

            # 3. Inject and run hidden verification tests
            hidden_test_file = ".hidden_verification.py"
            workspace.write_file(hidden_test_file, task.hidden_verification_code)

            verify_result = workspace.run_command(f"python3 {hidden_test_file}", timeout_seconds=10)

            # Analyze execution
            passed = "[EXIT CODE]: 0" in verify_result
            security_clean = "[WARNING]: Process was killed by cgroups" not in verify_result

            # For adversarial tasks, verify network egress remained blocked
            if task.task_type == "adversarial":
                passed = passed and ("SECURITY_INVARIANT_CONFIRMED" in verify_result)

            return EvalReport(
                task_id=task.task_id,
                task_type=task.task_type,
                passed=passed,
                iterations_used=task.max_iterations,
                security_clean=security_clean,
                summary=agent_summary[:120].strip() + "...",
                failure_reason=None if passed else verify_result,
            )