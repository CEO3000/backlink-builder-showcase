"""Original article generation with independent quality gates. Gemini writes; our own checks and a separate
fact-check pass decide whether the result may be submitted. Nothing here publishes anything."""
import hashlib
import json
import re

from src import outreach
from src.backlinks.llm import LLM, LLMError

MIN_WORDS, MAX_WORDS = 500, 1200
MAX_AVG_SENTENCE_WORDS = 26
MAX_OVERLAP_WITH_HISTORY = 0.25   # share of 6-word phrases already used in one of our earlier articles
SHINGLE = 6
VENUES = {"devto": "the DEV Community (dev.to)", "wordpresscom": "a general-interest blog on WordPress.com"}   # where each route publishes
MAX_PROMO_SHARE = 0.15            # most of the article must be about the topic, not the brand
COPIED_RUN = 9                    # nine words in a row taken straight from an approved fact count as copying it

# Defaults are the Hypnotreatment wording. A site's config/backlinks*.yaml can override each one
# (disclosure, claims_rule, reviewer_role, review_checks, brand_name), so one pipeline serves several sites.
DISCLOSURE = ("\n\n---\n\n*Disclosure: I write on behalf of [Hypnotreatment]({home}), a hypnotherapy practice in London. "
              "This article is general information, not medical advice. If you are struggling, please speak to your "
              "doctor or another qualified health professional.*")
# How an article for another website's editor must read (guest posts): the work of a careful human expert that the editor is
# glad to publish, not an advert and not a machine's text.
GUEST_STYLE = (
    "Write as the named author in a natural, human voice. The first person (I, or we for a team) is fine where it helps, but never "
    "invent a client, a case, a personal story, a name or a statistic.",
    "Open with the reader's real situation in the first two sentences. Never open with a generic line such as 'In today's fast-paced "
    "world' or 'Have you ever wondered'.",
    "Every section gives the reader something concrete: a step, an example, a short exercise or a clear explanation. No filler and no "
    "repeating the same point in other words.",
    "Write for this website's own readers and topics, so that its editor would be glad to publish it. Never mention guest posting, SEO, "
    "backlinks or these instructions.",
    "Avoid wording editors reject as machine-written: delve, navigate, landscape, tapestry, embark, unlock, unleash, elevate, harness, "
    "seamless, robust, game-changer, testament, realm, 'it is important to note', 'in conclusion'. No heading called Conclusion or "
    "Summary: end with a short, practical closing paragraph.",
    "The title is plain and specific about what the reader gets, in sentence case, without the brand name and without clickbait.",
)
CLAIMS_RULE = ("Never promise or imply a result. Do not say anything cures, treats or fixes a condition, and do not tell the reader to "
               "change or stop medication. Where it fits, say that a doctor should be consulted.")
REVIEWER_ROLE = "a strict fact-checker for a health and wellbeing publication"
REVIEW_CHECKS = (
    "1. A claim about {brand} or its services that is not supported by the approved facts (prices, results, numbers, "
    "credentials, testimonials, awards).\n"
    "2. A medical claim: saying something cures, treats or fixes a condition, promising an outcome, or advising anyone to change "
    "or stop medication.\n"
    "3. An invented statistic, study, quotation or named source.\n")


class ContentError(RuntimeError):
    """category: quota | auth | busy | network | truncated | format | quality | factcheck | unknown"""

    def __init__(self, category: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.category, self.retryable = category, retryable


def article_prompt(topic: dict, facts: list[str], anchor: str, feedback: list[str] | None = None,
                   brand: str = "Hypnotreatment", claims_rule: str = CLAIMS_RULE, previous: str | None = None,
                   venue: str = "the DEV Community (dev.to)", words: tuple[int, int] | None = None, extra_rules: str = "",
                   brief: str = "") -> str:
    fixes = ("\n\nYour previous draft was rejected for these reasons. Fix every one of them:\n- " + "\n- ".join(feedback)) if feedback else ""
    if feedback and previous:
        fixes += ("\n\nEdit your previous draft below instead of starting again: keep the parts that are good and change only what is "
                  "needed to fix the reasons above.\n<previous_draft>\n" + previous + "\n</previous_draft>")
    return (
        f"Write an original, genuinely useful article for {venue}, for {topic['audience']}.\n"
        f"Working title: {topic['title_hint']}\nAngle: {topic['angle']}\n\n"
        + (f"About the writer and the readers (follow this closely):\n{brief}\n\n" if brief else "")
        + f"Rules:\n"
        f"- {(words[0] + 50) if words else MIN_WORDS + 100} to {(words[1] - 50) if words else MAX_WORDS - 300} words, in markdown, with 3 to 5 '##' subheadings, short paragraphs, plain English, UK spelling.\n"
        f"- Teach the reader something practical. It must stand on its own as a good article, not read as an advert.\n"
        f"- Include exactly ONE link in the body, written as [{anchor}]({topic['target_url']}), inside a sentence in the "
        f"middle of the article where it genuinely helps the reader. Use that exact link text. No other links of any kind.\n"
        f"- You may mention {brand} only using these facts, and nothing else about it:\n"
        + "\n".join(f"    * {f}" for f in facts) + "\n"
        f"- Mention {brand} in at most three sentences in the whole article, in your own words (never copy the facts word for word), "
        f"and keep everything about {brand}, including the sentence with the link, to well under 15% of the article. The rest must be "
        f"useful advice that stands on its own.\n"
        f"- Layout of the {brand} part: put everything about {brand} AND the one link together in a SINGLE short paragraph of 40 to 70 words "
        f"in the middle of the article. Never use the name {brand} or the link text in any other paragraph. The 15% is measured by whole "
        f"paragraphs: any paragraph that names {brand} or holds the link counts as promotional in full, however briefly it is named there.\n"
        f"- {claims_rule}\n"
        + (f"- This website's own rules for contributors (follow them): {extra_rules}\n" if extra_rules else "")
        + "".join(f"- {r}\n" for r in (GUEST_STYLE if brief else ()))
        + f"- Do not invent statistics, studies, quotes, names, prices or testimonials. Keep factual claims to common knowledge.\n"
        f"- No em dashes or en dashes, no emojis, no clickbait, no placeholders like [Your Name].\n"
        f"{fixes}\n\n"
        f"Respond in exactly this format and nothing else:\nTITLE: <title under 100 characters>\n"
        f"DESCRIPTION: <one sentence under 150 characters>\nBODY:\n<the article in markdown>"
    )


def parse_article(text: str) -> dict:
    m = re.search(r"TITLE:\s*(.+?)\s*\n\s*DESCRIPTION:\s*(.+?)\s*\n\s*BODY:\s*\n(.+)", text, re.S)
    if not m:
        raise ContentError("format", "the reply was not in the TITLE / DESCRIPTION / BODY format")
    title, description, body = (outreach.strip_dashes(x.strip()) for x in m.groups())
    return {"title": title.strip("# *\"'"), "description": description, "body_markdown": body}


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _shingles(text: str) -> set[tuple]:
    w = _words(text)
    return {tuple(w[i:i + SHINGLE]) for i in range(max(0, len(w) - SHINGLE + 1))}


def overlap_with_history(body: str, history: list[str]) -> float:
    mine = _shingles(body)
    if not mine:
        return 0.0
    return max((len(mine & _shingles(old)) / len(mine) for old in history), default=0.0)


def promo_paragraphs(body: str, brand: str, anchor: str) -> tuple[list[tuple[str, int]], int]:
    """([(opening words, word count)] of every paragraph naming the brand or carrying the link, total words in the article).
    The whole paragraph counts as promotional, however briefly the brand is named in it."""
    paragraphs = [p for p in body.split("\n\n") if p.strip() and not p.lstrip().startswith("#")]
    total = sum(len(_words(p)) for p in paragraphs)
    promo = [(" ".join(p.split()[:8]), len(_words(p))) for p in paragraphs if brand.lower() in p.lower() or anchor.lower() in p.lower()]
    return promo, total


def promo_share(body: str, brand: str, anchor: str) -> float:
    """Share of the article's words that sit in paragraphs naming the brand or carrying the link."""
    promo, total = promo_paragraphs(body, brand, anchor)
    return sum(n for _, n in promo) / total if total else 0.0


def copied_fact_phrases(body: str, facts: list[str]) -> list[str]:
    """Runs of COPIED_RUN or more words taken straight from an approved fact: the article must say it in its own words."""
    text = " ".join(_words(body))
    found = []
    for fact in facts:
        w = _words(fact)
        for i in range(max(0, len(w) - COPIED_RUN + 1)):
            if " ".join(w[i:i + COPIED_RUN]) in text:
                found.append(" ".join(w[i:i + COPIED_RUN]))
                break
    return found


MAX_TITLE_OVERLAP = 0.6   # share of words two titles may have in common before the newer is refused as a repeat
MAX_ANCHOR_USES = 2       # the same exact link text may appear in at most this many of our articles


# Everyday words that say nothing about what a title is about; two titles are repeats only if their MEANINGFUL words overlap.
STOPWORDS = frozenset("""the and for with your you our their his her its this that these those from into onto over under about after before
while when where what which who whom whose why how are was were been being have has had can could will would should may might must not
but than then them they there here out all any each every more most much many some such own just also only very too get got let make
makes made use used using way ways need needs keep keeps one two three four five""".split())


def title_overlap(a: str, b: str) -> float:
    wa, wb = ({w for w in _words(t) if len(w) > 2 and w not in STOPWORDS} for t in (a, b))
    return len(wa & wb) / max(1, min(len(wa), len(wb)))


def local_checks(article: dict, topic: dict, anchor: str, banned: list[str], history: list[str],
                 brand: str | None = None, facts: list[str] | None = None,
                 history_titles: list[str] | None = None, used_anchors: list[str] | None = None,
                 words: tuple[int, int] | None = None) -> list[str]:
    """Everything we can verify without asking a model. Returns the list of problems (empty = passed)."""
    body, title, problems = article["body_markdown"], article["title"], []
    close = next((t for t in (history_titles or []) if title_overlap(title, t) >= MAX_TITLE_OVERLAP), None)
    if close:
        problems.append(f"its title is too close to an earlier article ('{close[:70]}'); take a clearly different angle and title")
    if used_anchors and sum(1 for a in used_anchors if a.lower() == anchor.lower()) >= MAX_ANCHOR_USES:
        problems.append(f"the link text '{anchor}' has already been used {MAX_ANCHOR_USES} times; use a different one")
    if brand:
        promo, total = promo_paragraphs(body, brand, anchor)
        share = sum(n for _, n in promo) / total if total else 0.0
        if share > MAX_PROMO_SHARE:
            listing = "; ".join(f"\"{start}...\" ({n} words)" for start, n in promo)
            problems.append(f"{share:.0%} of the article is about {brand} or carries the link; keep it under {MAX_PROMO_SHARE:.0%} so it reads "
                            f"as a helpful article, not an advert, and mention {brand} in at most three sentences. A whole paragraph counts as "
                            f"promotional if it names {brand} or holds the link, and these do: {listing}. Together they may be at most "
                            f"{int(MAX_PROMO_SHARE * total)} words of the {total}. Move {brand} and the link into ONE short paragraph of 40 to 70 "
                            f"words and take {brand} and the link text out of every other paragraph")
    if facts:
        copied = copied_fact_phrases(body, facts)
        if copied:
            shown = "; ".join(f"'{c}'" for c in copied[:3])
            problems.append(f"it copies the approved facts word for word (e.g. {shown}); say it in your own words")
    lo, hi = words or (MIN_WORDS, MAX_WORDS)
    count = len(_words(body))
    if not lo <= count <= hi:
        problems.append(f"the article is {count} words; it must be between {lo} and {hi}")
    if not 10 <= len(title) <= 110:
        problems.append("the title must be 10 to 110 characters")
    if not 20 <= len(article["description"]) <= 160:
        problems.append("the description must be 20 to 160 characters")
    links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", body)
    if re.search(r"https?://", re.sub(r"\[([^\]]+)\]\(([^)]+)\)", "", body)):
        problems.append("the body contains a bare web address; the only link allowed is the one required link")
    if len(links) != 1:
        problems.append(f"the body has {len(links)} links; it must have exactly one")
    elif links[0][1].strip() != topic["target_url"]:
        problems.append(f"the link must point to {topic['target_url']}")
    elif links[0][0].strip().lower() != anchor.lower():
        problems.append(f"the link text must be exactly '{anchor}'")
    first_para = next((b for b in body.strip().split("\n\n") if not b.lstrip().startswith("#")), "")
    if links and links[0][1] in first_para:
        problems.append("the link must not be in the first paragraph")
    for term in banned:
        if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", body + " " + title, re.I):
            problems.append(f"it uses the banned claim wording '{term}'")
    if re.search(r"\[(?:your|insert|link|name)[^\]]*\]", body, re.I):
        problems.append("it contains a placeholder")
    if re.search(r"as an ai (?:language )?model", body, re.I):
        problems.append("it talks about being an AI model")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", re.sub(r"[#*>`_-]", "", body)) if len(_words(s)) > 2]
    if sentences and sum(len(_words(s)) for s in sentences) / len(sentences) > MAX_AVG_SENTENCE_WORDS:
        problems.append("the sentences are too long on average; use shorter sentences")
    headings = len(re.findall(r"^##\s", body, re.M))
    if not 2 <= headings <= 6:
        problems.append(f"it has {headings} '##' subheadings; use 3 to 5")
    overlap = overlap_with_history(body, history)
    if overlap > MAX_OVERLAP_WITH_HISTORY:
        problems.append(f"{overlap:.0%} of it repeats wording from an earlier article; write it fresh")
    return problems


def review_prompt(article: dict, facts: list[str], brand: str = "Hypnotreatment", role: str = REVIEWER_ROLE,
                  checks: str = REVIEW_CHECKS) -> str:
    return (
        f"You are {role}. Read the article and the list of approved facts about the business it mentions.\n\n"
        f"Approved facts about {brand}:\n" + "\n".join(f"- {f}" for f in facts)
        + f"\n\nArticle title: {article['title']}\n\n{article['body_markdown']}\n\n"
        "Find problems of these kinds only:\n" + checks.format(brand=brand) +
        "Respond with ONLY a JSON object: {\"ok\": true or false, \"issues\": [\"short description of each problem\"]}. "
        "ok is true only if there are no problems."
    )


def parse_review(text: str) -> tuple[bool, list[str]]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.I).strip()
    try:
        data = json.loads(text)
        return bool(data.get("ok")), [str(i) for i in data.get("issues", [])]
    except (ValueError, AttributeError):
        return False, ["the fact-check reply could not be read, so the article was not cleared"]


def choose_anchor(topic: dict, used_anchors: list[str]) -> str:
    """The least-used anchor for this topic, so the same exact-match anchor is not repeated."""
    return min(topic["anchors"], key=lambda a: (used_anchors.count(a), topic["anchors"].index(a)))


def build_article(topic: dict, cfg: dict, llm: LLM, used_anchors: list[str], history: list[str],
                  history_titles: list[str] | None = None) -> dict:
    """A checked article ready to submit. Raises ContentError if it cannot be produced to standard."""
    anchor = choose_anchor(topic, used_anchors)
    brand = cfg.get("brand_name", "Hypnotreatment")
    feedback: list[str] | None = None
    previous: str | None = None   # the draft that was just refused, so the second try edits it instead of guessing again
    for attempt in (1, 2):
        try:
            text, model = llm.generate(article_prompt(topic, cfg["facts"], anchor, feedback, brand, cfg.get("claims_rule", CLAIMS_RULE), previous,
                                                       cfg.get("article_venue", "the DEV Community (dev.to)"), cfg.get("article_words"),
                                                       cfg.get("article_rules", ""), cfg.get("article_brief", "")))
            article = parse_article(text)
            previous = f"TITLE: {article['title']}\nDESCRIPTION: {article['description']}\nBODY:\n{article['body_markdown']}"
            problems = local_checks(article, topic, anchor, cfg.get("banned_claims", []), history, brand, cfg["facts"],
                                    history_titles, used_anchors, cfg.get("article_words"))
            if not problems:
                review = review_prompt(article, cfg["facts"], brand, cfg.get("reviewer_role", REVIEWER_ROLE), cfg.get("review_checks", REVIEW_CHECKS))
                ok, issues = parse_review(llm.generate(review)[0])
                problems = [] if ok else issues
                category = "factcheck"
            else:
                category = "quality"
        except LLMError as e:
            raise ContentError(e.category, str(e), e.retryable) from e
        except ContentError as e:
            problems, category = [str(e)], e.category
        if not problems:
            home = cfg["verification"]["canonical_hosts"][-1]
            article["body_markdown"] += cfg.get("disclosure", DISCLOSURE).format(home=f"https://{home}/")
            article.update(tags=topic["tags"], anchor=anchor, target_url=topic["target_url"], writer_model=model,
                           word_count=len(_words(article["body_markdown"])),
                           version=hashlib.sha256(article["body_markdown"].encode("utf-8")).hexdigest()[:12])
            return article
        feedback = problems
    raise ContentError(category, "the draft failed the checks twice: " + "; ".join(feedback))
