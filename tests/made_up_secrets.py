"""Made-up secrets for the redaction tests, built from pieces so that no whole key shape is ever
in the source (the repository is public and secret-scanned)."""

from pi.core import owner_settings

DASHES = "-" * 5
OPENAI_SHAPE = "sk" + "-proj-" + "fake0" * 5
WIDE_OPENAI_SHAPE = "sk" + "-proj-" + "fake0" * 12  # as wide as a wrapped line
GITHUB_SHAPE = "gh" + "p_" + "fake0" * 7 + "x"
FINE_GRAINED_SHAPE = "github" + "_pat_" + "fake0_" * 10
SLACK_SHAPE = "xo" + "xb-" + "1234-5678-fakefake"
AWS_SHAPE = "AK" + "IA" + "FAKE0FAKE0FAKE00"
STRIPE_SHAPE = "sk" + "_live_" + "fake0" * 4
GOOGLE_SHAPE = "AI" + "za" + "fake0" * 7
JWT_SHAPE = "ey" + "JhbGciOiJub25lIn0." + "ey" + "JzdWIiOiIxMjMifQ.fake0sig"
BEARER_SHAPE = "fake0fake1fake2fake3"
HUGGING_FACE_SHAPE = "h" + "f_" + "FakeMadeUp" * 3 + "Fake"  # letters only, as theirs are
NPM_SHAPE = "np" + "m_" + "fake0" * 7 + "x"
MADE_UP_VALUE = "fake0" * 8  # a value with a digit, long enough to look like a key
CARD = "4111 1111 1111 1111"  # the card networks' published test numbers
KEY_LINES = ["FAKE" * 16, "MADEUP00" * 8, "NOTAREALKEY0" * 5 + "AAAA", "FAKE0=="]


def pem(kind="RSA PRIVATE KEY", lines=KEY_LINES, newline="\n"):
    return newline.join(
        [DASHES + "BEGIN " + kind + DASHES, *lines, DASHES + "END " + kind + DASHES]
    )


def choose(store, values):
    """Save the owner's settings document with these values."""
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(store, owner_settings.Update(expected_revision=revision, values=values))
