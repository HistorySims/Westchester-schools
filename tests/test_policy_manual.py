"""Tests for splitting a BoardPolicyOnline whole-manual export.

The browser half (``fetch_manual_export``) is exercised against the live
portal from CI, not here; what is worth pinning in a unit test is the parse —
the export's real shape, taken from the Port Chester-Rye capture.
"""

from __future__ import annotations

from herald.scrape.policy_manual import (
    JQUERY_ASSET,
    BrowserOptions,
    portal_base,
    split_export,
)

# Trimmed from the real 2.4 MB export: a divider with no body, a policy, and
# a policy whose number carries a decimal — plus the boilerplate footer that
# repeats under every section and must not end up in the text.
EXPORT = """
<html><head><title>Selected policies for Port Chester-Rye</title></head><body>
<div class="ck-content export-section page-break bookmark-level-1"
     data-bookmark-title="5000 STUDENT POLICIES" id="37840">
  <div><p class="section-title">5000 STUDENT POLICIES</p></div>
  <div class="section-footer">Port Chester-Rye Union Free School District</div>
  <div class="page-break"></div>
</div>
<div class="ck-content export-section page-break bookmark-level-1"
     data-bookmark-title="5100 ATTENDANCE" id="37843">
  <div><p class="section-title">5100 ATTENDANCE</p></div>
  <p>At the high school level, any student with more than nine unexcused ATEDs
     for one-half year or 18 unexcused ATEDs for a full year will not receive
     credit for that course.</p>
  <p>Adoption date: April 18, 2023</p>
  <div class="section-footer">Port Chester-Rye Union Free School District</div>
</div>
<div class="ck-content export-section page-break bookmark-level-1"
     data-bookmark-title="0110.2 SEXUAL HARASSMENT IN THE WORKPLACE" id="37628">
  <div><p class="section-title">0110.2 SEXUAL HARASSMENT IN THE WORKPLACE</p></div>
  <p>The district is committed to maintaining a workplace free from sexual
     harassment, and this policy applies to all employees, applicants for
     employment, interns, and non-employees in the workplace.</p>
  <div class="section-footer">Port Chester-Rye Union Free School District</div>
</div>
</body></html>
"""

PORTAL = "https://v3.boardpolicyonline.com/b/port_chester_rye"


def test_portal_base_survives_deep_links():
    assert portal_base(PORTAL) == PORTAL
    assert portal_base(f"{PORTAL}/s/37843") == PORTAL
    assert portal_base(f"{PORTAL}/") == PORTAL


def test_split_export_yields_one_doc_per_section_in_order():
    docs = split_export(EXPORT, portal_url=PORTAL)
    assert [d.section_id for d in docs] == ["37840", "37843", "37628"]
    assert [d.number for d in docs] == ["5000", "5100", "0110.2"]
    assert docs[1].title == "5100 ATTENDANCE"


def test_dividers_are_kept_but_flagged_not_substantive():
    # A section with a title and no body is a divider, not a policy. It is
    # returned (never silently dropped) but callers can tell the difference.
    docs = split_export(EXPORT, portal_url=PORTAL)
    assert docs[0].is_substantive is False
    assert docs[1].is_substantive is True
    assert docs[2].is_substantive is True


def test_body_text_drops_title_and_repeating_footer():
    doc = split_export(EXPORT, portal_url=PORTAL)[1]
    assert "18 unexcused ATEDs" in doc.text
    assert "Adoption date: April 18, 2023" in doc.text
    assert "5100 ATTENDANCE" not in doc.text            # title stripped
    assert "Union Free School District" not in doc.text  # footer stripped
    assert "<p>" in doc.html                             # html kept verbatim


def test_every_policy_gets_the_portals_own_deep_link():
    # Provenance is the point: an answer about 5100 must be able to cite the
    # district's live manual, not a filename in our raw store.
    docs = split_export(EXPORT, portal_url=f"{PORTAL}/s/37620")
    assert docs[1].source_url == f"{PORTAL}/s/37843"


def test_number_suffix_must_be_attached_not_just_the_next_word():
    # Real titles from the Port Chester-Rye manual. A regulation/exhibit
    # shares its policy's number ("0115-R" implements "0115"), so the number
    # is stored without the suffix — otherwise a question about 0115 misses
    # the regulation that actually answers it. But the suffix has to be
    # *attached*: "0115 STUDENT HARASSMENT..." must not parse as suffix
    # "STUDENT".
    titles = [
        "0115 STUDENT HARASSMENT AND BULLYING PREVENTION AND INTERVENTION",
        "0115-R STUDENT HARASSMENT AND BULLYING PREVENTION REGULATION",
        "0110.2-E SEXUAL HARASSMENT IN THE WORKPLACE EXHIBIT",
        "6170.R SCHOOL-WIDE PRE-REFERRAL APPROACHES AND INTERVENTIONS",
        "5405-R(1) WELLNESS REGULATION - SCHOOL-BASED IMPLEMENTATION",
        "8123.1-E.4 STATEMENT OF EMPLOYEE'S DECISION TO RECEIVE HEPATITIS B",
        "7331/7332/7333/7340 PLANS, SPECIFICATIONS AND COST ESTIMATES",
        "Student Dress Code",
    ]
    html = "".join(
        f'<div class="export-section" data-bookmark-title="{t}" id="{i}">'
        f'<p class="section-title">{t}</p><p>body text that is long enough to '
        f"count as a real policy body for the substantive check.</p></div>"
        for i, t in enumerate(titles)
    )
    docs = split_export(html, portal_url=PORTAL)
    assert [d.number for d in docs] == [
        "0115", "0115", "0110.2", "6170", "5405", "8123.1", "7331/7332/7333/7340", None,
    ]
    assert [d.suffix for d in docs] == ["", "R", "E", "R", "R(1)", "E.4", "", ""]
    assert [d.full_number for d in docs[:6]] == [
        "0115", "0115-R", "0110.2-E", "6170-R", "5405-R(1)", "8123.1-E.4",
    ]


def test_split_export_is_safe_on_empty_input():
    assert split_export("", portal_url=PORTAL) == []


def test_jquery_is_vendored_so_the_scrape_does_not_need_a_cdn():
    # The portal hard-depends on jQuery; without it the Blazor circuit throws
    # and the page renders only an error. Serving our own copy removes a
    # third-party CDN from the critical path.
    assert JQUERY_ASSET.exists()
    assert "jQuery v3.7.1" in JQUERY_ASSET.read_text(encoding="utf-8")[:200]


def test_browser_options_only_adds_the_tls_flag_when_asked():
    assert BrowserOptions().launch_args() == []
    assert "--ssl-version-max=tls1.2" in BrowserOptions(tls12=True).launch_args()
