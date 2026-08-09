"""The Celery half of LEM: the app itself (`my_celery`), its config, and the task modules.

The imports below are load-bearing, not conveniences. `my_celery` calls
`autodiscover_tasks(['cqc_lem'])`, which looks for a `cqc_lem.tasks` module that does not exist — so
importing this package is what actually pulls in the modules carrying `@app.task`, and those modules
are exactly the ones that define one. Dropping one as an "unused import" unregisters its tasks: the
worker starts clean and every message on those queues is rejected as unknown.

`engagement` is listed explicitly for that reason, even though `run_automation` imports it today for
two tasks it dispatches. That import is a fact about the current code, not a guarantee; the day a
context leaves `run_automation` with nothing dispatching back, the only thing still registering its
tasks would be this line.
"""
from . import (
    aws_test_celery_task,
    engagement,
    run_automation,
    run_avatar,
    run_content_plan,
    run_scheduler,
)

# Declared so the side-effecting imports above read as deliberate rather than as six things ruff
# would like to delete. F401 is exactly right about them being unreferenced — the REGISTRATION is
# the point, and that is not something a linter can see.
__all__ = [
    "aws_test_celery_task",
    "engagement",
    "run_automation",
    "run_avatar",
    "run_content_plan",
    "run_scheduler",
]
