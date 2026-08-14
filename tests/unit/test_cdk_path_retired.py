"""Guards that the retired AWS CDK deploy path stays retired (#973).

The CDK tree was never used for production — prod is the VPS Docker Compose stack — and it carried
~20 TODOs that were wrong for prod, so it read as a live option while being unsafe to deploy. The
owner's call was to delete it and let git history be the reference. What this file protects is the
part a later change could silently undo: a stray `cqc_lem.aws` / `aws_cdk` import, or a CDK driver
script reappearing at the repo root, would resurrect that misleading second deploy path without
anyone deciding to.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Trees that ship or drive code. Docs may still DISCUSS the CDK path (DEPLOYMENT.md records that it
# existed), so prose is deliberately out of scope here — only importable/runnable code is.
SCANNED_TREES = ("src", "scripts", "tests")

CDK_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(?:cqc_lem\.aws|aws_cdk)\b", re.MULTILINE)


def test_cdk_tree_is_gone() -> None:
    """`src/cqc_lem/aws/` was deleted in #973 and must not come back."""
    assert not (REPO_ROOT / "src" / "cqc_lem" / "aws").exists()


def test_no_cdk_driver_scripts_at_repo_root() -> None:
    """The bootstrap/synth/deploy CDK wrappers went with the tree they `cd` into."""
    strays = sorted(p.name for p in REPO_ROOT.glob("*_aws_cdk.sh"))
    assert strays == [], f"CDK driver scripts are back: {strays}"


@pytest.mark.parametrize("tree", SCANNED_TREES)
def test_nothing_imports_the_cdk_path(tree: str) -> None:
    """No Python file may import `cqc_lem.aws` or `aws_cdk` — both are uninstalled and undeployable."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / tree).rglob("*.py")
        if CDK_IMPORT_RE.search(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == [], f"CDK imports found in {tree}/: {offenders}"


def test_cdk_toolchain_is_not_a_dependency() -> None:
    """`pyproject.toml` must not re-add the CDK packages the deleted tree needed."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declarations = [
        line
        for line in pyproject.splitlines()
        if re.match(r"^\s*(aws-cdk[\w-]*|cdk-ecr-deployment)\s*=", line)
    ]
    assert declarations == [], f"CDK dependencies are back: {declarations}"
