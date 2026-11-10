"""The publication lifecycle. A backlink only counts once a job reaches VERIFIED: an accepted submission, a
generated article or a sent email is never success.

DISCOVERED -> QUALIFIED -> CONTENT_READY -> SUBMISSION_IN_PROGRESS -> SUBMITTED -> PUBLISHED -> VERIFIED
"""

DISCOVERED = "DISCOVERED"
QUALIFIED = "QUALIFIED"
CONTENT_READY = "CONTENT_READY"
SUBMISSION_IN_PROGRESS = "SUBMISSION_IN_PROGRESS"
SUBMITTED = "SUBMITTED"
PUBLISHED = "PUBLISHED"
VERIFIED = "VERIFIED"

REJECTED = "REJECTED"                        # the site, or our own checks, refused it
PENDING_APPROVAL = "PENDING_APPROVAL"        # accepted but waiting for a human (editor, or us before going live)
RETRY_REQUIRED = "RETRY_REQUIRED"            # a transient failure; retried with backoff, a bounded number of times
VERIFICATION_FAILED = "VERIFICATION_FAILED"  # published, but the link could not be confirmed (yet)
REMOVED = "REMOVED"                          # it was verified once and is no longer live
UNSUPPORTED_PUBLICATION_METHOD = "UNSUPPORTED_PUBLICATION_METHOD"  # needs email, CAPTCHA, an account or manual approval
BLOCKED_CREDENTIALS = "BLOCKED_CREDENTIALS"  # a key or login is missing, expired or refused
INACCESSIBLE = "INACCESSIBLE"                # the published page is permanently unreachable

ALL = {
    DISCOVERED, QUALIFIED, CONTENT_READY, SUBMISSION_IN_PROGRESS, SUBMITTED, PUBLISHED, VERIFIED, REJECTED,
    PENDING_APPROVAL, RETRY_REQUIRED, VERIFICATION_FAILED, REMOVED, UNSUPPORTED_PUBLICATION_METHOD,
    BLOCKED_CREDENTIALS, INACCESSIBLE,
}

TRANSITIONS = {
    DISCOVERED: {QUALIFIED, REJECTED, UNSUPPORTED_PUBLICATION_METHOD, RETRY_REQUIRED},
    QUALIFIED: {CONTENT_READY, BLOCKED_CREDENTIALS, REJECTED, RETRY_REQUIRED},
    CONTENT_READY: {SUBMISSION_IN_PROGRESS, REJECTED, RETRY_REQUIRED},
    SUBMISSION_IN_PROGRESS: {SUBMITTED, PUBLISHED, PENDING_APPROVAL, REJECTED, RETRY_REQUIRED, BLOCKED_CREDENTIALS,
                             UNSUPPORTED_PUBLICATION_METHOD},
    SUBMITTED: {PUBLISHED, PENDING_APPROVAL, REJECTED, RETRY_REQUIRED},
    PENDING_APPROVAL: {PUBLISHED, REJECTED, SUBMITTED},   # a guest post waits for the owner's CAPTCHA click, then is sent
    PUBLISHED: {VERIFIED, VERIFICATION_FAILED, INACCESSIBLE},
    VERIFICATION_FAILED: {PUBLISHED, VERIFIED, INACCESSIBLE, REMOVED},
    VERIFIED: {VERIFIED, REMOVED, VERIFICATION_FAILED},
    RETRY_REQUIRED: {DISCOVERED, QUALIFIED, CONTENT_READY, REJECTED},
    BLOCKED_CREDENTIALS: {QUALIFIED, REJECTED},
    REJECTED: set(),
    REMOVED: set(),
    UNSUPPORTED_PUBLICATION_METHOD: set(),
    INACCESSIBLE: set(),
}

TERMINAL = {s for s, nxt in TRANSITIONS.items() if not nxt}
# Jobs in these states are finished with, so their domain/page no longer blocks a new attempt.
DEAD = TERMINAL | {REJECTED}


class IllegalTransition(ValueError):
    pass


def check_transition(old: str, new: str) -> None:
    if new not in ALL:
        raise IllegalTransition(f"unknown status {new!r}")
    if new not in TRANSITIONS.get(old, set()):
        raise IllegalTransition(f"{old} -> {new} is not allowed")
