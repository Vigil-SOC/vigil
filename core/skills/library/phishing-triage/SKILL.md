---
name: phishing-triage
description: Give a reported email a verdict, the lure, and the first action. Read this for a user-reported message, suspicious mail, or any finding that is an email.
metadata:
  vigil-origin: "epic #882 slice 2, #1076"
---

# Phishing triage

Use this for a reported email. Judge from `raw_data`, `iocs` and `description`.
If you hold only an id and you hold `get_finding`, fetch the finding first. If
no tool is callable this turn, judge from the input in front of you and still
finish the report. Never end the turn on a tool call you announced, and never
invent a lookup, a click, or a domain age.

## Extract

From the finding, note the display name, envelope from, reply-to and sender
domain; the recipients; the subject; each URL's display text and destination;
each attachment's name, type and hash; and SPF, DKIM and DMARC when present.
A field the finding does not contain is `unknown`.

## Checks

Call a tool only when it is in your grant and callable this turn. Otherwise
record the gap and continue.

- `recall_entity` on the sender domain and each URL, if you hold it. Whether
  that sender or URL has already hit other users: `search_findings`, if you
  hold it. If you hold neither, or you cannot call them, write `not checked`.
- Domain age is `not checked` unless a tool returned it. Never guess one.
- Reputation of each URL and each file hash: read `ioc-enrichment` when you
  hold `read_skill`, and follow that skill for the lookup. Do not restate its
  procedure, and do not end on its indicator report. What it returns is
  evidence for this verdict. A reputation check you did not run is `not queried`.
- Note the pretext: urgency, a request for a password or code, a payment or
  bank-detail change, executive impersonation (a leader's or the company's
  display name on a domain that is not the recipient's).
- Note whether the link destination host differs from the text the user sees,
  and the attachment type (macro-enabled Office, executable, script, disk).
- The recipient clicked or opened something only when the finding says so.
  `user-action` is `clicked` or `opened` in that case, `reported-only` when
  the finding says they reported it and did not click or open, and `unknown`
  when it is silent.

A check you did not run does not soften a rule below. Do not downgrade a
matching rule only because reputation or age was `not queried` or `not checked`.

## Decide

Apply the first rule that matches.

1. A macro-enabled attachment is present (the name ends in `docm`, `dotm`,
   `xlsm`, `xltm` or `pptm`, or the finding says the file contains macros),
   or the attachment is an executable, script or disk image. Verdict
   malicious, lure malware, technique T1566.001, confidence high, next contain.
2. The message asks for a password, passphrase, login or one-time code, and
   it carries a link, and the envelope domain is not the recipient's domain,
   and at least one of these is also true: the domain is a look-alike (a
   character swap, an inserted word, or a brand name that is not that
   domain's real owner), any of SPF, DKIM or DMARC failed, or the destination
   host differs from the visible link text. The brand's own domain with
   passing authentication and a link host that matches the visible text does
   not match, and neither does a failure on the recipient's own domain.
   Verdict malicious, lure credential, technique T1566.002, confidence high,
   next contain.
3. The message asks to change payment or bank details, the envelope domain
   is not the recipient's, and at least one of those same three signals is
   true. The sender's own domain with passing authentication and no
   look-alike does not match. Verdict malicious, lure payment. Technique
   T1566.002 when a link carries the ask, T1566.001 when an attachment does,
   otherwise technique none. Confidence high, next contain.
4. The envelope domain equals the recipient's domain, reply-to is absent or
   on that same domain, SPF, DKIM and DMARC are all pass, there is no link
   to another domain, no attachment, and no request for a password, a code
   or a payment change. A display name of that organisation on its own
   domain is not impersonation. Any failed authentication result, or a
   reply-to on another domain, means this rule does not match. Verdict
   benign, lure none, technique none, confidence high, next dismiss. Do not
   pick suspicious or investigate when this rule matches. Unsolicited
   marketing with those same absences, including passing authentication, is
   verdict spam, lure none, technique none, next dismiss. That a person
   reported the message, or was unsure, is how it reached you, not evidence
   against this rule.
5. Otherwise verdict suspicious, confidence medium or low, next investigate,
   and name what is missing.

`next` contain means recommend blocking the sender domain or URL, and a
password reset when the finding says the recipient clicked or opened. If you
hold `create_approval_action`, propose that block or reset there. If you do
not, escalate with `create_case` when you hold it; otherwise say in the
report that a human must raise it. Never block, reset or delete anything
yourself, and never end the turn on that proposal.

## Output

At most four short sentences, then this block, then stop. The block is the
last thing in the answer: do not wrap it in a code fence. One field per line,
keys exactly as written, one value from each list, no brackets, pipes, bold
or backticks.

```
verdict: <malicious|suspicious|benign|spam>
lure: <credential|payment|malware|none>
sender-domain: <domain or unknown>
user-action: <clicked|opened|reported-only|unknown>
technique: <T1566.001|T1566.002|none>
confidence: <high|medium|low>
next: <contain|investigate|dismiss>
```
