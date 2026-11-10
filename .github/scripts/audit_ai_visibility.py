"""Read-only audit of whether each site is reachable/citable by AI answer
engines (ChatGPT, Perplexity, Gemini, Copilot). This is NOT a ranking
tracker -- it never prompts a chat interface. It just checks:

  1. Whether any of the major AI crawlers are blocked in robots.txt.
  2. Whether an /llms.txt file exists.
  3. Whether the homepage has FAQPage/LocalBusiness structured data.
  4. Whether the homepage has any clear Q&A-style headings.

For any site with something to flag, it drafts an llms.txt and a FAQPage
JSON-LD snippet (plain string templating, no LLM call) the owner can
copy-paste into their site, and emails one plain-English report -- this
repo does not control hosting for the client sites, so nothing here is
auto-deployed anywhere.
"""
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from src import outreach
from src.config import load_sites, load_settings
from src.state import now_iso

STATE_SITES_DIR = Path("state/sites")
TIMEOUT_SECONDS = 10
UA = "Mozilla/5.0 (compatible; BacklinkAudit/1.0; +https://github.com/OWNER/backlink-builder)"


CRAWLERS = [
    "GPTBot",
    "ChatGPT-User",
    "PerplexityBot",
    "Google-Extended",
    "ClaudeBot",
    "CCBot",
    "Applebot-Extended",
    "Bingbot",
]

CRAWLER_LABELS = {
    "GPTBot": "OpenAI's ChatGPT training crawler (GPTBot)",
    "ChatGPT-User": "ChatGPT's live-browsing crawler (ChatGPT-User)",
    "PerplexityBot": "Perplexity's crawler (PerplexityBot)",
    "Google-Extended": "Google's AI crawler (Google-Extended), used by Gemini",
    "ClaudeBot": "Anthropic's Claude crawler (ClaudeBot)",
    "CCBot": "Common Crawl's crawler (CCBot) -- a dataset many AI tools train on",
    "Applebot-Extended": "Apple's AI crawler (Applebot-Extended)",
    "Bingbot": "Bing's crawler (Bingbot) -- Microsoft Copilot uses Bing's index",
}

QA_START_RE = re.compile(r"^\s*(how|what|why|can|do|does)\b", re.IGNORECASE)
LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
HEADING_RE = re.compile(r"<h[23][^>]*>(.*?)</h[23]>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def _load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def fetch(url):
    """Best-effort GET. Returns (status, body_text) -- (None, None) on any
    failure, so callers can treat "couldn't check" as a non-crash."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


def _is_broad_disallow(value):
    return value.strip() in ("/", "/*")


def parse_blocked_crawlers(robots_text, crawler_names):
    """Parses User-agent blocks in robots.txt and returns the subset of
    crawler_names that have a Disallow broad enough to block the whole
    site."""
    groups = []
    current_agents = set()
    current_directives = []
    started_directives = False

    for raw_line in robots_text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if started_directives:
                groups.append((current_agents, current_directives))
                current_agents, current_directives, started_directives = set(), [], False
            current_agents.add(value.lower())
        elif key in ("disallow", "allow"):
            started_directives = True
            current_directives.append((key, value))
    if current_agents:
        groups.append((current_agents, current_directives))

    blocked = set()
    for name in crawler_names:
        name_lower = name.lower()
        for agents, directives in groups:
            if name_lower not in agents:
                continue
            if any(k == "disallow" and _is_broad_disallow(v) for k, v in directives):
                blocked.add(name)
    return blocked


def check_robots(site_url):
    _, body = fetch(f"{site_url}/robots.txt")
    if not body:
        return set()
    return parse_blocked_crawlers(body, CRAWLERS)


def check_llms_txt(site_url):
    status, _ = fetch(f"{site_url}/llms.txt")
    return status == 200


def check_homepage(site_url):
    _, body = fetch(site_url)
    if not body:
        return False, False

    structured_data_found = any(
        "FAQPage" in block or "LocalBusiness" in block for block in LD_JSON_RE.findall(body)
    )

    qa_headings_found = False
    for heading in HEADING_RE.findall(body):
        text = TAG_RE.sub("", heading).strip()
        if "?" in text or QA_START_RE.match(text):
            qa_headings_found = True
            break

    return structured_data_found, qa_headings_found


def build_llms_txt_draft(name, description, keywords, site_url):
    lines = [
        f"# {name}",
        "",
        description.strip() or "(no description on file yet)",
        "",
        f"Homepage: {site_url}",
        "",
        "## Topics we cover",
    ]
    for kw in keywords[:10]:
        lines.append(f"- {kw}")
    return "\n".join(lines) + "\n"


def _generate_faq_answers(name, description, questions):
    """One call (not one per question) asking Gemini for a distinct, specific answer to each question. Returns a list of answer
    strings (same length/order as `questions`), or None if the key is
    missing or the response doesn't parse into the expected shape -- callers
    fall back to the templated answer in that case."""
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_key:
        return None

    prompt = (
        f"Site name: {name}\n"
        f"Site description: {description.strip() or '(no description on file)'}\n\n"
        "Questions this FAQ needs to answer, in order:\n"
        + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
        + "\n\n"
        "Write a short (1-2 sentence), natural, specific answer to each question. "
        "Each answer must be distinct from the others -- do not repeat the same "
        "sentence across answers. Base every answer only on the site description "
        "above; do not invent facts, numbers, or claims it doesn't support. "
        "Respond with ONLY a JSON array of strings, one answer per question, in "
        "the same order as the questions -- no other text, no markdown."
    )

    text = outreach.gemini_text(prompt, gemini_key).strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()

    answers = json.loads(text)
    if not isinstance(answers, list) or len(answers) != len(questions):
        return None
    if not all(isinstance(a, str) and a.strip() for a in answers):
        return None
    return [a.strip() for a in answers]


def build_faq_jsonld_draft(name, description, keywords):
    questions = [f"Do you help with {kw}?" for kw in keywords[:4]]
    fallback_answer = description.strip() or "Yes, this is one of the areas we help with."

    answers = None
    if questions:
        try:
            answers = _generate_faq_answers(name, description, questions)
        except Exception as e:
            print(f"[audit_ai_visibility] AI FAQ-answer generation failed, using templated answers: {e}")
            answers = None

    if not answers:
        answers = [fallback_answer] * len(questions)

    entities = [
        {
            "@type": "Question",
            "name": question,
            "acceptedAnswer": {"@type": "Answer", "text": answer},
        }
        for question, answer in zip(questions, answers)
    ]
    faq = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": entities}
    return json.dumps(faq, indent=2)


def audit_site(site, recheck_days):
    site_id = site["id"]
    site_url = site["url"].rstrip("/")
    state_path = STATE_SITES_DIR / site_id / "ai_visibility.json"

    existing = _load_json(state_path, {})
    checked_at = existing.get("checked_at")
    if checked_at:
        try:
            checked_dt = datetime.fromisoformat(checked_at)
            if datetime.now(timezone.utc) - checked_dt < timedelta(days=recheck_days):
                return None
        except ValueError:
            pass

    blocked_crawlers = sorted(check_robots(site_url))
    llms_txt_present = check_llms_txt(site_url)
    structured_data_found, qa_headings_found = check_homepage(site_url)

    profile = _load_json(STATE_SITES_DIR / site_id / "profile.json", {}) or {}
    name = profile.get("name") or site_id
    description = profile.get("description") or ""
    keywords = profile.get("keywords") or []

    llms_txt_draft = build_llms_txt_draft(name, description, keywords, site_url)
    faq_jsonld_draft = build_faq_jsonld_draft(name, description, keywords)

    record = {
        "checked_at": now_iso(),
        "blocked_crawlers": blocked_crawlers,
        "llms_txt_present": llms_txt_present,
        "structured_data_found": structured_data_found,
        "qa_headings_found": qa_headings_found,
        "llms_txt_draft": llms_txt_draft,
        "faq_jsonld_draft": faq_jsonld_draft,
    }
    _save_json(state_path, record)

    flagged = blocked_crawlers or not llms_txt_present or not structured_data_found or not qa_headings_found
    if not flagged:
        return None

    return {
        "site_id": site_id,
        "site_url": site_url,
        "name": name,
        **record,
    }


def send_report(flagged_sites):
    lines = [
        "AI visibility audit -- these sites may not be fully reachable/citable by "
        "AI answer engines (ChatGPT, Perplexity, Gemini, Copilot) yet:",
        "",
    ]
    for site in flagged_sites:
        lines.append(f"=== {site['name']} ({site['site_url']}) ===")
        for crawler in site["blocked_crawlers"]:
            label = CRAWLER_LABELS.get(crawler, crawler)
            lines.append(
                f"- {label} is blocked in your robots.txt -- your site can never be "
                f"cited by the AI tool(s) that rely on it until this is fixed."
            )
        if not site["llms_txt_present"]:
            lines.append(
                "- No /llms.txt file found -- this is a simple text file some AI tools "
                "check to understand what your site offers. A draft is included below."
            )
        if not site["structured_data_found"]:
            lines.append(
                "- No FAQ or LocalBusiness structured data (schema.org markup) found on "
                "your homepage -- this helps AI engines understand and trust your content "
                "enough to cite it. A draft FAQ snippet is included below."
            )
        if not site["qa_headings_found"]:
            lines.append(
                '- No clear question-and-answer style headings found on your homepage '
                '(e.g. "What is...", "How do I...") -- AI engines tend to favor pages '
                "that directly answer questions like this."
            )
        lines.append("")
        lines.append("--- Draft llms.txt content (copy-paste into a new /llms.txt file on your site) ---")
        lines.append(site["llms_txt_draft"])
        lines.append(
            '--- Draft FAQ structured data (paste inside a <script type="application/ld+json"> '
            "tag on your homepage) ---"
        )
        lines.append(site["faq_jsonld_draft"])
        lines.append("")
        lines.append(
            "(Where exactly to paste these depends on your specific website platform/CMS -- "
            "if you're not sure, ask whoever manages your site's hosting.)"
        )
        lines.append("")
    body = "\n".join(lines)

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    from_email = os.environ.get("SMTP_FROM", user)
    to_email = os.environ.get("NOTIFY_TO", "owner@example.com")

    msg = MIMEText(body)
    msg["Subject"] = f"Backlink Builder: AI visibility audit flagged {len(flagged_sites)} site(s)"
    msg["From"] = from_email
    msg["To"] = to_email

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, password)
        server.send_message(msg)


def main():
    settings = load_settings()
    if settings.get("ai_visibility", {}).get("enabled", True) is False:
        print("AI-visibility audit is paused (ai_visibility.enabled: false in config/settings.yaml).")
        return
    recheck_days = settings.get("ai_visibility", {}).get("recheck_days", 7)
    sites = load_sites()

    needs_attention = []
    for site in sites:
        try:
            result = audit_site(site, recheck_days)
            if result:
                needs_attention.append(result)
        except Exception as e:
            print(f"[audit_ai_visibility] site {site.get('id')} failed: {e}")

    if not needs_attention:
        print("AI visibility audit: nothing due, or everything checked looks clean.")
        return

    send_report(needs_attention)
    print(f"AI visibility audit: reported {len(needs_attention)} site(s) needing attention.")


if __name__ == "__main__":
    main()
