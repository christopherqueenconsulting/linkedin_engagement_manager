"""The Celery half of LEM: the app itself (`my_celery`), its config, and the task modules.

The imports below are load-bearing, not conveniences. `my_celery` calls
`autodiscover_tasks(['cqc_lem'])`, which looks for a `cqc_lem.tasks` module that does not exist — so
importing this package is what actually pulls in the modules carrying `@app.task`, and those five
are exactly the modules that define one. Dropping one as an "unused import" unregisters its tasks:
the worker starts clean and every message on those queues is rejected as unknown.
"""
from . import aws_test_celery_task, run_automation, run_avatar, run_content_plan, run_scheduler
