"""External systems LEM talks to: LinkedIn, the AI proxy, and the rest.

The first tenant of the layered layout the restructure is moving toward. Nothing here knows about
Celery tasks or HTTP routes — it is the edge, not the application.
"""
