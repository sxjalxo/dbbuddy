# CodeQL triage — 2026-08-25

The first CodeQL scan of the public repository raised 23 alerts. Each was checked
against the code rather than pattern-matched against the rule name, and the
regular-expression family was **measured** rather than reasoned about, because the
rule fires on a shape and the shape is not always the problem.

Seven were real and are fixed. The rest are accurate readings of the code that do
not describe a reachable weakness; they are listed here with the evidence, so a
dismissal is a decision someone can check rather than a shrug.

---

## Fixed

### Quadratic clause extraction — alerts #11, #12, #15, #16, #17, #21

`KEYWORD\s+(.+?)\s+END` is quadratic when the terminator is missing: the engine
restarts at every position where the opening keyword matches, and each restart
scans to the end. Repeating the keyword is enough to trigger it.

Measured, single call, no database involved:

| site | n=1500 | n=3000 | growth |
|---|---|---|---|
| `execution.validate_aggregation` — `select…from` | 125.78 ms | 481.03 ms | 3.8× |
| `intent_builder.ranking_dimension_phrase` — `top N … by` | 103.57 ms | 420.84 ms | 4.1× |
| `query` — `select…from` | 118.38 ms | 470.75 ms | 4.0× |
| `execution` — `SET…WHERE` | 72.97 ms | 297.51 ms | 4.1× |

Both inputs are uncontrolled: `/execute` takes raw SQL from a permitted caller, and
`ranking_dimension_phrase` reads the natural-language question directly.

A cleverer regex does **not** fix this — the cost is the number of starting
positions, not ambiguity inside the pattern. Rewriting the boundaries as
`\bselect\b(.*?)\bfrom\b` measured just as quadratic (439.99 ms at n=3000). What
fixes it is two anchored searches: find the opening keyword once, then the
terminator once, forward from there. That is `dbbuddy_core/text_spans.py`.

After: **0.24 ms** at n=3000, growing linearly.

`execution.py`'s `\([^)]*\)` became `\([^()]*\)` — excluding `(` from the body
bounds each attempt instead of letting every unmatched paren scan to the end.

### Driver text returned to the client — alerts #1–#6

`/analyze` returns a 502 whose `detail` is the `DatabaseUnavailableError` message.
Every raise site was a fixed string except one, which interpolated the raw driver
exception. A driver error routinely embeds the DSN, host, port and username of the
*customer's* database — reconnaissance for an authenticated-but-untrusted caller,
and of no use to a legitimate one. The message now names the engine, database and
host (the caller's own connection details, which is what says *which* connection
failed) and the driver text goes to the log.

### A password-reset token logged in production — alert #8

With `SMTP_HOST` unset the sender logs the message instead of sending it, body
included — and a reset body is a single-use account-takeover link. Correct for a
development box, unacceptable on a real one. Under `DBBUDDY_ENV=production` the
console transport is now refused and returns `False`, so the caller also stops
reporting an undelivered message as sent.

### A credentials file created world-readable — alert #7

The CLI wrote its token file and *then* `chmod`ed it, leaving a window in which any
local account could read the session token. It is now opened with the mode already
set (`os.open(..., 0o600)`).

---

## Not fixed, with evidence

These need dismissing in the GitHub UI; nothing in the code will change them.

### Regexes that are anchored in situ — alerts #10, #20

The rule reads the pattern, not the call. Both are anchored by the API used, so
there is one starting position and no rescan:

- `sql/compiler.py` — `re.match(r"\s*(\w+)\s*\(\s*([\w.]+)\s*\)\s*$", col)`
- `intent_builder.py` — `re.fullmatch(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", value)`

Measured with the API the code actually calls: **1.1× and 2.0×** from n=1500 to
n=3000 — linear. (Measured with `search` instead, they look quadratic, which is how
the rule sees them.)

**Suggested dismissal:** *False positive* — anchored match, linear.

### Regexes measured linear on adversarial input — alerts #13, #14, #19, #22

- `safety.py` — `/\*.*?\*/` under DOTALL
- `execution.py` — `\s+as\s+.*$`, `\s+\w+\s*$`
- `intent_builder.py` — `([a-z_]+)\s*$`

All measured at 1.2×–2.0× growth against inputs built to maximise restarts. They
are left alone deliberately: changing measured-linear code in a hot path buys
nothing and risks a semantic regression, which is a worse trade than an open alert.

**Suggested dismissal:** *False positive* — measured linear; see this file.

### SHA-256 over an API key — alert #23

`hash_api_key` digests a token whose secret is `secrets.token_urlsafe(32)` — 256
bits of entropy. The rule wants a password KDF, which exists to make *guessable*
inputs expensive to try. There is nothing to guess here, and a deliberately slow
hash would be paid on every authenticated API request. This is the same construction
GitHub uses for its own personal access tokens.

**Suggested dismissal:** *Won't fix* — high-entropy random token, not a password.

### The CLI printing a newly created API key — alert #9

`dbbuddy keys create` prints the key once, to stdout, to the person who just created
it. That is the only delivery mechanism; the alternative is a key nobody can use.
Everything else on that path already goes to stderr, and the value is never stored
or re-displayed.

**Suggested dismissal:** *Won't fix* — intentional one-time display to the owner.

---

## How to re-check

The measurements are reproducible. Time each pattern against an input that repeats
its opening keyword — `"select " * n` — at two sizes and compare: a ratio near 2×
is linear, near 4× is quadratic.
