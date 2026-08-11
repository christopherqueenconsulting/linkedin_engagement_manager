"""The ONE two-pass DOM scan behind a sort-control `sdui_selector_evidence` capture (#1117, #1270).

Two surfaces lose their sort control to the same kind of drift — the comment sweep's
'Most relevant / Most recent' switch (`app/engagement/posting.py`) and the home feed's
'Sort by' control (`app/engagement/feed.py`) — and both need the same product when a locator chain
resolves nothing: a bounded description of what the page rendered INSTEAD, shipped as an event so
the next locator iteration is written against production evidence rather than guesses.

The scan is parameterised by the two things that actually differ between those surfaces — which
selectors find the first CONTENT item, and which container holds the prose that must not be allowed
to match — because everything else about the problem is identical.

The invariants that travel with the scan:

* **Read-only, and never costs the reading it rode in on.** It describes elements; it clicks,
  navigates and writes nothing, and `scan_sort_control_candidates` swallows its own failure.
* **TWO passes, keyword then header.** A keyword pass alone cannot see the drift it exists to
  describe: a rotated label matches no sort word, which is precisely the shape that left #818 with
  no evidence for a month. When the keyword pass finds NOTHING, the second pass describes the
  short-labeled interactive controls rendered ABOVE the first content item, which is where the
  control lives whatever it now calls itself. `reason` says which pass produced a row, and
  'unanchored' marks a header pass that could not find the content to measure "above" against, so a
  reader can tell a near-miss from a shot in the dark.
* **Both passes ignore an element whose OWN rendered text is long.** A container div inherits every
  descendant's text, so matching on it describes the page rather than a control: it would fill the
  cap with ancestors, leave the header pass permanently unreached, and ship other people's post or
  comment text to analytics.
* **Anything holding the content — inside it or wrapped around it — may match on its LABEL only.**
  A post body and a comment body are prose, and one 'sort of agree' would fill the cap with
  someone's writing and starve the header pass. A wrapper div inherits that same text, so the rule
  has to run both ways or a surface whose `prose_container` is the text node itself (the feed) is
  guarded in name only.
"""

# Bounded on purpose: the event carries the sample, and eight rows is enough to re-ground a locator.
# `observability.track_selector_evidence` caps again on its own side — this cap keeps the scan cheap.
SORT_CANDIDATE_SCAN_CAP = 8

# An element whose own rendered text is longer than this is a container, not a control.
SORT_CONTROL_OWN_TEXT_MAX = 40


def build_sort_control_scan_js(*, item_selectors: list[str], prose_container: str) -> str:
    """The scan JS for one surface, given how to find its content.

    `item_selectors` are tried in order to anchor "the first content item" — the header pass keeps
    only controls that precede it, which is what makes the pass describe a header strip rather than
    the whole page. They must be LIVE-GROUNDED selectors: an invented anchor leaves the scan
    unanchored on every real page, which the `reason` field then reports honestly but uselessly.

    `prose_container` is the element whose descendants' visible text is user content. An element
    that is inside it OR contains one is matched on its LABEL only. Containment is half the rule,
    not a refinement of it: the comment surface names a LIST that wraps every body, so `closest`
    alone covers it, but the feed names the post text box ITSELF — its wrapper divs are ANCESTORS,
    they inherit exactly that post's text, and on `closest` alone a card reading 'Top 3 lessons'
    ships several rows of somebody's post to analytics and can fill the cap before the header pass
    (the only pass that can see a rotated label) ever runs.
    """
    first_expr = "||".join(f'document.querySelector("{sel}")' for sel in item_selectors)
    return (
        "const root=document.querySelector('main')||document.body;"
        f"const first={first_expr};"
        f"const CAP={SORT_CANDIDATE_SCAN_CAP};const TEXT_MAX={SORT_CONTROL_OWN_TEXT_MAX};"
        "const out=[];const seen=new Set();"
        "const own=el=>(el.innerText||'').replace(/\\s+/g,' ').trim();"
        "const push=(el,reason)=>{"
        "  if(out.length>=CAP||seen.has(el)) return;"
        "  seen.add(el);"
        "  const aria=(el.getAttribute('aria-label')||'');"
        "  out.push({"
        "    tag:el.tagName.toLowerCase(),"
        "    data_testid:el.getAttribute('data-testid')||'',"
        "    aria_label:aria.slice(0,120),"
        "    role:el.getAttribute('role')||'',"
        "    text:own(el).slice(0,80),"
        "    has_popup:el.getAttribute('aria-haspopup')||'',"
        "    classes:(el.getAttribute('class')||'').split(/\\s+/).filter(c=>c.length>3).slice(0,6).join(' '),"
        "    reason:reason"
        "  });"
        "};"
        "const KW=/sort|most relevant|most recent|\\btop\\b|\\bnewest\\b/;"
        "for(const el of root.querySelectorAll('button,[role=\"button\"],select,[aria-haspopup],div')){"
        "  const text=own(el);"
        "  if(text.length>TEXT_MAX) continue;"
        "  const label=((el.getAttribute('aria-label')||'')+' '+(el.getAttribute('data-testid')||'')"
        "    +' '+(el.getAttribute('class')||'')).toLowerCase();"
        f"  const inList=(el.closest&&el.closest(\"{prose_container}\"))"
        f"||(el.querySelector&&el.querySelector(\"{prose_container}\"));"
        "  if(KW.test(inList?label:label+' '+text.toLowerCase())) push(el,'keyword');"
        "  if(out.length>=CAP) break;"
        "}"
        "if(!out.length){"
        "  for(const el of root.querySelectorAll('button,[role=\"button\"],select,[aria-haspopup]')){"
        "    if(first){const pos=first.compareDocumentPosition(el);"
        "      if(!(pos&Node.DOCUMENT_POSITION_PRECEDING)||(pos&Node.DOCUMENT_POSITION_CONTAINS)) continue;}"
        "    if(own(el).length>TEXT_MAX) continue;"
        "    push(el,first?'header':'unanchored');"
        "    if(out.length>=CAP) break;"
        "  }"
        "}"
        "return out;")


def scan_sort_control_candidates(driver, scan_js: str) -> list[dict]:
    """Run one surface's scan and return its structured descriptors, or [] when the read failed.

    [] is also what a page with nothing describable returns, and the two are deliberately not told
    apart here: both mean "the capture is blind", and the caller emits that reading rather than
    suppressing it — a surface that looks un-drifted because its evidence was dropped is the failure
    mode this whole mechanism exists to end.
    """
    try:
        result = driver.execute_script(scan_js)
        return [dict(r) for r in (result or []) if isinstance(r, dict)]
    except Exception:
        return []
