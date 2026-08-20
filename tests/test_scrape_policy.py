"""Tests for policy-portal discovery + reconnaissance."""

from __future__ import annotations

import json
from pathlib import Path

from herald.scrape.core import Fetcher
from herald.scrape.policy import (
    discover_portal,
    find_portal_links,
    load_portal_targets,
    policy_page_links,
    probe_portal,
    render_report,
    vendor_of,
)


def _fast_fetcher() -> Fetcher:
    return Fetcher(min_request_interval=0.0, retry_base_delay=0.0, respect_robots=False)


def test_vendor_of():
    assert vendor_of("https://boardpolicyonline.com/?b=peekskill") == "boardpolicyonline"
    assert vendor_of("https://v3.boardpolicyonline.com/b/peekskill") == "boardpolicyonline"
    assert vendor_of("https://policy.microscribepub.com/cgi-bin/om_isapi.dll?x=1") == "microscribe"
    assert vendor_of("https://www.portchesterschools.org/board/policies") is None


def test_find_portal_links_handles_hrefs_entities_and_bare_text():
    # Port Chester's real page shape: the manual link plus section deep links,
    # HTML-escaped, alongside the loose Finalsite PDFs we already collect.
    html = """
    <a href="https://boardpolicyonline.com/?b=port_chester_rye">Policy Manual</a>
    <a href="https://boardpolicyonline.com/?b=port_chester_rye&amp;s=37623">Series 1000</a>
    <a href="/fs/resource-manager/view/abc">A loose policy PDF</a>
    <script>var p = "https://policy.microscribepub.com/cgi-bin/om_isapi.dll?infobase=x.nfo";</script>
    """
    got = find_portal_links(html, base_url="https://www.portchesterschools.org/board/policies")
    assert "https://boardpolicyonline.com/?b=port_chester_rye" in got
    assert "https://boardpolicyonline.com/?b=port_chester_rye&s=37623" in got   # unescaped
    assert any("microscribepub" in u for u in got)                              # found in JS
    assert not any("resource-manager" in u for u in got)


def test_policy_page_links_stay_on_host_and_mention_policy():
    html = """
    <a href="/board/policies">Board Policies</a>
    <a href="/board/agendas">Agendas</a>
    <a href="/district/policy-manual">Manual</a>
    <a href="https://elsewhere.example/policies">Offsite</a>
    """
    got = policy_page_links(html, base_url="https://d.test/board")
    assert "https://d.test/board/policies" in got
    assert "https://d.test/district/policy-manual" in got
    assert not any("agendas" in u for u in got)
    assert not any("elsewhere" in u for u in got)


def test_discover_portal_follows_a_policy_page_then_stops(httpx_mock):
    # Landing page links a policy page; the policy page carries the portal.
    httpx_mock.add_response(
        url="https://d.test/",
        headers={"Content-Type": "text/html"},
        text='<a href="/board/policies">Board Policies</a>',
    )
    httpx_mock.add_response(
        url="https://d.test/board/policies",
        headers={"Content-Type": "text/html"},
        text='<a href="https://boardpolicyonline.com/?b=d_test">Manual</a>',
    )
    with _fast_fetcher() as f:
        got = discover_portal(f, "https://d.test/")
    assert got == ["https://boardpolicyonline.com/?b=d_test"]


def test_probe_portal_captures_structure_and_saves_body(httpx_mock, tmp_path):
    # The Folio viewer is a frameset and hides its endpoints in scripts —
    # exactly the details the parser will need.
    httpx_mock.add_response(
        url="https://policy.microscribepub.com/x",
        headers={"Content-Type": "text/html"},
        text="""<html><head><title>Ossining Policy Manual</title>
                <script src="/js/folio.js"></script></head>
                <frameset><frame src="/cgi-bin/om_isapi.dll?toc=1"></frameset>
                <a href="/policy/5100">5100 Attendance</a></html>""",
    )
    saved = tmp_path / "oss.html"
    with _fast_fetcher() as f:
        p = probe_portal(f, "ossining", "https://policy.microscribepub.com/x", save_to=saved)

    assert p.status == 200 and p.vendor == "microscribe"
    assert p.title == "Ossining Policy Manual"
    assert any("om_isapi.dll?toc=1" in u for u in p.frames)
    assert any("folio.js" in u for u in p.scripts)
    assert saved.exists() and "Policy Manual" in saved.read_text(encoding="utf-8")
    assert render_report([p], {}).startswith("# Policy portal probe")


def test_probe_portal_records_errors_without_raising(httpx_mock):
    httpx_mock.add_exception(Exception("connection reset"), url="https://dead.test/x")
    with _fast_fetcher() as f:
        p = probe_portal(f, "greenburgh-central", "https://dead.test/x")
    assert p.status is None and "connection reset" in p.error


def test_policy_portals_targets_file_is_valid():
    districts = load_portal_targets("data/targets/policy_portals.json")
    expected = {
        "port-chester-rye", "ossining", "peekskill", "tarrytowns",
        "elmsford", "mount-vernon", "greenburgh-central", "white-plains",
    }
    assert set(districts) == expected
    for slug, cfg in districts.items():
        # every district must give the probe somewhere to start
        assert cfg.get("portal") or cfg.get("discover_from"), slug
        if cfg.get("portal"):
            assert vendor_of(cfg["portal"]) == cfg["vendor"], slug
    # the vendor block documents both URL templates
    raw = json.loads(Path("data/targets/policy_portals.json").read_text(encoding="utf-8"))
    assert set(raw["vendors"]) == {"boardpolicyonline", "microscribe"}
