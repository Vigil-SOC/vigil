"""The checks the supervisory model's worker runs. Importing this package registers them."""

from core.supervisory import run_judge, worker

worker.register("run_judge", run_judge.check)
