from inspect import Parameter

from celery_once import QueueOnce as _CeleryQueueOnce


class QueueOnce(_CeleryQueueOnce):
    """celery-once, but a task's OWN parameter defaults count toward its dedup key.

    Upstream builds the key from `Signature.bind(...).arguments`, which never applies defaults, then
    indexes that mapping with every name in `once={'keys': [...]}`. So enqueuing a task without
    passing a defaulted key parameter raised `KeyError: '<key>'` inside `apply_async` — before the
    message was ever sent — which surfaced as a 500 on the caller (issue #989: `sweep_slot` on the
    inbound reply-notification webhook). Filling the default makes an omitted `sweep_slot=0` and an
    explicit `sweep_slot=0` the SAME lock, which is what the key always meant.

    Only keys named in `once['keys']` are filled, so the unrestricted case keeps upstream's key shape
    byte for byte and no existing lock changes identity.
    """

    abstract = True

    def _get_call_args(self, args, kwargs) -> dict:
        call_args = super()._get_call_args(args, kwargs)
        for key in self.once.get('keys') or ():
            if key in call_args:
                continue
            param = self._signature.parameters.get(key)
            if param is not None and param.default is not Parameter.empty:
                call_args[key] = param.default
        return call_args
