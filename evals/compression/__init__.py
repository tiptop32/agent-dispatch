"""Frozen context-compression evaluations; default execution is offline and free.

Live semantic split selection defaults to all 30 cases. Calibration (10) and
holdout (20) reports cover only that subset, never the full gate. Paired failures
retain task IDs, cancellation outcomes and unconfirmed termination in checkpoints.
Merged usage is a known sum, not a complete bill when repair/recovery usage is
unobserved. Production thresholds, prompts and the frozen corpus are unchanged.
"""
