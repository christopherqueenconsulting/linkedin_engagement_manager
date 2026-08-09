"""One-off: re-dispatch the 24 profile-viewer backfill invites that failed under the rail bug
(2026-08-03), now that v0.129.1's URL-route fix is deployed. Bare invites (message=None);
QueueOnce + the 1/m task rate limit pace them.

Run:  sudo docker exec -i celery_worker python - < scripts/tmp_redispatch_invites.py
"""
from cqc_lem.app.engagement.invites import invite_to_connect

TARGETS = [
    "https://www.linkedin.com/in/trusha-parmar-7b05771bb/",
    "https://www.linkedin.com/in/mariedaras/",
    "https://www.linkedin.com/in/kirill-pokidov-9a7a224b/en/",
    "https://www.linkedin.com/in/renato-camacho/",
    "https://www.linkedin.com/in/ani-agi-asi/",
    "https://www.linkedin.com/in/nutan-kumar-naik-63878b242/",
    "https://www.linkedin.com/in/dariusrenauld-asmr-386665341/",
    "https://www.linkedin.com/in/brian-gribbon-0ab882198/",
    "https://www.linkedin.com/in/2subh/",
    "https://www.linkedin.com/in/rolfbostrom/",
    "https://www.linkedin.com/in/erika-hibler/",
    "https://www.linkedin.com/in/mario-morin-4587a8222/",
    "https://www.linkedin.com/in/dejenet/",
    "https://www.linkedin.com/in/best-seo-expert-in-malaysia/",
    "https://www.linkedin.com/in/talex-maxim-87abbb60/",
    "https://www.linkedin.com/in/gaurav-sharma-04aa7b26a/",
    "https://www.linkedin.com/in/jerry-fisher-0333aa57/",
    "https://www.linkedin.com/in/samasolomon/",
    "https://www.linkedin.com/in/lcdrdevinthomas/",
    "https://www.linkedin.com/in/navneetkumar93/",
    "https://www.linkedin.com/in/patcii/",
    "https://www.linkedin.com/in/upender-k-279b9aba/",
    "https://www.linkedin.com/in/ernestobejarano/",
    "https://www.linkedin.com/in/harish-g-harry-83872360/",
]

for url in TARGETS:
    invite_to_connect.apply_async(kwargs={"user_id": 1, "profile_url": url, "message": None})

print(f"dispatched {len(TARGETS)} invite_to_connect tasks")
