"""The Celery half of LEM: the app itself (`my_celery`), its config, and the task modules.

The imports below are load-bearing, not conveniences. `my_celery` calls
`autodiscover_tasks(['cqc_lem'])`, which looks for a `cqc_lem.tasks` module that does not exist — so
importing this package is what actually pulls in the modules carrying `@app.task`, and those modules
are exactly the ones that define one. Dropping one as an "unused import" unregisters its tasks: the
worker starts clean and every message on those queues is rejected as unknown.

`engagement` is listed explicitly for that reason: every engagement task is registered by
`cqc_lem.app.engagement.__init__`, which names all five modules. `run_automation` was removed from
this list in #1206 along with the module itself — it re-exported those tasks and defined none, so
the registration was never its doing. The tasks still ANSWER to `cqc_lem.app.run_automation.<fn>`,
because each one pins that name in its own decorator; that spelling is a wire identifier now, not a
module path, and nothing imports it.
"""
from . import (
    aws_test_celery_task,
    engagement,
    run_avatar,
    run_content_plan,
    run_scheduler,
)

# Declared so the side-effecting imports above read as deliberate rather than as five things ruff
# would like to delete. F401 is exactly right about them being unreferenced — the REGISTRATION is
# the point, and that is not something a linter can see.
__all__ = [
    "aws_test_celery_task",
    "engagement",
    "run_avatar",
    "run_content_plan",
    "run_scheduler",
]
