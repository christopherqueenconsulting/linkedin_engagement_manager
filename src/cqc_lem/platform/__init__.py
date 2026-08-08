"""Infrastructure LEM runs ON: the database, Redis, settings, logging, observability.

Nothing here knows about LinkedIn, posts, or engagement — it is the machinery the domain and
application layers sit on top of. Named `platform` deliberately as a package, not a module; there
is no bare `import platform` anywhere in this tree, so stdlib is never shadowed.
"""
