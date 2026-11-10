"""Articles must read as helpful guides, not adverts: the brand takes up little space and the approved facts are
rephrased, never pasted in. These cases come from the first real Gemini drafts, which copied the facts word for word."""
from test_content import OK_REVIEW, ScriptedLLM, article, body, raw, topic
from src.backlinks import content as C

FACT_PARAGRAPH = ("Hypnotreatment is the hypnotherapy practice of Shahin Gurbanov, a qualified hypnotherapist in London. "
                  "Shahin uses hypnotherapy with NLP and ideas from cognitive behavioural therapy (CBT). "
                  "Sessions are available in person in London and online by video, starting with a free video consultation.")
AI_PARAGRAPH = ("Hypnotreatment has an AI Hypnotherapist: it gives a personal consultation, listens to the person's answers, then writes "
                "and records a hypnotherapy session made for that person. The AI Hypnotherapist uses the techniques professional "
                "hypnotherapists use, including Ericksonian hypnosis, NLP and ideas from CBT, and follows the method of Shahin, a "
                "qualified hypnotherapist.")
OWN_WORDS = "One London practice, Hypnotreatment, offers a free first video call, which is a low-pressure way to see what a session involves."


def checks(cfg, art):
    return C.local_checks(art, topic(cfg), "hypnotherapy for insomnia", cfg["banned_claims"], [], cfg["brand_name"], cfg["facts"])


def test_a_normal_article_with_a_brief_mention_passes(cfg):
    assert checks(cfg, article(cfg, extra="\n\n" + OWN_WORDS)) == []


def test_a_long_brand_section_is_refused_as_an_advert(cfg):
    advert = article(cfg, extra="\n\n" + FACT_PARAGRAPH + "\n\n" + AI_PARAGRAPH)
    problems = checks(cfg, advert)
    assert any("reads as a helpful article, not an advert" in p for p in problems)


def test_pasting_the_approved_facts_is_refused_even_when_the_share_is_small(cfg):
    short = "Hypnotreatment is the hypnotherapy practice of Shahin Gurbanov, a qualified hypnotherapist in London."
    problems = checks(cfg, article(cfg, extra="\n\n" + short))
    assert any("copies the approved facts word for word" in p for p in problems)


def test_the_same_facts_in_other_words_are_fine(cfg):
    assert checks(cfg, article(cfg, extra="\n\nShahin Gurbanov, a London hypnotherapist, runs Hypnotreatment and also works online.")) == []


def test_promo_share_counts_the_paragraph_with_the_link_too(cfg):
    plain = body()
    share = C.promo_share(plain, "Hypnotreatment", "hypnotherapy for insomnia")
    assert 0 < share < C.MAX_PROMO_SHARE, "the one link paragraph alone is within the limit"
    heavy = plain + "\n\n" + "Hypnotreatment " + "word " * 300
    assert C.promo_share(heavy, "Hypnotreatment", "hypnotherapy for insomnia") > C.MAX_PROMO_SHARE


def test_the_check_is_skipped_when_no_brand_or_facts_are_given(cfg):
    advert = article(cfg, extra="\n\n" + FACT_PARAGRAPH + "\n\n" + AI_PARAGRAPH)
    assert not any("advert" in p or "word for word" in p for p in C.local_checks(advert, topic(cfg), "hypnotherapy for insomnia", cfg["banned_claims"], []))


def test_the_instructions_tell_gemini_to_keep_the_brand_short_and_in_its_own_words(cfg):
    prompt = C.article_prompt(topic(cfg), cfg["facts"], "an anchor", None, "Hypnotreatment")
    assert "at most three sentences" in prompt and "never copy the facts word for word" in prompt and "under 15%" in prompt


def test_an_advert_style_draft_is_rewritten_with_the_reason_and_the_second_draft_is_used(cfg):
    advert = raw(cfg, extra="\n\n" + FACT_PARAGRAPH + "\n\n" + AI_PARAGRAPH)
    good = raw(cfg, extra="\n\n" + OWN_WORDS)
    llm = ScriptedLLM([advert, good, OK_REVIEW])
    art = C.build_article(topic(cfg), cfg, llm, [], [])
    assert "not an advert" in llm.prompts[1] and OWN_WORDS in art["body_markdown"]


def test_the_socialfield_facts_are_protected_the_same_way():
    import yaml
    from pathlib import Path
    sf = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "backlinks-social-field.yaml").read_text(encoding="utf-8"))
    pasted = "People can follow organizers and get an email whenever they publish a new event."
    assert C.copied_fact_phrases("Some intro. " + pasted + " More text.", sf["facts"])
    assert not C.copied_fact_phrases("Followers hear about your next event by email.", sf["facts"])


# ---------- the first real 2-a-day run: 4 of 6 drafts were refused at 16-17% brand share, because the brand was named in
# ordinary advice paragraphs and a whole paragraph counts as promotional. The prompt now says how it is measured, and the
# rewrite edits the refused draft with the exact paragraphs to change.

def _brand_in_an_advice_paragraph(cfg, **kw):
    art = article(cfg, **kw)
    art["body_markdown"] = art["body_markdown"].replace("Evening habit number 1 ", "At Hypnotreatment, evening habit number 1 ", 1)
    return art


def test_naming_the_brand_in_an_advice_paragraph_is_refused_and_the_reason_names_the_paragraphs(cfg):
    problems = checks(cfg, _brand_in_an_advice_paragraph(cfg))
    promo = next(p for p in problems if "not an advert" in p)
    assert "A whole paragraph counts as promotional" in promo and "\"Evening habit number 0 suggests" in promo   # the paragraph is identified by its opening words
    assert "words)" in promo and "ONE short paragraph of 40 to 70 words" in promo and "at most" in promo


def test_brand_and_link_together_in_one_short_paragraph_passes(cfg):
    art = article(cfg)
    art["body_markdown"] = art["body_markdown"].replace("Some people also read about", "Some people also read about Hypnotreatment's guide to", 1)
    assert checks(cfg, art) == []


def test_the_prompt_explains_that_whole_paragraphs_are_counted(cfg):
    prompt = C.article_prompt(topic(cfg), cfg["facts"], "an anchor", None, "Hypnotreatment")
    assert "SINGLE short paragraph of 40 to 70 words" in prompt and "whole paragraphs" in prompt and "counts as promotional in full" in prompt
    assert "<previous_draft>" not in prompt, "a first attempt has nothing to edit"


def test_the_rewrite_edits_the_refused_draft_instead_of_starting_again(cfg):
    refused = raw(cfg, extra="\n\nA second, distinctive closing thought about winding down.").replace(
        "Evening habit number 1 ", "At Hypnotreatment, evening habit number 1 ", 1)
    llm = ScriptedLLM([refused, raw(cfg), OK_REVIEW])
    C.build_article(topic(cfg), cfg, llm, [], [])
    assert "<previous_draft>" not in llm.prompts[0]
    assert "<previous_draft>" in llm.prompts[1] and "A second, distinctive closing thought" in llm.prompts[1]
    assert "Edit your previous draft" in llm.prompts[1] and "A whole paragraph counts as promotional" in llm.prompts[1]


def test_copied_facts_are_all_named_up_to_three(cfg):
    problems = checks(cfg, article(cfg, extra="\n\n" + FACT_PARAGRAPH))
    copied = next(p for p in problems if "word for word" in p)
    assert copied.count("'") >= 4, "more than one pasted phrase is listed, so one rewrite can remove all of them"
