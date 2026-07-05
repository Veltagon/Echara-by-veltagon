# DEFINITION OF DONE

A build is delivered if and only if:
  - import smoke passes (`from app.main import app` exits 0)
  - alembic upgrade head passes (all migrations apply cleanly)
  - pytest passes (all collected tests pass, excluding py_compile failures)
  - All three checks above pass in a single verification run

No other metric is a gate. No LLM scores. No quality thresholds.
No refine loops. No jury reviews. No F500 mock reviews.

If verification fails, Builder gets the exact error and retries (max 3).
If it fails after 3 retries, the build is failed. Period.
