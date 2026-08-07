"""Source-root marker — `src` is a layout directory, not an import path.

Poetry packages this tree as `cqc_lem` alone (`pyproject.toml`: `include = "cqc_lem", from = "src"`)
and pytest puts `src` itself on `sys.path` (`pythonpath = ["src"]`), so every import in this repo
starts at `cqc_lem.*` and nothing imports `src.*`. Put nothing here: code in this file ships in no
distribution and would only be importable in a checkout.
"""
