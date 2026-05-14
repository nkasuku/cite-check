---
name: cite-check
description: 'Verifiable legal-citation tooling for product counsel reviews. When activated, cite-check turns a flagged risk (or a whole PPL review issue) into a Word document of Citation Cards, where every claim is anchored by two highlighted quotes — one from the legal source, one from the product fact — both byte-for-byte verified against the source before the document is written.'
triggers:
  - cite-check
  - cite check
  - use cite-check
  - run cite-check
  - citation card
  - citation cards
  - legal citation review
  - PPL citation
  - PPL review citations
tools:
  - ask_user
  - bash
  - view
  - create
  - edit
  - grep
  - glob
  - web_fetch
  - sql
---

# cite-check — Copilot CLI Skill

Helps a GitHub product counsel verify AI-generated legal citations in Privacy and Product Legal (PPL) reviews. Produces a Word document of **Citation Cards** on the user's Desktop where each flagged risk is anchored by verbatim quotes from (a) the legal source and (b) the product fact, with byte-for-byte verification done before the document is written.

Modeled on [`dvelton/eyeball`](https://github.com/dvelton/eyeball). Eyeball anchors one claim to one source. cite-check anchors one risk to two — the law and the fact — because in a legal review either anchor failing means the conclusion is wrong.

## Activation

When the user invokes this skill (e.g., "use cite-check", "run cite-check on issue #847", "cite-check this risk"), respond with:

> **cite-check is active.** I'll extract the product facts, identify the implicated legal provisions, byte-for-byte verify every quote, and produce a Word document of Citation Cards on your Desktop. Anything I cannot verify will be refused — never silently quoted.

Then follow the workflow below.

## Tool location

The cite-check Python utility lives at:

```
<plugin_dir>/skills/cite-check/tools/cite.py
```

To find the actual path on this machine:

```bash
find ~/.copilot -name "cite.py" -path "*/cite-check/*" 2>/dev/null
```

If you cannot find it under `~/.copilot`, ask the user for the path to their cite-check checkout.

## First-run setup

Before first use, check that dependencies are installed and the corpus is built:

```bash
python3 <path-to>/cite.py setup-check
```

If anything is missing, run the setup script from the cite-check repo root:

```bash
bash <path-to-repo>/setup.sh         # macOS / Linux
.\setup.ps1                          # Windows (PowerShell)
```

Then build the local corpus from `github/ppl-legal-reference`:

```bash
python3 <path-to>/cite.py refresh-corpus
```

Re-run `refresh-corpus` whenever the user mentions that the reference library has been updated.

## Hard rules (do not break)

These rules implement the user's standing custom instructions. They are non-negotiable.

1. **No fabricated citations.** Every quote that appears in a Citation Card must be byte-for-byte present in the cited source. Use `cite.py verify` on every quote before assembling the cards file. If verify returns exit code 2, you must either (a) replace the quote with one that does verify, or (b) drop the card.
2. **Public-source-only legal basis.** The `LEGAL PROVISION` quote in any card must come from a source classified as `public-law`, `public-guidance`, or `github-public`. Sources classified as `github-internal` may appear *only* in an `INTERNAL CONTEXT — background only` quote inside a card, never as the legal basis. Use the classification field returned by `cite.py extract-source`.
3. **`[VERIFY]` tagging for web fetches.** Anything pulled by `cite.py extract-source --ref <https-URL>` (rather than from the cached corpus) is auto-classified as `verify-required`. You must keep that classification through to the cards file so the rendered card carries the `[VERIFY]` tag.
4. **Jurisdiction confirmation.** Before extracting any legal provisions, ask the user (use `ask_user`) which jurisdictions to analyze under. Default per the user's standing instructions: US federal + California. Always offer to add EU/UK if the product touches EU/UK users.
5. **Never paraphrase inside a quote block.** Citation card quote blocks contain only verbatim text from the source. Analytical text (your interpretation, the nexus, the recommendation) lives in the `nexus` and `action` fields, never in `quote`.
6. **No final legal advice.** The output document is always framed as a drafting aid. Do not change the disclaimer banner the build tool emits.

## Workflow

Follow these steps in order. The order matters.

### Step 1 — Confirm scope with the user

Use `ask_user` for each item. Do not bundle.

1. **Source of facts:** PPL issue ref (e.g., `github/ppl-reviews#847`) OR a pasted risk paragraph OR a pasted draft review. If they give an issue ref, capture it. If they paste text, write it to `~/.copilot/skills/cite-check/cache/facts/manual-<timestamp>.txt`.
2. **Jurisdictions:** default `US federal + California`. Offer EU/UK if not already obvious from the issue.
3. **Risk-tier filter for cards:** default `Medium and High`. Confirm.
4. **Output title and filename** for the Word doc on the Desktop.

### Step 2 — Extract product facts

If the user gave an issue ref:

```bash
python3 <path-to>/cite.py extract-facts --issue <owner>/<repo>#<N> --json
```

Read the JSON output. Identify and **record the verbatim spans** that you will quote later — pull them character-for-character from `body` or each comment's `body` field. Record the source ref as `<owner>/<repo>#<N>` for the issue body, or `<owner>/<repo>#<N>::comment_<id>` for a specific comment.

If the user pasted facts, those are the fact corpus — record verbatim spans with source ref `manual:<timestamp>::para_<n>`.

If the issue links to a design doc in another repo, fetch it with `gh api` or `view` and add it to the fact corpus.

### Step 3 — Identify candidate provisions

Read `<path-to>/taxonomy.json`. For each risk you intend to flag, look up the matching category and list the candidate provisions. The taxonomy is a starting cheat sheet — extend it if you flag a recurring risk that isn't there (edit `taxonomy.json` and tell the user you did).

If a risk doesn't fit any taxonomy category, you may still create a card — but you must locate the provision either in the cached corpus (`cite.py list-corpus` to browse) or via web fetch (auto-tagged `[VERIFY]`).

### Step 4 — Extract verbatim provision text

For each candidate provision:

```bash
python3 <path-to>/cite.py extract-source --ref "<filename>.md#<anchor>" --json
```

Or for a public-law URL not in the cache:

```bash
python3 <path-to>/cite.py extract-source --ref "https://eur-lex.europa.eu/..." --json
```

Read the returned text. Pick the **shortest verbatim span** that fully supports your claim — typically one to three sentences. Record:
- the verbatim quote
- the source ref (the same `--ref` value)
- the `classification` field from the JSON

### Step 5 — Verify every quote

For each quote you intend to put in a card:

```bash
python3 <path-to>/cite.py verify --quote "<exact text>" --source "<ref>"
```

Exit code `0` = verified, set `verified: true` in your card. Exit code `2` = NOT FOUND — you must replace the quote with one that does verify, or drop the card. **Do not edit the verify result.** Do not mark a quote `verified: true` without running this command.

For long multi-sentence quotes, prefer `--strict` to lock to byte-for-byte. For quotes that cross a line break in the source, the default whitespace-normalized mode is fine.

### Step 5b — (Optional) Pre-check screenshot rendering

`cite.py build` automatically renders a highlighted screenshot for each quote and embeds it in the Word doc. If a quote's source is not renderable (see source-ref shapes below), the build falls back to a verbatim text block and logs a "Screenshot render notes" page at the back. To pre-check whether one quote will screenshot cleanly:

```bash
python3 <path-to>/cite.py screenshot \
  --quote "<exact text>" \
  --source "<ref>" \
  --output /tmp/preview.png
```

Exit `0` = PNG written. Exit `2` = the quote text wasn't found in the rendered source (e.g., smart quotes vs. straight quotes, or wrapping that broke the search). If exit 2, either shorten the quote to a distinctive sub-span and re-run `verify` + `screenshot`, or accept the text-only fallback.

**Source-ref shapes that screenshot cleanly:**

| Shape | Example | Renders as |
|---|---|---|
| Cached corpus markdown | `03-reg-gdpr.md#Article 46` | Markdown rendered to a clean document page |
| Public URL | `https://eur-lex.europa.eu/...` | Live page rendered via Playwright (auto `[VERIFY]`) |
| Issue body | `github/ppl-reviews#847` | GitHub-style comment card |
| Specific comment | `github/ppl-reviews#847::comment_12345` | GitHub-style comment card with author + timestamp |

**Anchor conventions in the cached corpus** (what to put after `#` in `<filename>.md#<anchor>`):

| Document type | Anchor convention | Example |
|---|---|---|
| Regulations (GDPR, EU AI Act, ePrivacy, SCCs) | Bare `Article N`, `CHAPTER N`, `SECTION N`, `ANNEX N` | `03-reg-gdpr.md#Article 6`, `03-reg-eu-ai-act-text.md#ANNEX III` |
| US/state codes (CCPA) | `<section number>.` (with trailing dot) | `03-reg-ccpa.md#1798.105.` |
| US Code (Title 17 etc.) | `§<N>` (substring-matches the full `§N · Title` heading) | `06-ip-us-copyright-act-title17.md#§107` |
| Contracts / DPAs (GitHub DPA, Microsoft DPA) | `<N>. <Title>.` (the numbered heading line, dot included) | `01-github-github-dpa.md#9. Subprocessors.` |
| Markdown-structured docs (most policies, guidance) | The literal heading text (case-insensitive substring match) | `01-github-github-privacy-statement.md#Children` |
| Whole document (license texts, short policies) | Omit the `#anchor` entirely | `06-ip-mit-license.md` |

If you're not sure what anchor to use, run `cite.py list-corpus --file <filename>.md` first to see every section the indexer found.

**Source-ref shapes that do NOT screenshot (text-only fallback):**

| Shape | Why | What to do |
|---|---|---|
| `manual:<timestamp>::para_<n>` | No renderable source on disk | If the user pasted the text in chat, save it to a markdown file under the corpus dir and use a real `filename.md#Anchor` ref, then `refresh-corpus`. |

### Step 6 — Assemble the cards file

Write a JSON file to `~/.copilot/skills/cite-check/cache/facts/cards-<timestamp>.json` matching this schema:

```jsonc
{
  "title": "Citation Cards — Product Counsel review of <product>",
  "subtitle": "Source: github/ppl-reviews#847 · Generated by cite-check",
  "risks": [
    {
      "tier": "medium",                // "high" | "medium" | "low"
      "summary": "Cross-border transfer to AI subprocessor without SCCs",
      "quotes": [
        {
          "label": "LEGAL PROVISION",
          "quote": "...verbatim text...",
          "source_ref": "03-reg-gdpr.md#Article 46",
          "classification": "public-law",
          "verified": true
        },
        {
          "label": "GITHUB COMMITMENT",
          "quote": "...verbatim text...",
          "source_ref": "01-github-github-dpa.md#9. Subprocessors.",
          "classification": "github-public",
          "verified": true
        },
        {
          "label": "PRODUCT FACT",
          "quote": "...verbatim text from issue...",
          "source_ref": "github/ppl-reviews#847::comment_12345",
          "classification": "github-public",   // GitHub-internal source, but the FACT itself, not the legal basis
          "verified": true
        }
      ],
      "nexus": "EU personal data will move to a US subprocessor before any Art. 46 transfer mechanism is in place — non-compliant with the regulation and inconsistent with our own DPA commitment.",
      "action": "Block release until SCCs are executed OR Provider X is added to GitHub's approved subprocessor list."
    }
  ]
}
```

Notes on the schema:
- Each card must have at minimum one `LEGAL PROVISION` quote and one `PRODUCT FACT` quote. A `GITHUB COMMITMENT` quote is recommended where applicable.
- `classification` matches what `cite.py extract-source` reported. If the build tool sees `verify-required`, it will tag the rendered card `[VERIFY]`. If it sees `github-internal`, it will tag the rendered card `[INTERNAL CONTEXT — background only]`.
- `verified: true` is mandatory. The build tool refuses to render an unverified quote and lists refusals in the output document.

### Step 7 — Build the document

```bash
python3 <path-to>/cite.py build \
  --cards ~/.copilot/skills/cite-check/cache/facts/cards-<timestamp>.json \
  --output ~/Desktop/<title>.docx \
  --strict
```

`--strict` makes the build fail (exit 2) if any quote is unverified. Use `--strict` by default. Drop it only if the user explicitly wants to see refused-quote placeholders.

Each verified quote is:
1. Captured as a highlighted screenshot from the rendered source (yellow on the cited language) and embedded as a picture, OR
2. If the source ref is not renderable, shown as a verbatim text block with a small italic "screenshot unavailable" note.

In both cases the verbatim text appears on the page so the document is searchable. Render failures are listed on a "Screenshot render notes" page at the back — they do not block the build.

Pass `--no-screenshots` to skip rendering entirely (faster, text-only). Pass `--image-width 5.5` (inches) to shrink screenshots if they overflow.

### Step 8 — Deliver

Tell the user:
- the output file path
- how many cards were rendered, broken down by tier
- any refusals (verify failures, missing sources) and what the user should do about each
- the next reviewer action you'd recommend (e.g., "ready for your read; one card on data retention is parked because GDPR Art. 5(1)(e) wasn't yet in the cache — run `refresh-corpus` and re-run cite-check on that risk only")

## Self-check before delivery

Before saving the cards file or running `build`, mentally verify:

1. Does each card have at least one `LEGAL PROVISION` quote and one `PRODUCT FACT` quote?
2. Is every `LEGAL PROVISION` quote classified `public-law`, `public-guidance`, or `github-public`?
3. Is no `LEGAL PROVISION` block sourced from a `github-internal` document?
4. Did `cite.py verify` actually return 0 for every quote you marked `verified: true`?
5. Does each `nexus` paragraph explain *why* the legal quote and the product quote together create a risk — not just restate them?
6. Are risk tiers conservative? Use 🔴 only for clear non-compliance with a binding rule; 🟡 for material concerns or ambiguity that needs negotiation; 🟢 for minor improvements.
7. Are `[VERIFY]` tags preserved on every web-fetched quote?

## When to extend the taxonomy

If you flag a risk that doesn't map to an existing category in `taxonomy.json`, and you expect this pattern to recur:

1. Read the taxonomy: `view <path-to>/taxonomy.json`
2. Add a new category with `summary` and `candidates` (each candidate needs `id`, `label`, `source_type`, `location`, `anchor`, `classification`).
3. Use `edit` to write the change in place.
4. Tell the user what you added so they can sanity-check.

## When to extend the cached corpus

If the right provision is on a public regulator's site but not in the cache, and you expect to cite it again:

1. Read the taxonomy: `view <path-to>/taxonomy.json`
2. Add the filename to `cached_corpus_allowlist`.
3. Note that the file must already exist in `github/ppl-legal-reference` for `refresh-corpus` to fetch it. If it doesn't, raise it as a follow-up for the user to add to the reference repo.

## Failure modes and how to handle them

| Symptom | What it means | What to do |
|---|---|---|
| `cite.py refresh-corpus` returns "✗ filename (gh api failed)" | File not in `github/ppl-legal-reference` yet | Skip that file; tell the user; suggest opening a PR to add it |
| `cite.py verify` returns exit 2 | Your quote is not in the source | Re-extract with `cite.py extract-source` and copy a span verbatim, or drop the card |
| `cite.py screenshot` returns exit 2 | Quote text wasn't found in the rendered source (smart quotes, ligatures, wrapping) | Try a shorter, more distinctive sub-span — re-`verify` it, then re-`screenshot`. If still fails, accept the text-only fallback. |
| `cite.py build --strict` returns exit 2 | At least one quote is still unverified | Fix verify failures one at a time; do not bypass `--strict` |
| User pastes a risk with no clear product fact | Missing factual anchor | Use `ask_user` to ask for the issue ref, design doc URL, or specific quote that triggered the concern. Do not invent a fact. |
| Provision exists only in a `github-internal` doc | Cannot serve as legal basis | Surface the *public* analogue (regulation, GitHub public commitment); demote the internal doc to `INTERNAL CONTEXT — background only` |
| Build succeeds but several render-notes appear | Screenshots failed for renderable sources (e.g., a public URL was 503) | Re-run build later, or drop `--no-screenshots` for a text-only doc, or replace the URL ref with a cached corpus ref if the same content is mirrored. |
