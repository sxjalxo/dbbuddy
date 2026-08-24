"""Resolve once, validate that answer, and connect to *that* address.

The SSRF guard validated the URL and then let `requests` resolve the hostname
again at connect time. Between those two lookups a name can change what it points
at — that is DNS rebinding, and it is not exotic: a short TTL and an
attacker-controlled zone is all it takes. `url_guard` said so about itself:

    Note the limit honestly: this validates the URL, not the socket.

So the check passed on `1.2.3.4` and the socket opened on `169.254.169.254`,
which on a cloud VM hands out IAM credentials to anything that can issue a GET.

The fix is to make validation and connection the *same* answer: resolve once,
check every address that lookup returned, then dial the specific address that was
approved. The name is never resolved a second time, so there is no second answer
to differ.

The rules themselves had to move to `dbbuddy_core` to make this possible. They
lived in `backend/app_db/url_guard.py`, but the outbound call is made by the
engine, which cannot import the backend — which is precisely why validation and
connection were in different places to begin with.
"""

import ipaddress

import pytest

from dbbuddy_core import net_guard


@pytest.fixture(autouse=True)
def strict_mode(monkeypatch):
    """Private ranges blocked, as a hosted deployment would run."""
    monkeypatch.setenv("AI_PROVIDER_BLOCK_PRIVATE_NETWORKS", "1")


def _resolver(*answers):
    """A resolver that returns a different answer on each successive call."""
    calls = {"n": 0}

    def resolve(host):
        index = min(calls["n"], len(answers) - 1)
        calls["n"] += 1
        return [ipaddress.ip_address(a) for a in answers[index]]

    resolve.calls = calls
    return resolve


# ── The rules still hold ─────────────────────────────────────────────────────

def test_link_local_is_always_refused(monkeypatch):
    monkeypatch.delenv("AI_PROVIDER_BLOCK_PRIVATE_NETWORKS", raising=False)
    resolve = _resolver(["169.254.169.254"])
    with pytest.raises(ValueError, match="link-local"):
        net_guard.resolve_and_pin("http://metadata.example", resolver=resolve)


def test_a_public_address_is_allowed():
    resolve = _resolver(["93.184.216.34"])
    url, ip = net_guard.resolve_and_pin("https://example.com/v1", resolver=resolve)
    assert ip == "93.184.216.34"
    assert url == "https://example.com/v1"


def test_private_space_is_refused_in_strict_mode():
    resolve = _resolver(["10.1.2.3"])
    with pytest.raises(ValueError, match="private or loopback"):
        net_guard.resolve_and_pin("http://internal.example", resolver=resolve)


def test_private_space_is_allowed_when_not_strict(monkeypatch):
    """The default deployment runs Ollama on the LAN — blocking it breaks the product."""
    monkeypatch.delenv("AI_PROVIDER_BLOCK_PRIVATE_NETWORKS", raising=False)
    resolve = _resolver(["10.1.2.3"])
    _, ip = net_guard.resolve_and_pin("http://ollama.lan:11434", resolver=resolve)
    assert ip == "10.1.2.3"


def test_a_literal_ip_needs_no_lookup():
    # A genuinely public address: 203.0.113.0/24 is TEST-NET-3, which Python's
    # ipaddress reports as private, so strict mode would refuse it — correctly,
    # and for a reason that has nothing to do with what this test is checking.
    resolve = _resolver(["93.184.216.34"])
    _, ip = net_guard.resolve_and_pin("http://93.184.216.34:8080", resolver=resolve)
    assert ip == "93.184.216.34"
    assert resolve.calls["n"] == 0, "a literal address is already the answer"


# ── The rebinding case ───────────────────────────────────────────────────────

def test_every_address_the_lookup_returned_must_pass():
    """One safe answer alongside a blocked one is still blocked.

    A name can legitimately return several addresses, and picking the first
    acceptable one would let an attacker hide a metadata address behind a public
    one in the same response.
    """
    resolve = _resolver(["93.184.216.34", "169.254.169.254"])
    with pytest.raises(ValueError, match="link-local"):
        net_guard.resolve_and_pin("http://mixed.example", resolver=resolve)


def test_the_name_is_resolved_exactly_once():
    """The whole defence: no second lookup means no second answer to differ."""
    resolve = _resolver(["93.184.216.34"], ["169.254.169.254"])
    _, ip = net_guard.resolve_and_pin("https://rebind.example", resolver=resolve)
    assert ip == "93.184.216.34"
    assert resolve.calls["n"] == 1


def test_a_rebinding_answer_would_have_been_caught_had_it_come_first():
    """Sanity check on the simulation itself.

    If the second answer is what the lookup returns, it is refused — so the
    previous test is demonstrating the pin, not a resolver that never changes.
    """
    resolve = _resolver(["169.254.169.254"], ["93.184.216.34"])
    with pytest.raises(ValueError, match="link-local"):
        net_guard.resolve_and_pin("https://rebind.example", resolver=resolve)


def test_an_unresolvable_name_is_refused_rather_than_dialled():
    """Previously an empty resolution passed validation, because there was nothing
    to object to. Now it fails: there is no address to pin, so there is nothing
    safe to connect to."""
    resolve = _resolver([])
    with pytest.raises(ValueError, match="could not be resolved"):
        net_guard.resolve_and_pin("http://nowhere.example", resolver=resolve)


def test_a_non_http_scheme_is_refused():
    with pytest.raises(ValueError, match="http or https"):
        net_guard.resolve_and_pin("file:///etc/passwd")
