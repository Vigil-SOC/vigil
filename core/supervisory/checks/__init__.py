"""The checks the supervisory model's worker runs. Importing this package registers them."""

from core.supervisory import worker
from core.supervisory.checks import review_threshold

worker.register("review_threshold", review_threshold.check)
