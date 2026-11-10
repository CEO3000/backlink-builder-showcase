import json
from pathlib import Path

import pytest
import yaml

from src.backlinks import content as C
from src.backlinks.llm import LLMError

ROOT = Path(__file__).resolve().parent.parent
TOPIC_URL = "https://www.hypnotreatment.uk/treatments/insomnia"


def topic(cfg):
    return next(t for t in cfg["topics"] if t["key"] == "screens-and-sleep")


def body(link_text="hypnotherapy for insomnia", url=TOPIC_URL, n=48, extra="", link_in_first=False):
    """A valid ~600 word article: unique sentences, 4 subheadings, exactly one link in the middle."""
    paras = []
    for i in range(n // 4):
        sentences = " ".join(f"Evening habit number {i * 4 + j} suggests a calm, specific change that readers can try tonight at home." for j in range(4))
        if i == (0 if link_in_first else n // 8):
            sentences += f" Some people also read about [{link_text}]({url}) when their sleep stays poor."
        paras.append(sentences)
        if i % 3 == 0:
            paras.insert(len(paras) - 1, f"## Part {i}")
    return "\n\n".join(paras) + extra


def article(cfg, **kw):
    return {"title": "Why your brain will not switch off at night", "description": "A practical guide to winding down after a long day at a screen.",
            "body_markdown": body(**kw)}


def checks(cfg, art, history=()):
    return C.local_checks(art, topic(cfg), "hypnotherapy for insomnia", cfg["banned_claims"], list(history))


def test_a_good_article_passes_every_local_check(cfg):
    assert checks(cfg, article(cfg)) == []


def test_exactly_one_link_is_required_and_it_must_be_the_right_one(cfg):
    two = article(cfg, extra="\n\nSee [more](https://www.hypnotreatment.uk/blog).")
    assert any("2 links" in p for p in checks(cfg, two))
    none = article(cfg); none["body_markdown"] = none["body_markdown"].replace(f"[hypnotherapy for insomnia]({TOPIC_URL})", "hypnotherapy for insomnia")
    assert any("0 links" in p for p in checks(cfg, none))
    assert any("must point to" in p for p in checks(cfg, article(cfg, url="https://www.hypnotreatment.uk/treatments/phobia")))
    assert any("exactly 'hypnotherapy for insomnia'" in p for p in checks(cfg, article(cfg, link_text="click here")))


def test_bare_urls_and_first_paragraph_links_are_refused(cfg):
    assert any("bare web address" in p for p in checks(cfg, article(cfg, extra="\n\nVisit https://spam.example.com now.")))
    assert any("first paragraph" in p for p in checks(cfg, article(cfg, link_in_first=True)))


@pytest.mark.parametrize("phrase", ["This will cure insomnia.", "Results are guaranteed.", "It is clinically proven.", "100% effective",
                                    "You can stop taking your medication.", "A miracle method."])
def test_unsupported_medical_claims_are_refused(cfg, phrase):
    assert any("banned claim" in p for p in checks(cfg, article(cfg, extra=f"\n\n{phrase}")))


def test_innocent_words_that_contain_banned_ones_are_allowed(cfg):
    assert checks(cfg, article(cfg, extra="\n\nPeople feel more secure and have an accurate picture of their sleep.")) == []


def test_length_title_description_placeholder_and_readability_rules(cfg):
    short = article(cfg, n=8)
    assert any("words" in p for p in checks(cfg, short))
    art = article(cfg); art["title"] = "Hi"
    assert any("title" in p for p in checks(cfg, art))
    art = article(cfg); art["description"] = "x" * 200
    assert any("description" in p for p in checks(cfg, art))
    art = article(cfg, extra="\n\nSigned [Your Name]")
    assert any("placeholder" in p for p in checks(cfg, art))
    long_sentence = "word " * 60 + "."
    art = article(cfg); art["body_markdown"] = art["body_markdown"].replace("Evening habit", long_sentence + " Evening habit")
    assert any("too long" in p for p in checks(cfg, art))


def test_a_near_copy_of_an_earlier_article_is_refused(cfg):
    first = article(cfg)
    assert any("repeats wording" in p for p in checks(cfg, article(cfg), history=[first["body_markdown"]]))
    unrelated = " ".join(f"Completely different sentence about gardening number {i} in a quiet allotment." for i in range(80))
    assert checks(cfg, article(cfg), history=[unrelated]) == []


def test_parse_article_and_dashes():
    raw = "TITLE: A calm — clear title\nDESCRIPTION: Short summary here, in one sentence.\nBODY:\nFirst para – with a dash.\n\n## H"
    a = C.parse_article(raw)
    assert "—" not in a["title"] + a["description"] + a["body_markdown"] and "–" not in a["body_markdown"]
    with pytest.raises(C.ContentError) as e:
        C.parse_article("just some text")
    assert e.value.category == "format"


def test_anchor_choice_rotates_and_prefers_unused(cfg):
    t = topic(cfg)
    first = C.choose_anchor(t, [])
    second = C.choose_anchor(t, [first])
    third = C.choose_anchor(t, [first, second])
    assert len({first, second, third}) == 3
    assert C.choose_anchor(t, [first, second, third]) == first


def test_review_parsing_is_conservative():
    assert C.parse_review('{"ok": true, "issues": []}') == (True, [])
    assert C.parse_review('```json\n{"ok": false, "issues": ["invented a price"]}\n```') == (False, ["invented a price"])
    ok, issues = C.parse_review("I think it is fine")
    assert ok is False and issues


class ScriptedLLM:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def generate(self, prompt):
        self.prompts.append(prompt)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, "gemini-test"


def raw(cfg, **kw):
    a = article(cfg, **kw)
    return f"TITLE: {a['title']}\nDESCRIPTION: {a['description']}\nBODY:\n{a['body_markdown']}"


OK_REVIEW = json.dumps({"ok": True, "issues": []})


def test_build_article_success_adds_disclosure_and_metadata(cfg):
    llm = ScriptedLLM([raw(cfg), OK_REVIEW])
    art = C.build_article(topic(cfg), cfg, llm, [], [])
    assert art["anchor"] == "hypnotherapy for insomnia" and art["tags"] == topic(cfg)["tags"] and len(art["version"]) == 12
    assert "Disclosure: I write on behalf of [Hypnotreatment](https://www.hypnotreatment.uk/)" in art["body_markdown"]
    assert "not medical advice" in art["body_markdown"]
    assert len(llm.prompts) == 2 and "ONE link" in llm.prompts[0]


def test_failed_draft_is_rewritten_once_with_the_reasons(cfg):
    bad = raw(cfg, extra="\n\nThis is a miracle cure.")
    llm = ScriptedLLM([bad, raw(cfg), OK_REVIEW])
    art = C.build_article(topic(cfg), cfg, llm, [], [])
    assert art["title"] and "rejected for these reasons" in llm.prompts[1] and "banned claim" in llm.prompts[1]


def test_two_bad_drafts_raise_a_quality_error(cfg):
    bad = raw(cfg, extra="\n\nThis is a miracle cure.")
    with pytest.raises(C.ContentError) as e:
        C.build_article(topic(cfg), cfg, ScriptedLLM([bad, bad]), [], [])
    assert e.value.category == "quality"


def test_fact_check_failure_blocks_the_article(cfg):
    review = json.dumps({"ok": False, "issues": ["invents a price"]})
    with pytest.raises(C.ContentError) as e:
        C.build_article(topic(cfg), cfg, ScriptedLLM([raw(cfg), review, raw(cfg), review]), [], [])
    assert e.value.category == "factcheck" and "invents a price" in str(e.value)


def test_llm_errors_become_content_errors_with_their_category(cfg):
    with pytest.raises(C.ContentError) as e:
        C.build_article(topic(cfg), cfg, ScriptedLLM([LLMError("quota", "out of quota", True)]), [], [])
    assert e.value.category == "quota" and e.value.retryable


def test_config_topics_are_valid(cfg):
    keys = [t["key"] for t in cfg["topics"]]
    assert len(keys) == len(set(keys)) >= 6
    hosts = {h for h in cfg["verification"]["canonical_hosts"]}
    for t in cfg["topics"]:
        assert t["target_url"].startswith("https://www.hypnotreatment.uk/"), t["key"]
        assert 1 <= len(t["tags"]) <= 4 and all(tag.isalnum() for tag in t["tags"]), t["key"]
        assert len(set(t["anchors"])) == len(t["anchors"]) >= 3
        assert t["title_hint"] and t["audience"] and t["angle"]
    assert cfg["verification"]["canonical_hosts"][-1] in hosts
    assert not any("price" in f.lower() or "£" in f for f in cfg["facts"])
