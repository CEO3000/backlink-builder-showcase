"""Gemini access for the publishing pipeline. It reuses the existing client in src/outreach.py (model failover,
request spacing, short retries on busy/429), which stays unchanged for the email workflows, and adds:
  * patience: the publisher writes only a couple of articles a day, so when every model is busy, timing out or
    rate-limited it waits a few minutes and tries again (a few rounds, within a time budget) instead of giving up
  * a temporary problem is never remembered as a permanent one: only a refused key, or a daily quota that Google
    names as per-day, takes a key out for the rest of the run
  * optional extra keys (GEMINI_API_KEY_2, GEMINI_API_KEY_3), tried first so the primary key keeps its quota for the emails
  * a daily call cap counted by us, and error classification

Extra keys only add capacity if they belong to different Google Cloud projects: the Gemini free-tier quota is
per project, so two keys created in the same project share one allowance. Google does not tell us which project a
key belongs to, so this cannot be detected here."""
import inspect
import os
import re
import time
import urllib.error

from src import outreach

KEY_ENV_NAMES = ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3")
PATIENCE_SECONDS = (60, 120)        # waits between whole attempts when Gemini is busy or rate-limited
WAIT_BUDGET_SECONDS = 300           # the most one LLM object (one site's run) loses to Gemini trouble in total (owner, 2026-11-02)
NORMAL_REPLY_SECONDS = 90           # a normal reply takes less than this; anything longer is retries or a hanging request


class LLMError(RuntimeError):
    """category: quota | auth | busy | network | truncated | unknown.
    temporary: waiting a few minutes may fix it (busy, timeouts, a per-minute limit); False for a refused key or a
    quota Google names as per-day."""

    def __init__(self, category: str, message: str, retryable: bool, temporary: bool = False):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.temporary = temporary


def gemini_keys() -> list[str]:
    """Extra keys first, the primary key last: a dedicated key keeps the publisher from using up the primary key's
    daily quota, which the email drafting also needs. With only the primary key, it is used alone."""
    keys = []
    for name in (*KEY_ENV_NAMES[1:], KEY_ENV_NAMES[0]):
        key = os.environ.get(name, "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def quota_detail(e: urllib.error.HTTPError) -> str:
    """Which quota Google says was hit, e.g. 'GenerateRequestsPerDayPerProjectPerModel-FreeTier (limit 20)'."""
    text = getattr(e, "body_text", "") or ""
    ids = sorted(set(re.findall(r'"quotaId"\s*:\s*"([^"]+)"', text)))
    values = re.findall(r'"quotaValue"\s*:\s*"([^"]+)"', text)
    return f": {', '.join(ids)}" + (f" (limit {values[0]})" if values else "") if ids else ""


def classify(e: Exception) -> LLMError:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 429:
            detail = quota_detail(e)
            # Google names per-day quotas ("...PerDay..."): those will not recover by waiting a few minutes.
            return LLMError("quota", f"Gemini quota exhausted (HTTP 429){detail}", True, temporary="PerDay" not in detail)
        if e.code in (401, 403):
            return LLMError("auth", f"Gemini refused the key (HTTP {e.code})", False)
        if e.code in (500, 502, 503, 504):
            return LLMError("busy", f"Gemini is busy (HTTP {e.code})", True, temporary=True)
        return LLMError("unknown", f"Gemini HTTP {e.code}", False)
    if isinstance(e, (TimeoutError, urllib.error.URLError)):
        return LLMError("network", f"{type(e).__name__}: {e}", True, temporary=True)
    if isinstance(e, ValueError) and "stopped before finishing" in str(e):
        return LLMError("truncated", str(e), True, temporary=True)
    return LLMError("unknown", f"{type(e).__name__}: {e}", False)


class LLM:
    def __init__(self, store, daily_cap: int, generate=outreach.gemini_generate, keys: list[str] | None = None,
                 patience=PATIENCE_SECONDS, wait_budget: float = WAIT_BUDGET_SECONDS, sleep=time.sleep, clock=time.monotonic):
        self.store, self.daily_cap, self._generate = store, daily_cap, generate
        self.keys = keys if keys is not None else gemini_keys()
        self.patience, self.wait_budget, self._sleep, self._clock = tuple(patience), wait_budget, sleep, clock
        # time lost to Gemini trouble in this run: failed or hanging requests, the slow part of replies, and the waits between rounds
        self._lost = 0.0
        try:
            self._takes_deadline = "deadline" in inspect.signature(generate).parameters
        except (TypeError, ValueError):
            self._takes_deadline = False
        self._out_for_today: set[int] = set()  # keys that were refused, or whose quota Google says is per-day and used up
        self._last_refusal: LLMError | None = None

    def _attempt(self, prompt: str) -> tuple[str, str]:
        """One pass over the usable keys."""
        last: LLMError | None = None
        for i, key in enumerate(self.keys):
            if i in self._out_for_today:
                continue
            started = self._clock()
            try:
                if self._takes_deadline:   # the request itself never runs past what is left of the budget
                    result = self._generate(prompt, key, deadline=started + max(0.0, self.wait_budget - self._lost) + NORMAL_REPLY_SECONDS)
                else:
                    result = self._generate(prompt, key)
            except Exception as e:  # noqa: BLE001 -- classified below
                self._lost += self._clock() - started
                last = classify(e)
                if last.category == "auth" or (last.category == "quota" and not last.temporary):
                    self._out_for_today.add(i)
                    self._last_refusal = last
                    continue  # another key (another project) may still have capacity
                if last.category == "quota":
                    continue  # a per-minute limit on this key: try the next, and wait if none works
                raise last  # busy / timeout / cut off: not about the key, so switching keys would not help
            finally:
                self.store.add_usage("gemini")
            self._lost += max(0.0, self._clock() - started - NORMAL_REPLY_SECONDS)
            return result
        raise last or self._last_refusal or LLMError("quota", "every Gemini key is out of quota for today", True)

    def generate(self, prompt: str) -> tuple[str, str]:
        """(text, model). When Gemini is busy or rate-limited it waits a few minutes and tries again, a few times,
        within the time budget. Raises LLMError."""
        if not self.keys:
            raise LLMError("auth", "no GEMINI_API_KEY is set", False)
        if self.store.used_today("gemini") >= self.daily_cap:
            raise LLMError("quota", f"our own daily cap of {self.daily_cap} Gemini calls is reached", True)
        if self._lost >= self.wait_budget:
            raise LLMError("busy", f"Gemini has been slow or busy for {self.wait_budget / 60:g} minutes in this run; the rest waits for the next run", True)
        for round_number in range(len(self.patience) + 1):
            try:
                return self._attempt(prompt)
            except LLMError as e:
                if not e.temporary or round_number == len(self.patience):
                    raise
                wait = self.patience[round_number]
                if self._lost + wait > self.wait_budget:
                    raise
                print(f"  [backlinks] Gemini not available ({e}); waiting {wait:g}s, then trying again "
                      f"(round {round_number + 2} of {len(self.patience) + 1})")
                self._sleep(wait)
                self._lost += wait
        raise AssertionError("unreachable")
