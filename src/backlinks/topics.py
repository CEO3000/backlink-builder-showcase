"""Keeps the article topics from running out. At two articles a day a hand-written list of eight lasts four days, so when
few unused topics are left Gemini proposes new ones. A proposal is only an idea: the link target must be one of the pages
listed in the site's config (`topic_generation.allowed_targets`), so it can never invent an address, and every article
written from it still passes the same checks and fact-check as the hand-written topics."""
import json
import re

from src.backlinks.content import title_overlap
from src.backlinks.llm import LLM, LLMError

DEFAULT_WHEN_UNUSED_BELOW = 4
DEFAULT_COUNT = 6
MAX_TITLE_OVERLAP = 0.6   # share of words two titles may have in common before the newer one is dropped as a repeat


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


def _overlap(a: str, b: str) -> float:
    return title_overlap(a, b)


def path(store):
    return store.root / "topics.json"


def load_generated(store) -> list[dict]:
    try:
        return json.loads(path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_generated(store, topics: list[dict]) -> None:
    path(store).parent.mkdir(parents=True, exist_ok=True)
    path(store).write_text(json.dumps(topics, indent=2, ensure_ascii=False), encoding="utf-8")


def topics_prompt(cfg: dict, existing_titles: list[str], count: int) -> str:
    gen = cfg["topic_generation"]
    pages = "\n".join(f"  * {t['url']}  ({t['about']})" for t in gen["allowed_targets"])
    fit = re.sub(r"^fit with\s+", "", (cfg.get("qualification") or {}).get("fit_criteria") or "the subjects of its own pages")
    brief = (cfg.get("guest_posts") or {}).get("brief") or ""
    return (
        f"Suggest {count} new article topics, written on behalf of {cfg['brand_name']}. Each article will be offered to other websites "
        f"about {fit} that publish articles by outside authors, so it must interest the ordinary readers of such websites "
        f"(never programmers or software developers, unless the subject itself is technology).\n\n"
        + (f"About the writer and the readers:\n{brief}\n\n" if brief else "")
        + f"Approved facts about {cfg['brand_name']}:\n" + "\n".join(f"- {f}" for f in cfg["facts"]) + "\n\n"
        f"Each topic must be genuinely useful to the reader on its own, and only loosely connected to one page of the site. "
        f"The article will contain one link to that page. Pages you may choose from (use the address exactly):\n{pages}\n\n"
        f"Rule for every article: {cfg.get('claims_rule', 'Never promise a result.')}\n\n"
        f"Topics and articles that already exist. Every new topic must take a clearly different angle: never repeat, "
        f"rephrase or lightly vary any of these, and avoid anything tied to a date or season:\n" + "\n".join(f"- {t}" for t in existing_titles) + "\n\n"
        "Respond with ONLY a JSON array. Each item: {\"title_hint\": \"working title under 100 characters\", "
        "\"audience\": \"who it is for\", \"angle\": \"what the article teaches, in one sentence\", "
        "\"target_url\": \"one of the pages above\", \"anchors\": [\"three different natural link texts of 3 to 7 words\"], "
        "\"tags\": [\"up to four lowercase one-word topic tags\"]}. No dashes, no emojis."
    )


def validate(raw: dict, cfg: dict, taken_keys: set[str], taken_titles: list[str], taken_anchors: set[str] = frozenset()) -> tuple[dict | None, str]:
    """(topic, "") or (None, the reason it was dropped)."""
    allowed = {t["url"] for t in cfg["topic_generation"]["allowed_targets"]}
    try:
        title, audience, angle, target = (str(raw[k]).strip() for k in ("title_hint", "audience", "angle", "target_url"))
        anchors = [str(a).strip() for a in raw["anchors"]]
        tags = [re.sub(r"[^a-z0-9]", "", str(t).lower()) for t in raw.get("tags", [])]
    except (KeyError, TypeError, AttributeError):
        return None, "missing or malformed fields"
    tags = [t for t in dict.fromkeys(tags) if t][:4]
    if target not in allowed:
        return None, f"target {target!r} is not one of the allowed pages"
    if not 15 <= len(title) <= 110 or len(audience) < 5 or len(angle) < 15:
        return None, "title, audience or angle has an odd length"
    anchors = list(dict.fromkeys(a for a in anchors if 2 <= len(a.split()) <= 9 and "http" not in a.lower() and a.lower() not in taken_anchors))
    if len(anchors) < 3:
        return None, "fewer than three usable link texts that no earlier article has used"
    if not tags:
        return None, "no usable tags"
    if re.search("[–—]", title + angle + " ".join(anchors)):
        return None, "contains a dash"
    key = f"gen-{cfg['site_id']}-{_slug(title)}"
    if key in taken_keys or any(_overlap(title, t) >= MAX_TITLE_OVERLAP for t in taken_titles):
        return None, "repeats an existing topic"
    return {"key": key, "title_hint": title, "audience": audience, "angle": angle, "target_url": target, "anchors": anchors[:4],
            "tags": tags, "generated": True}, ""


def parse_topics(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.I).strip()
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array")
    return data


def used_keys(store, adapter_names=None) -> set[str]:
    """Topics already written, whatever route the article took (guest post, or a platform in the past)."""
    return {j["topic_key"] for j in store.jobs.values() if j.get("topic_key")}


def with_generated_topics(store, cfg: dict, llm: LLM | None, log: list[str] | None = None) -> dict:
    """cfg with the hand-written topics followed by the generated ones, asking Gemini for more first if few are unused."""
    gen = cfg.get("topic_generation") or {}
    if not gen.get("enabled"):
        return cfg
    log = log if log is not None else []
    generated = load_generated(store)
    topics = [*cfg["topics"], *generated]
    used = used_keys(store, cfg.get("platforms", {}).keys())
    unused = [t for t in topics if t["key"] not in used]
    if len(unused) < gen.get("when_unused_below", DEFAULT_WHEN_UNUSED_BELOW) and llm is not None:
        count = gen.get("count", DEFAULT_COUNT)
        try:
            written = [j for j in store.jobs.values() if j.get("anchor_text") and j.get("title")]   # our own articles, on any route
            existing = [t["title_hint"] for t in topics] + [j["title"] for j in written if j.get("title")]
            proposals = parse_topics(llm.generate(topics_prompt(cfg, existing, count))[0])
        except (LLMError, ValueError) as e:
            log.append(f"- Topic ideas could not be generated now ({type(e).__name__}: {str(e)[:100]}); {len(unused)} unused topic(s) left.")
        else:
            taken_keys, taken_titles = {t["key"] for t in topics}, list(existing)
            taken_anchors = {a.lower() for t in topics for a in t["anchors"]} | {j["anchor_text"].lower() for j in written if j.get("anchor_text")}
            added, dropped = [], []
            for raw in proposals:
                topic, why = validate(raw, cfg, taken_keys, taken_titles, taken_anchors) if isinstance(raw, dict) else (None, "not an object")
                if topic:
                    added.append(topic)
                    taken_keys.add(topic["key"])
                    taken_titles.append(topic["title_hint"])
                    taken_anchors.update(a.lower() for a in topic["anchors"])
                else:
                    dropped.append(why)
            if added:
                generated += added
                save_generated(store, generated)
                topics = [*cfg["topics"], *generated]
            log.append(f"- Topic ideas: {len(added)} new topic(s) added, {len(dropped)} dropped" + (f" ({'; '.join(sorted(set(dropped)))[:160]})" if dropped else "") + ".")
    return {**cfg, "topics": topics}
