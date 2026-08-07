"""The feedback -> auto-work loop, one stage per module (docs/launch-and-marketing-plan.md §B).

    capture (#496, api/main) ─► classifier.py (#497) ─► issue_service.py (#498) ─► shipped.py (#502)
                                        └────────────► faq_service.py (#507)

The invariant every module in here keeps: the raw feedback body and the reporter's identity never
leave this package. GitHub sees only the classifier's factual summary, the public FAQ publishes only
a PII-redacted question, and the shipped-fix stage only READS merged PRs. Each stage also fails SAFE
rather than open or closed — a GitHub, LLM or mail outage delays a feedback row to the next pass, it
never drops it and never publishes something unreviewed to get unstuck.
"""
