# cite-check

A tool to help legal reviewers verify AI-generated legal citations.

When AI flags a risk in a product review and tells you "this raises a GDPR Art. 46 cross-border transfer issue," how do you know (a) Art. 46 actually says what the AI claims it says, and (b) the product actually does the thing the AI says triggers it? `cite-check` lets you see for yourself.

This is a Copilot CLI plugin that turns flagged risks in a product review into **Citation Cards** — Word documents where every claim is anchored by **two highlighted screenshots**: one from the legal source, one from the product fact (issue, design doc, or repo). You can verify each link without switching between tabs or hunting for the right section.

> Modeled on [`dvelton/eyeball`](https://github.com/dvelton/eyeball). Eyeball anchors one claim to one source. cite-check anchors one risk to two — the law and the fact — so the reviewer can see both halves of a legal conclusion at once.

## What it does

cite-check has two modes.

### Mode A — ORIGINATE (Citation Cards from a fresh review)

You point Copilot at a Privacy and Product Legal (PPL) review issue (or paste in a flagged risk paragraph) and tell it to use cite-check. The skill:

1. Extracts the **product facts** from the issue body, comments, and any linked design docs.
2. Identifies the **candidate legal provisions** implicated, using a small risk taxonomy plus retrieval over a cached mirror of [`github/ppl-legal-reference`](https://github.com/github/ppl-legal-reference).
3. Pulls the **verbatim text** of each provision (from cache for the high-frequency set, from the regulator's site for everything else — auto-tagged `[VERIFY]`).
4. Verifies every quote byte-for-byte against the source. **A card with a verify failure will not be emitted.**
5. Builds a Word document on your Desktop with one Citation Card per risk: legal screenshot, product fact screenshot, nexus paragraph, recommended action, risk tier (🔴/🟡/🟢).

If the legal screenshot doesn't show what the analysis claims, or the product screenshot doesn't show what the analysis claims, you can see it immediately — without believing the AI.

### Mode B — PRESSURE-TEST (validate a review you already have)

You've already done the review. Now you want to be sure the citations actually hold up. Tell Copilot *"pressure-test the cross-border transfer risk"* (or *"pressure-test all the High-tier risks"*). The skill:

1. Reads the existing review from the conversation (or from a GitHub issue/comment).
2. Builds a JSON spec listing each risk's claim, asserted legal sources, and asserted product facts.
3. For each risk, runs three checks:
   - **Legal anchor:** does the cited provision actually contain the quote (or, if no quote was provided, does the source at least resolve)?
   - **Product fact:** does the cited issue/comment actually say what the review attributes to it, byte-for-byte?
   - **Public-source policy:** is every primary cite publicly citable, or is a `github-internal` document being used as the basis for a legal conclusion?
4. Surfaces a per-risk PASS / WARN / FAIL report inline, with specifics on every gap.

Use Mode B when you want to pressure-test before sending the review out. It catches paraphrased law, hallucinated product quotes, dead links, and reliance on internal-only sources.

## Why two anchors per risk?

A product counsel reviewing AI-assisted output is the most hallucination-sensitive consumer in the building. There are two failure modes:

- The model invents a quote from the law that doesn't exist.
- The model misreads the product and attributes a behavior the product doesn't have.

Either failure ends the analysis. cite-check forces the AI to anchor **both** ends of every conclusion in verbatim source text, then renders both ends as highlighted screenshots so you don't have to take its word for either one.

## Installation

### Prerequisites

- [Copilot CLI](https://docs.github.com/copilot/concepts/agents/about-copilot-cli) installed and authenticated
- [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated with access to `github/ppl-legal-reference`
- Python 3.9 or later

### Install the plugin

In a Copilot CLI session, run:

```
Install the plugin at github.com/<your-org>/cite-check for me.
```

Or clone manually:

```bash
git clone https://github.com/<your-org>/cite-check.git
cd cite-check
bash setup.sh           # macOS / Linux
# or
.\setup.ps1             # Windows (PowerShell)
```

### Verify setup

```bash
python3 skills/cite-check/tools/cite.py setup-check
```

### One-time corpus refresh

Pull the cached legal reference library:

```bash
python3 skills/cite-check/tools/cite.py refresh-corpus
```

Re-run any time `github/ppl-legal-reference` is updated.

## How to use it

In a Copilot CLI conversation, tell it to use cite-check and what you want analyzed:

```
use cite-check on github/ppl-reviews#847 -- review for cross-border data
transfer risks in the AI subprocessor section
```

```
use cite-check on this risk paragraph: "The product will collect IP addresses
from EU users and use them to personalize the experience without a consent
banner."
```

```
use cite-check to draft the full Product Counsel review of issue #912 with
Citation Cards for every Medium and High risk
```

cite-check activates, extracts facts, retrieves provisions, verifies every quote, and writes a Word document to your Desktop.

## What it supports

| Source type | How it's pulled |
|---|---|
| Cached legal corpus (GDPR, CCPA, GitHub DPA, GitHub ToS, GitHub Privacy Statement, MS DPA, SCC Modules, etc.) | Local mirror of `github/ppl-legal-reference`, refreshed on demand |
| Public laws and regulator guidance not in the cache | Web fetch via Playwright, auto-tagged `[VERIFY]` |
| Product facts | `gh api` for issues, comments, linked PRs; local file reads for linked design docs |

## Source-classification rules

Every section in the cached corpus is tagged at refresh time:

| Tag | Example | Allowed in `[LEGAL]` block of a Citation Card? |
|---|---|---|
| `public-law` | GDPR, CCPA, EU AI Act | ✅ |
| `public-guidance` | EDPB, FTC, NIST AI RMF | ✅ |
| `github-public` | GitHub ToS, Privacy Statement, DPA, AUP | ✅ |
| `github-internal` | Internal playbooks, training decks | ❌ — appears only in `[INTERNAL CONTEXT — background only]` block |

This enforces the rule that legal conclusions are grounded only in publicly available sources.

## Output: the Citation Card

For each flagged risk, one card with five blocks:

```
🟡 RISK 2 of 5 — Cross-border transfer to AI subprocessor without SCCs

[1] LEGAL PROVISION                              [VERIFY]
   <highlighted screenshot of GDPR Art. 46(2)(c)>
   Source: EUR-Lex CELEX:32016R0679, Art. 46(2)(c)

[2] GITHUB COMMITMENT
   <highlighted screenshot of GitHub DPA §7.2>
   Source: github/ppl-legal-reference/01-github-dpa.md §7.2

[3] PRODUCT FACT
   <highlighted screenshot of issue #847, comment 4>
   Source: github/ppl-reviews#847, comment 4 (@pm-jane, 2026-04-12)

[4] NEXUS
   EU personal data will move to a US subprocessor before any Art. 46
   transfer mechanism is in place — non-compliant with the regulation
   and inconsistent with our own DPA §7.2 commitment.

[5] RECOMMENDED ACTION
   Block release until SCCs are executed OR Provider X is added to
   GitHub's approved subprocessor list.
```

## Limitations

- Citations to laws not in the cached corpus are tagged `[VERIFY]` so the reviewer knows to confirm independently. This is by design — `cite-check` will not silently treat a web-fetched quote as authoritative.
- Section-level retrieval depends on the corpus being indexed. Deeply nested or non-standard documents may need taxonomy entries added by hand.
- This tool is a drafting aid. It produces structured first drafts with verifiable anchors — never final legal advice.

## License

MIT
