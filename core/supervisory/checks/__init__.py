"""The checks the supervisory model's worker runs. Importing this package registers them."""

from core.supervisory import worker
from core.supervisory.checks import limits, review_threshold, run_judge

worker.register("review_threshold", review_threshold.check)
worker.register("investigation_limits", limits.check)
worker.register("run_judge", run_judge.check)
