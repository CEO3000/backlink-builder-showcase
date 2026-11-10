"""The contract every publication adapter fulfils. One adapter = one confirmed, documented publication route.
There is deliberately no generic "browser bot": a site only gets an adapter after its workflow has been confirmed."""
from dataclasses import dataclass, field


@dataclass
class SubmissionResult:
    # published | draft (saved, not public) | pending_approval | rejected | error
    outcome: str
    url: str | None = None
    reference: str | None = None
    message: str = ""
    # For errors: auth | validation | rate_limited | server | network | rejected | unknown
    error_category: str | None = None
    retryable: bool = False
    http_status: int | None = None
    raw: dict = field(default_factory=dict)


class Adapter:
    name = "base"
    domain = ""
    docs_url = ""
    mechanism = ""

    @classmethod
    def from_config(cls, platform_cfg: dict) -> "Adapter":
        """Built from this platform's block in config/backlinks*.yaml."""
        return cls()

    def available(self) -> tuple[bool, str]:
        """(usable, reason). False when a credential is missing, so nothing is generated for a route that cannot be used."""
        raise NotImplementedError

    def submit(self, content: dict, live: bool) -> SubmissionResult:
        """content: {title, body_markdown, description, tags}. live=False must save a non-public draft."""
        raise NotImplementedError

    def publish_draft(self, reference: str) -> SubmissionResult:
        """Makes an earlier private draft public. outcome is 'published' (with its public url) or an error."""
        raise NotImplementedError

    def find_existing(self, title: str) -> SubmissionResult | None:
        """An earlier submission with this title (used after a crash, so a retry never creates a duplicate)."""
        raise NotImplementedError
