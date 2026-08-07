"""Package root — the ONE place the on-disk asset root is resolved.

`assets_dir` is that root. Every stored image, carousel slide, video and avatar sample is written
under it and persisted in the DB as a path RELATIVE to it, which is what lets `/api/assets` serve it
by name and what the containment checks (`post_image.py`, `newsletter_cover.py`, `utils.py`) measure
an incoming path against before touching the filesystem. It is derived from THIS file's location, so
it follows the package into the image instead of depending on a process's working directory — which
is why it must stay resolved here and never be re-derived at a call site.
"""

import os

assets_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets')
build_dir = os.path.join(os.path.dirname(os.path.abspath(__file__) + "/../../../"))
