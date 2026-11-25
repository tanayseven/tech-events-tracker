import pytest
from ddgs.exceptions import DDGSException, RatelimitException

import verify_urls as vu
from tech_events.pipelines import YEAR


def hit(url, title="t"):
    return {"href": url, "title": title}


# ── URL helpers ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [
    ("https://www.nullcon.net/", "http://nullcon.net/event/goa"),
    ("https://{year}.pyconfhyd.org/", "https://pyconfhyd.org/"),
    ("https://{year}.pyconfhyd.org/", f"https://{YEAR}.pyconfhyd.org/schedule"),
    ("https://events.linuxfoundation.org/x/", "https://events.linuxfoundation.org/y"),
])
def test_same_site(a, b):
    assert vu._same_site(a, b)


@pytest.mark.parametrize("a,b", [
    ("https://in.pycon.org/{year}/", "https://us.pycon.org/2026/"),  # shared parent domain
    ("https://reactindia.io/", "https://react.dev/"),
    ("https://x.org/", ""),
])
def test_not_same_site(a, b):
    assert not vu._same_site(a, b)


def test_aggregator_detection():
    assert vu._is_aggregator("https://www.eventbrite.com/e/123")
    assert vu._is_aggregator("https://konfhub.com/nullcon-goa-2026")
    assert vu._is_aggregator("https://en.wikipedia.org/wiki/X")
    assert not vu._is_aggregator("https://nullcon.net/")


# ── Verdict logic ─────────────────────────────────────────────────────────────

def test_ok_when_host_in_top_n():
    r = vu._verdict("https://www.nullcon.net/", [
        hit("https://konfhub.com/nullcon"), hit("https://nullcon.net/event/goa-2026/"),
    ], top_n=5)
    assert r.verdict == vu.OK
    assert r.matched_url == "https://nullcon.net/event/goa-2026/"


def test_changed_when_host_beyond_top_n():
    hits = [hit(f"https://other{i}.org/") for i in range(5)] + [hit("https://nullcon.net/")]
    r = vu._verdict("https://nullcon.net/", hits, top_n=5)
    assert r.verdict == vu.CHANGED
    assert r.suggested == "https://other0.org/"


def test_changed_skips_aggregators_for_suggestion():
    r = vu._verdict("https://old.org/", [
        hit("https://www.eventbrite.com/e/1"), hit("https://new.org/", "New Conf"),
    ], top_n=5)
    assert r.verdict == vu.CHANGED
    assert r.suggested == "https://new.org/"
    assert r.candidates == [{"url": "https://new.org/", "title": "New Conf"}]


def test_changed_keeps_year_template():
    r = vu._verdict("https://{year}.oldconf.org/", [hit(f"https://newconf.dev/{YEAR}/")], top_n=5)
    assert r.verdict == vu.CHANGED
    assert r.suggested == "https://newconf.dev/{year}/"


def test_not_found_when_no_hits():
    assert vu._verdict("https://x.org/", [], top_n=5).verdict == vu.NOT_FOUND


def test_not_found_when_only_aggregators():
    r = vu._verdict("https://x.org/", [hit("https://meetup.com/x")], top_n=5)
    assert r.verdict == vu.NOT_FOUND


# ── Search wrapper / check_entry ──────────────────────────────────────────────

class FakeDDGS:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.queries = []

    def text(self, query, max_results):
        self.queries.append(query)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(vu.time, "sleep", lambda s: None)


def test_check_entry_uses_name_in_query():
    d = FakeDDGS([[hit("https://nullcon.net/")]])
    r = vu.check_entry(d, {"name": "Nullcon", "url": "https://www.nullcon.net/"}, 10, 5)
    assert r.name == "Nullcon" and r.verdict == vu.OK
    assert '"Nullcon"' in d.queries[0] and str(YEAR) in d.queries[0]


def test_search_retries_on_ratelimit():
    d = FakeDDGS([RatelimitException("slow down"), [hit("https://a.org/")]])
    assert vu.search(d, "q", 10) == [hit("https://a.org/")]


def test_search_retries_on_transient_no_results():
    d = FakeDDGS([DDGSException("No results found."), [hit("https://a.org/")]])
    assert vu.search(d, "q", 10) == [hit("https://a.org/")]


def test_check_entry_error_after_retries():
    d = FakeDDGS([RatelimitException("x")] * 3)
    r = vu.check_entry(d, {"name": "X", "url": "https://x.org/"}, 10, 5)
    assert r.verdict == vu.ERROR and r.error


# ── Ignore file / rejected suggestions ───────────────────────────────────────

def test_rejected_site_never_suggested():
    r = vu._verdict("https://old.org/", [hit("https://bad.org/x"), hit("https://good.org/")],
                    top_n=5, rejected=["https://www.bad.org/"])
    assert r.suggested == "https://good.org/"


def test_load_ignore(tmp_path):
    p = tmp_path / "ignore.yaml"
    p.write_text("skip:\n  - BSides\nreject:\n  JavaOne:\n    - https://dev.java/\n")
    skip, reject = vu._load_ignore(p)
    assert skip == {"bsides"}
    assert reject == {"javaone": ["https://dev.java/"]}


def test_load_ignore_missing_file(tmp_path):
    assert vu._load_ignore(tmp_path / "nope.yaml") == (set(), {})


# ── apply_changes / write_summary ────────────────────────────────────────────

def _res(name, current, verdict, suggested=None, candidates=()):
    return vu.Result(name=name, current_url=current, verdict=verdict,
                     suggested=suggested, candidates=list(candidates))


def test_apply_changes_updates_only_new_changed_urls():
    entries = [
        {"url": "https://old.org/", "name": "A"},
        {"url": "https://b.org/", "name": "B"},
        {"url": "https://shared.org/", "name": "C"},
        {"url": "https://d.org/", "name": "D"},
    ]
    results = [
        _res("A", "https://old.org/", vu.CHANGED, "https://new.org/"),
        _res("B", "https://b.org/", vu.OK),
        _res("D", "https://d.org/", vu.CHANGED, "https://www.shared.org"),  # already listed
    ]
    applied = vu.apply_changes(entries, results)
    assert [r.name for r in applied] == ["A"]
    assert entries[0]["url"] == "https://new.org/"
    assert entries[3]["url"] == "https://d.org/"


def test_apply_changes_noop_when_nothing_changed():
    entries = [{"url": "https://a.org/", "name": "A"}]
    assert vu.apply_changes(entries, [_res("A", "https://a.org/", vu.OK)]) == []
    assert entries[0]["url"] == "https://a.org/"


def test_write_summary(tmp_path):
    applied = [_res("A", "https://old.org/", vu.CHANGED, "https://new.org/",
                    [{"url": "https://new.org/", "title": "A"},
                     {"url": "https://alt.org/", "title": "Alt"}])]
    errs = [_res("E", "https://e.org/", vu.ERROR)]
    out = tmp_path / "body.md"
    vu.write_summary(out, applied, applied + errs)
    body = out.read_text()
    assert "| A | https://old.org/ | https://new.org/ | [Alt](https://alt.org/) |" in body
    assert "- E: ERROR" in body


# ── main(): circuit breaker ──────────────────────────────────────────────────

def test_main_stops_after_consecutive_errors(tmp_path, monkeypatch):
    names = [f"E{i}" for i in range(8)]
    (tmp_path / "events_list.yaml").write_text(
        "".join(f"- url: https://{n.lower()}.org/\n  name: {n}\n" for n in names))
    monkeypatch.chdir(tmp_path)
    calls = []

    class AlwaysFail:
        def text(self, query, max_results):
            calls.append(query)
            raise DDGSException("blocked")

    monkeypatch.setattr(vu, "DDGS", AlwaysFail)
    with pytest.raises(SystemExit, match="Every search failed"):
        vu.main(["--delay", "0"])
    # 5 entries x 3 attempts each, then the breaker trips; E5..E7 never searched.
    assert len(calls) == vu.MAX_CONSECUTIVE_ERRORS * 3


def test_summary_escapes_pipes_in_titles(tmp_path):
    applied = [_res("A", "https://old.org/", vu.CHANGED, "https://new.org/",
                    [{"url": "https://new.org/", "title": "A"},
                     {"url": "https://alt.org/", "title": "Alt | 2026"}])]
    out = tmp_path / "body.md"
    vu.write_summary(out, applied, applied)
    assert "[Alt \\| 2026](https://alt.org/)" in out.read_text()


def test_search_engine_ad_links_are_filtered():
    assert vu._is_aggregator("https://www.bing.com/aclick?ld=abc")


# ── Name resemblance (cases from real runs) ──────────────────────────────────

@pytest.mark.parametrize("name,url", [
    ("ReactFoo", "https://reactfoo.in/"),
    ("Supercomputing Conference", "https://supercomputing.org/"),
    ("Linux Plumbers Conference", "https://lpc.events/"),          # acronym
    ("Bangalore Tech Summit", "https://www.bengalurutechsummit.com/"),  # 2 words
    ("MachineCon", "https://machinecon.aimmediahouse.com/"),
])
def test_resembles(name, url):
    assert vu._resembles(name, url)


@pytest.mark.parametrize("name,url", [
    ("ReactFoo", "https://react.dev/community/conferences"),
    ("MLOps World", "https://www.webmobi.com/discovery/events/mlops-world-2026"),
    ("MLOps World", "https://www.aiexpertmagazine.com/mlops-world-genai-summit-2026/"),
    ("DockerCon", "https://www.docker.com/blog/lets-dockercon/"),
    ("ng-conf", "https://angularbelgrade.org/"),
    ("Techfest IIT Bombay", "https://iitianvibes.com/blog/techfest-2026/"),
])
def test_does_not_resemble(name, url):
    assert not vu._resembles(name, url)


def test_name_like_candidate_preferred_over_higher_ranked():
    r = vu._verdict("https://hasgeek.com/reactfoo/", [
        hit("https://react.dev/community/conferences"), hit("https://reactfoo.in/"),
    ], top_n=5, name="ReactFoo")
    assert (r.verdict, r.suggested) == (vu.CHANGED, "https://reactfoo.in/")


def test_unsure_when_no_candidate_resembles_name():
    r = vu._verdict("https://mlopsworld.com", [
        hit("https://www.webmobi.com/discovery/events/mlops-world-2026"),
        hit("https://www.aiexpertmagazine.com/mlops-world/"),
    ], top_n=5, name="MLOps World")
    assert r.verdict == vu.UNSURE and r.suggested is None


def test_unsure_is_not_applied():
    entries = [{"url": "https://a.org/", "name": "A"}]
    res = vu.Result(name="A", current_url="https://a.org/", verdict=vu.UNSURE)
    assert vu.apply_changes(entries, [res]) == []
