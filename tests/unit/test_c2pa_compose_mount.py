"""Every service that runs ``c2pa_helper`` sees the cert dir read-only (#2794)."""
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
MOUNT = "./secrets/c2pa:/run/secrets/c2pa:ro"

# Runtime callers of c2pa_helper and where they run: image_gen, carousel_creator, run_content_plan,
# generate_variants, ai_helper and avatar/samples execute in Celery workers (they are tasks or called
# from tasks); the API colors are covered too in case a request-path call renders an image.
SIGNING_SERVICES_PROD = (
    "web_api_blue", "web_api_green", "celery_worker", "celery_worker_selenium",
    "celery_worker_selenium_prepost", "celery_worker_selenium_outreach",
    "celery_worker_selenium_content",
)


class _Loader(yaml.SafeLoader):
    """Compose's ``!reset`` / ``!override`` tags: keep the value, drop the tag."""


for _tag in ("!reset", "!override"):
    _Loader.add_constructor(_tag, lambda loader, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)))


def _services(name: str) -> dict:
    return yaml.load((REPO / name).read_text(), Loader=_Loader)["services"]


@pytest.mark.parametrize("service", SIGNING_SERVICES_PROD)
def test_prod_service_mounts_cert_dir_read_only(service):
    volumes = _services("docker-compose.prod.yml")[service]["volumes"]
    assert MOUNT in volumes


def test_dev_api_mounts_it_and_workers_inherit_through_volumes_from():
    services = _services("docker-compose.yml")
    assert MOUNT in services["web_app"]["volumes"]
    for name in ("celery_worker", "celery_worker_selenium", "celery_worker_selenium_prepost",
                 "celery_worker_selenium_outreach", "celery_worker_selenium_content"):
        assert "web_app" in services[name]["volumes_from"]


def test_every_prod_celery_worker_is_covered():
    workers = {n for n in _services("docker-compose.prod.yml") if n.startswith("celery_worker")}
    assert workers == {n for n in SIGNING_SERVICES_PROD if n.startswith("celery_worker")}


def test_secrets_dir_is_gitignored():
    assert "/secrets/" in (REPO / ".gitignore").read_text().splitlines()


def test_c2pa_stays_off_by_default_in_env_examples():
    for name in (".env.example", ".env.prod.example"):
        lines = (REPO / name).read_text().splitlines()
        assert "C2PA_ENABLED=true" not in lines
