#!/usr/bin/env python3
"""
cite-check — verifiable legal-citation tooling for product counsel reviews.

This is the worker invoked by the cite-check Copilot CLI skill (see SKILL.md
in the same directory). It exposes a small set of subcommands the agent uses
to (a) maintain a local cache of the legal reference library, (b) extract
verbatim text from legal sources and product facts, (c) byte-for-byte verify
quoted spans (the hallucination guard), and (d) build a Word document of
Citation Cards on the user's Desktop.

Modeled on dvelton/eyeball. Every claim that ends up in a card must be
anchored in verbatim text from a real source; this tool refuses to emit a
card that fails verification.

Subcommands:
  setup-check       — confirm Python deps + gh CLI are present
  refresh-corpus    — mirror the cached file allowlist from
                      github/ppl-legal-reference into the local cache and
                      build a section index
  list-corpus       — print the current cache state (file, classification,
                      section count)
  extract-source    — given a citation ref (e.g. "03-reg-eu-gdpr.md#Article 6"
                      or a public URL), print verbatim section text + anchors
  extract-facts     — given a GitHub issue ref (owner/repo#N), print body +
                      comments as a structured fact corpus
  verify            — given a quoted span and a source ref, exit 0 iff the
                      quote appears byte-for-byte in the source
  build             — given a JSON file of Citation Cards, render a Word doc

Cache layout:
  ~/.copilot/skills/cite-check/cache/
    corpus/                  # mirrored markdown files
    index.json               # file → list of (anchor, byte_start, byte_end,
                             #                  classification, source_url)
    fetched/                 # web-fetched provisions (auto [VERIFY] tagged)
    facts/                   # cached fact corpora keyed by issue ref

Exit codes:
  0  success
  1  generic error
  2  verification failed (the hallucination guard tripped)
  3  missing dependency or unconfigured environment
"""
from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional


# --- paths -------------------------------------------------------------------

TOOLS_DIR = Path(__file__).resolve().parent
TAXONOMY_PATH = TOOLS_DIR / "taxonomy.json"

CACHE_ROOT = Path.home() / ".copilot" / "skills" / "cite-check" / "cache"
CORPUS_DIR = CACHE_ROOT / "corpus"
FETCHED_DIR = CACHE_ROOT / "fetched"
FACTS_DIR = CACHE_ROOT / "facts"
RENDERED_DIR = CACHE_ROOT / "rendered"
INDEX_PATH = CACHE_ROOT / "index.json"

REFERENCE_REPO = "github/ppl-legal-reference"


# --- small helpers -----------------------------------------------------------

def _ensure_dirs() -> None:
    for p in (CORPUS_DIR, FETCHED_DIR, FACTS_DIR, RENDERED_DIR):
        p.mkdir(parents=True, exist_ok=True)


def _hash_key(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


def _load_taxonomy() -> dict[str, Any]:
    with TAXONOMY_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def _classification_for(filename: str) -> str:
    """Map the ppl-legal-reference filename prefix to a source classification.

    Mirrors the prefix taxonomy described in the user's custom instructions:
      01-github-*  → github-public  (ToS, DPA, Privacy Statement, AUP, ...)
      02-internal-* → github-internal  (playbooks, training)
      03-reg-*     → public-law
      04-msft-*    → public-guidance  (Microsoft DPA / minimum bar)
      05-guidance-* → public-guidance
      06-ip-*      → public-law       (Title 17 + open source licenses)
      07-process-* → github-internal
      08-caselaw-* → public-guidance
    """
    base = filename.lower()
    if base.startswith("01-github-"):
        return "github-public"
    if base.startswith("02-internal-"):
        return "github-internal"
    if base.startswith("03-reg-"):
        return "public-law"
    if base.startswith("04-msft-"):
        return "public-guidance"
    if base.startswith("05-guidance-"):
        return "public-guidance"
    if base.startswith("06-ip-"):
        return "public-law"
    if base.startswith("07-process-"):
        return "github-internal"
    if base.startswith("08-caselaw-"):
        return "public-guidance"
    return "github-public"


# --- subcommand: setup-check -------------------------------------------------

def cmd_setup_check(_args: argparse.Namespace) -> int:
    print("cite-check setup check")
    print("=" * 40)
    ok = True

    print(f"  python:       {sys.version.split()[0]}")

    for mod in ("docx", "fitz", "PIL", "playwright", "yaml", "requests", "markdown"):
        try:
            __import__(mod if mod != "docx" else "docx")
            print(f"  module {mod:<12} ✓")
        except ImportError:
            print(f"  module {mod:<12} ✗  (run setup.sh / setup.ps1)")
            ok = False

    if _have("gh"):
        try:
            who = _run(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
            print(f"  gh CLI        ✓  (authenticated as {who})")
        except subprocess.CalledProcessError:
            print("  gh CLI        ✗  (installed but not authenticated — run `gh auth login`)")
            ok = False
    else:
        print("  gh CLI        ✗  (install from https://cli.github.com/)")
        ok = False

    print(f"  cache root:   {CACHE_ROOT}  ({'exists' if CACHE_ROOT.exists() else 'will be created on first refresh'})")
    if INDEX_PATH.exists():
        idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        print(f"  corpus:       {len(idx.get('files', {}))} files indexed")
    else:
        print("  corpus:       not yet built — run `cite.py refresh-corpus`")

    return 0 if ok else 3


# --- subcommand: refresh-corpus ---------------------------------------------

_SECTION_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # Markdown headings
    ("md-heading", re.compile(r"^(?P<lvl>#{1,6})\s+(?P<text>.+\S)\s*$", re.MULTILINE)),
    # Regulation chapter / section / annex headers (GDPR, EU AI Act):
    #   "Article 6", "Article 6a", "Article 6(1)", "CHAPTER III", "SECTION 1",
    #   "ANNEX III"
    ("regulation-article", re.compile(
        r"^(?P<text>(?:Article\s+\d+[a-z]?(?:\(\d+\))?"
        r"|CHAPTER\s+[IVXLCDM]+"
        r"|SECTION\s+\d+"
        r"|ANNEX\s+[IVXLCDM]+))\s*$",
        re.MULTILINE,
    )),
    # CCPA-style numbered code sections: "1798.100. Title"
    ("us-code-section", re.compile(
        r"^(?P<text>\d{3,4}\.\d{1,3}\.\s+[A-Z][^\n]{3,160})$",
        re.MULTILINE,
    )),
    # US Code section-sign headings (Title 17, etc.):
    #   "§107 · Limitations on exclusive rights: Fair use"
    # We require the bullet+title because PDF-extracted pages have many bare
    # "§NNN" page-footer occurrences that aren't real section starts.
    ("us-code-section-sign", re.compile(
        r"^(?P<text>§\s?\d{1,4}[A-Z]?\s*·\s*[^\n]{2,200})\s*$",
        re.MULTILINE,
    )),
    # Contract / DPA numbered sections: "1. Definitions.", "9. Subprocessors."
    # Heuristic: digit(s) + dot + space + capital, ending with a period and short
    # title text. Restricted enough to not match every numbered list item.
    ("contract-section", re.compile(
        r"^(?P<text>\d{1,2}\.\s+[A-Z][A-Za-z][A-Za-z0-9 ,.&'/-]{2,80}\.)\s*(?:[A-Z]|$)",
        re.MULTILINE,
    )),
)


def _index_document(text: str) -> list[dict[str, Any]]:
    """Build a list of section anchors from a corpus document.

    Recognizes four structural conventions used in github/ppl-legal-reference:
      1. Markdown ATX headings (#, ##, ...)
      2. Regulation articles / chapters / annexes (GDPR, EU AI Act): bare lines
         like "Article 6", "CHAPTER III", "ANNEX III", "SECTION 1"
      3. US-code-style sections: "1798.100. Title" (CCPA)
      4. Contract / DPA numbered sections: "9. Subprocessors."

    Each returned section records (anchor, kind, level, byte_start, byte_end).
    The byte range covers from the matched line through the byte before the
    next section start (or end of file).
    """
    raw = text.encode("utf-8")
    if not raw:
        return [{"anchor": "(empty)", "kind": "doc", "level": 0, "byte_start": 0, "byte_end": 0}]

    # Convert character offsets to byte offsets once for the whole doc.
    char_to_byte = []
    cursor = 0
    for ch in text:
        char_to_byte.append(cursor)
        cursor += len(ch.encode("utf-8"))
    char_to_byte.append(len(raw))

    found: list[dict[str, Any]] = []
    seen_starts: set[int] = set()
    for kind, pat in _SECTION_PATTERNS:
        for m in pat.finditer(text):
            start_char = m.start()
            if start_char in seen_starts:
                continue
            seen_starts.add(start_char)
            anchor_text = m.group("text").strip()
            level = len(m.group("lvl")) if "lvl" in m.groupdict() and m.group("lvl") else 2
            found.append({
                "anchor": anchor_text,
                "kind": kind,
                "level": level,
                "_start_char": start_char,
                "byte_start": char_to_byte[start_char],
            })

    if not found:
        return [{
            "anchor": "(whole document)", "kind": "doc", "level": 0,
            "byte_start": 0, "byte_end": len(raw),
        }]

    found.sort(key=lambda s: s["_start_char"])
    sections: list[dict[str, Any]] = []
    for i, s in enumerate(found):
        end = found[i + 1]["byte_start"] if i + 1 < len(found) else len(raw)
        sections.append({
            "anchor": s["anchor"],
            "kind": s["kind"],
            "level": s["level"],
            "byte_start": s["byte_start"],
            "byte_end": end,
        })
    return sections


# Back-compat alias — earlier code (and tests) may reference _index_markdown.
_index_markdown = _index_document


def cmd_refresh_corpus(args: argparse.Namespace) -> int:
    if not _have("gh"):
        print("error: gh CLI not found. Install from https://cli.github.com/", file=sys.stderr)
        return 3

    _ensure_dirs()
    taxonomy = _load_taxonomy()
    allowlist: list[str] = list(taxonomy.get("cached_corpus_allowlist", []))
    if args.only:
        allowlist = [f for f in allowlist if f in set(args.only)]

    print(f"Refreshing {len(allowlist)} files from {REFERENCE_REPO} into {CORPUS_DIR}")
    # Merge with the existing index so `--only` updates a subset without
    # discarding entries for files we didn't fetch this run.
    if INDEX_PATH.exists() and args.only:
        try:
            index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            index.setdefault("files", {})
            index["reference_repo"] = REFERENCE_REPO
        except (json.JSONDecodeError, ValueError):
            index = {"reference_repo": REFERENCE_REPO, "files": {}}
    else:
        index = {"reference_repo": REFERENCE_REPO, "files": {}}

    for filename in allowlist:
        api_path = f"repos/{REFERENCE_REPO}/contents/{filename}"
        try:
            raw = _run(["gh", "api", api_path]).stdout
            payload = json.loads(raw)
            content_b64 = payload.get("content", "")
            sha = payload.get("sha", "")
            file_size = payload.get("size", 0)
            encoding = payload.get("encoding", "")
            content = base64.b64decode(content_b64).decode("utf-8") if content_b64 else ""
            # GitHub Contents API returns empty content with encoding="none" for
            # files larger than ~1MB. Fall back to the raw blob endpoint, which
            # streams the full file regardless of size.
            if (not content) and file_size > 0 and encoding in ("none", ""):
                blob_raw = _run([
                    "gh", "api",
                    "-H", "Accept: application/vnd.github.raw",
                    f"repos/{REFERENCE_REPO}/contents/{filename}",
                ]).stdout
                content = blob_raw
        except subprocess.CalledProcessError as e:
            print(f"  ✗ {filename}  (gh api failed — file may not exist yet)")
            print(f"      {e.stderr.strip().splitlines()[0] if e.stderr else ''}")
            continue
        except (json.JSONDecodeError, ValueError) as e:
            print(f"  ✗ {filename}  (decode error: {e})")
            continue

        local = CORPUS_DIR / filename
        local.write_text(content, encoding="utf-8")
        sections = _index_document(content)
        classification = _classification_for(filename)
        index["files"][filename] = {
            "sha": sha,
            "byte_size": len(content.encode("utf-8")),
            "classification": classification,
            "source_url": f"https://github.com/{REFERENCE_REPO}/blob/main/{filename}",
            "sections": sections,
        }
        print(f"  ✓ {filename:<45} {classification:<18} {len(sections):>3} sections")

    INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"\nWrote index: {INDEX_PATH}")
    return 0


# --- subcommand: list-corpus -------------------------------------------------

def cmd_list_corpus(args: argparse.Namespace) -> int:
    if not INDEX_PATH.exists():
        print("No corpus indexed yet. Run: cite.py refresh-corpus", file=sys.stderr)
        return 1
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    print(f"Reference repo: {index.get('reference_repo')}")
    print(f"Cache:          {CORPUS_DIR}\n")
    files = index.get("files", {})
    target = getattr(args, "file", None)
    if target:
        meta = files.get(target)
        if not meta:
            print(f"{target} not in cached corpus.", file=sys.stderr)
            print("Available files:", file=sys.stderr)
            for f in sorted(files):
                print(f"  {f}", file=sys.stderr)
            return 1
        print(
            f"  {target:<45} {meta['classification']:<18} "
            f"{meta['byte_size']:>7} bytes  {len(meta['sections']):>3} sections\n"
        )
        for s in meta["sections"]:
            kind = s.get("kind", "md-heading")
            print(f"    [{kind}] {s['anchor']}")
        return 0

    for filename, meta in sorted(files.items()):
        print(
            f"  {filename:<45} {meta['classification']:<18} "
            f"{meta['byte_size']:>7} bytes  {len(meta['sections']):>3} sections"
        )
    return 0


# --- subcommand: extract-source ---------------------------------------------

def _resolve_corpus_section(filename: str, anchor: str | None) -> dict[str, Any]:
    if not INDEX_PATH.exists():
        raise FileNotFoundError("Corpus index missing. Run refresh-corpus first.")
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    file_meta = index.get("files", {}).get(filename)
    if not file_meta:
        raise KeyError(f"{filename} not in cached corpus. Add to taxonomy.cached_corpus_allowlist and refresh.")
    local = CORPUS_DIR / filename
    if not local.exists():
        raise FileNotFoundError(f"Local copy missing: {local}. Run refresh-corpus.")
    raw_bytes = local.read_bytes()

    if not anchor:
        return {
            "filename": filename,
            "anchor": "(whole document)",
            "classification": file_meta["classification"],
            "source_url": file_meta["source_url"],
            "text": raw_bytes.decode("utf-8"),
            "byte_start": 0,
            "byte_end": len(raw_bytes),
        }

    matches = [s for s in file_meta["sections"] if s["anchor"].lower() == anchor.lower()]
    if not matches:
        matches = [s for s in file_meta["sections"] if anchor.lower() in s["anchor"].lower()]
    if not matches:
        raise KeyError(
            f"Anchor '{anchor}' not found in {filename}. "
            f"Available headings (first 20): {[s['anchor'] for s in file_meta['sections'][:20]]}"
        )
    section = matches[0]
    text = raw_bytes[section["byte_start"]: section["byte_end"]].decode("utf-8")
    return {
        "filename": filename,
        "anchor": section["anchor"],
        "classification": file_meta["classification"],
        "source_url": file_meta["source_url"],
        "text": text,
        "byte_start": section["byte_start"],
        "byte_end": section["byte_end"],
    }


def _fetch_web_source(url: str) -> dict[str, Any]:
    """Fetch a web source and cache it under fetched/. Auto-tagged [VERIFY]."""
    import requests  # noqa: WPS433 (deferred import keeps setup-check informative)

    cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    target = FETCHED_DIR / f"{cache_key}.txt"
    meta_target = FETCHED_DIR / f"{cache_key}.meta.json"
    if not target.exists():
        resp = requests.get(url, timeout=30, headers={"User-Agent": "cite-check/0.1"})
        resp.raise_for_status()
        target.write_text(resp.text, encoding="utf-8")
        meta_target.write_text(json.dumps({"url": url}), encoding="utf-8")
    return {
        "filename": str(target),
        "anchor": "(web)",
        "classification": "verify-required",
        "source_url": url,
        "text": target.read_text(encoding="utf-8"),
        "byte_start": 0,
        "byte_end": target.stat().st_size,
        "verify_tag": True,
    }


def _resolve_source_text(ref: str) -> dict[str, Any]:
    """Single dispatcher used by extract-source, verify, and screenshot to
    obtain the verbatim text of a source.

    Recognized source-ref shapes:
      * http(s):// URL                    → web fetch, classification verify-required
      * <owner>/<repo>#<N>               → GitHub issue body
      * <owner>/<repo>#<N>::comment_<id> → a single GitHub issue comment
      * <filename>.md                    → whole cached corpus file
      * <filename>.md#<anchor>           → one section of a cached corpus file

    Returns the same dict shape produced by `_resolve_corpus_section` /
    `_fetch_web_source` so verify can do byte-for-byte matching uniformly.
    """
    ref = ref.strip()
    if ref.startswith(("http://", "https://")):
        return _fetch_web_source(ref)

    cm = _COMMENT_REF_RE.match(ref)
    if cm:
        owner, repo, num = cm.group("owner"), cm.group("repo"), cm.group("num")
        comment_id = cm.group("cid")
        facts = _load_facts(f"{owner}/{repo}#{num}")
        if comment_id:
            comment = next(
                (c for c in facts.get("comments", []) if str(c.get("id")) == comment_id),
                None,
            )
            if not comment:
                raise KeyError(f"Comment {comment_id} not found in {ref}")
            text = comment.get("body", "") or ""
            anchor = f"comment by @{comment.get('author', '(unknown)')} on {comment.get('created_at', '')[:10]}"
            url = comment.get("url", facts.get("url"))
        else:
            text = facts.get("body", "") or ""
            anchor = f"issue body by @{facts.get('author', '(unknown)')}"
            url = facts.get("url")
        return {
            "filename": ref,
            "anchor": anchor,
            "classification": "github-public",
            "source_url": url,
            "text": text,
            "byte_start": 0,
            "byte_end": len(text.encode("utf-8")),
        }

    if "#" in ref:
        filename, anchor = ref.split("#", 1)
        return _resolve_corpus_section(filename.strip(), anchor.strip())
    return _resolve_corpus_section(ref, None)


def cmd_extract_source(args: argparse.Namespace) -> int:
    ref: str = args.ref.strip()
    try:
        result = _resolve_source_text(ref)
    except (KeyError, FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"Source:         {result['source_url']}")
        print(f"Anchor:         {result['anchor']}")
        print(f"Classification: {result['classification']}")
        if result.get("verify_tag"):
            print("Tag:            [VERIFY] (web-fetched)")
        print(f"Bytes:          {result['byte_start']}–{result['byte_end']}")
        print("-" * 60)
        print(result["text"])
    return 0


# --- subcommand: extract-facts ----------------------------------------------

_ISSUE_RE = re.compile(r"^(?P<owner>[^/]+)/(?P<repo>[^#]+)#(?P<num>\d+)$")


def _load_facts(issue_ref: str) -> dict[str, Any]:
    """Load (and cache) a GitHub issue's body + comments. Reusable by build/screenshot."""
    if not _have("gh"):
        raise RuntimeError("gh CLI not found.")
    m = _ISSUE_RE.match(issue_ref.strip())
    if not m:
        raise ValueError("issue ref must look like 'owner/repo#123'")
    owner, repo, num = m.group("owner"), m.group("repo"), m.group("num")
    _ensure_dirs()
    cached = FACTS_DIR / f"{owner}__{repo}__{num}.json"
    if cached.exists():
        return json.loads(cached.read_text(encoding="utf-8"))

    api_path = f"repos/{owner}/{repo}/issues/{num}"
    issue = json.loads(_run(["gh", "api", api_path]).stdout)
    comments = json.loads(_run(["gh", "api", f"{api_path}/comments"]).stdout)
    facts = {
        "ref": f"{owner}/{repo}#{num}",
        "url": issue.get("html_url"),
        "title": issue.get("title"),
        "author": issue.get("user", {}).get("login"),
        "created_at": issue.get("created_at"),
        "state": issue.get("state"),
        "labels": [lbl.get("name") for lbl in issue.get("labels", [])],
        "body": issue.get("body") or "",
        "comments": [
            {
                "id": c.get("id"),
                "author": c.get("user", {}).get("login"),
                "created_at": c.get("created_at"),
                "url": c.get("html_url"),
                "body": c.get("body") or "",
            }
            for c in comments
        ],
    }
    cached.write_text(json.dumps(facts, indent=2), encoding="utf-8")
    return facts


def cmd_extract_facts(args: argparse.Namespace) -> int:
    try:
        facts = _load_facts(args.issue)
    except (RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 3 if "gh CLI" in str(e) else 1
    except subprocess.CalledProcessError as e:
        print(f"error: gh api failed — {e.stderr.strip()}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(facts, indent=2))
    else:
        print(f"Issue:    {facts['ref']}  ({facts['url']})")
        print(f"Title:    {facts['title']}")
        print(f"Author:   {facts['author']}  ({facts['created_at']})")
        print(f"Labels:   {', '.join(facts['labels']) or '(none)'}")
        print(f"Comments: {len(facts['comments'])}")
        m = _ISSUE_RE.match(args.issue.strip())
        if m:
            owner, repo, num = m.group("owner"), m.group("repo"), m.group("num")
            print(f"Cached:   {FACTS_DIR / f'{owner}__{repo}__{num}.json'}")
    return 0


# --- subcommand: verify ------------------------------------------------------

def _normalize_for_match(s: str) -> str:
    """Light normalization to absorb whitespace differences without weakening
    the byte-for-byte intent. Collapses runs of whitespace; keeps everything
    else untouched. Tightens to true byte-for-byte if --strict is passed."""
    return re.sub(r"\s+", " ", s).strip()


def cmd_verify(args: argparse.Namespace) -> int:
    quote: str = args.quote
    ref: str = args.source.strip()

    try:
        source = _resolve_source_text(ref)
    except (KeyError, FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    haystack = source["text"]
    if args.strict:
        ok = quote in haystack
    else:
        ok = _normalize_for_match(quote) in _normalize_for_match(haystack)

    payload = {
        "ok": ok,
        "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        "source": source["source_url"],
        "anchor": source["anchor"],
        "classification": source["classification"],
        "verify_required": bool(source.get("verify_tag")),
        "mode": "strict" if args.strict else "whitespace-normalized",
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    elif ok:
        print(f"✓ verified  ({payload['mode']})  {source['source_url']} :: {source['anchor']}")
        if payload["verify_required"]:
            print("  note: source was web-fetched — append [VERIFY] tag in the citation card.")
    else:
        print(f"✗ NOT FOUND in source  ({payload['mode']})")
        print(f"  source:   {source['source_url']}")
        print(f"  anchor:   {source['anchor']}")
        print(f"  quote:    {quote[:120]}{'...' if len(quote) > 120 else ''}")
        print("  refusal:  cite-check will not emit a card with an unverified quote.")
    return 0 if ok else 2


# --- screenshot rendering ---------------------------------------------------
#
# eyeball-style: render the source to PDF (Playwright headless Chromium),
# search the rendered PDF for the verbatim quote with fitz.search_for, crop a
# region around the hit with padding, draw a yellow highlight rectangle, and
# emit a PNG. Two source types are supported:
#
#   1. cached corpus markdown — converted to HTML with print-friendly CSS
#      so headings, tables, and lists render legibly in the PDF.
#   2. product facts (issue body or a single comment) — wrapped in a
#      GitHub-like comment card so the screenshot is recognizably "an issue
#      comment from @author on date X".
#
# Rendered PDFs are cached under ~/.copilot/skills/cite-check/cache/rendered/
# keyed by a hash of (renderer_kind, source_ref, content_hash).

_DOC_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
          font-size: 11pt; line-height: 1.55; color: #1f2328;
          padding: 24px 8px; max-width: 760px; }}
  h1, h2, h3, h4, h5, h6 {{ font-weight: 600; line-height: 1.25; }}
  h1 {{ font-size: 22pt; padding-bottom: 6px; border-bottom: 1px solid #d0d7de; }}
  h2 {{ font-size: 17pt; padding-bottom: 4px; border-bottom: 1px solid #d0d7de; margin-top: 22pt; }}
  h3 {{ font-size: 14pt; margin-top: 16pt; }}
  h4, h5, h6 {{ font-size: 12pt; margin-top: 12pt; }}
  p, li {{ margin: 0 0 0.6em 0; }}
  pre, code {{ font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 9.5pt; }}
  pre {{ background: #f6f8fa; padding: 10px; border-radius: 6px; overflow-x: auto; }}
  blockquote {{ margin: 0 0 1em 0; padding: 0 1em; color: #57606a; border-left: 3px solid #d0d7de; }}
  table {{ border-collapse: collapse; }}
  td, th {{ border: 1px solid #d0d7de; padding: 4px 8px; }}
  .source-banner {{ background: #ddf4ff; border: 1px solid #54aeff; border-radius: 6px;
                    padding: 8px 12px; margin-bottom: 16px; font-size: 9.5pt; color: #0969da; }}
</style></head><body>
{banner}
{content}
</body></html>"""

_COMMENT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
          font-size: 11pt; line-height: 1.55; color: #1f2328;
          padding: 24px 8px; max-width: 760px; }}
  .issue-banner {{ background: #ddf4ff; border: 1px solid #54aeff; border-radius: 6px;
                   padding: 10px 14px; margin-bottom: 16px; }}
  .issue-banner .title {{ font-weight: 600; font-size: 13pt; color: #1f2328; }}
  .issue-banner .meta {{ font-size: 9.5pt; color: #0969da; margin-top: 4px; }}
  .comment {{ border: 1px solid #d0d7de; border-radius: 6px; margin-bottom: 16px; }}
  .comment-header {{ background: #f6f8fa; padding: 10px 14px;
                     border-bottom: 1px solid #d0d7de; font-size: 10pt; color: #57606a; }}
  .comment-author {{ color: #0969da; font-weight: 600; }}
  .comment-body {{ padding: 14px; }}
  .comment-body p, .comment-body li {{ margin: 0 0 0.6em 0; }}
  pre, code {{ font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 9.5pt; }}
  pre {{ background: #f6f8fa; padding: 10px; border-radius: 6px; }}
</style></head><body>
{banner}
{content}
</body></html>"""


def _markdown_to_html(md_text: str) -> str:
    try:
        import markdown
    except ImportError:
        from html import escape
        return f"<pre>{escape(md_text)}</pre>"
    return markdown.markdown(
        md_text,
        extensions=["fenced_code", "tables", "sane_lists"],
    )


def _render_html_to_pdf(html: str, pdf_path: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed. Run setup.sh / setup.ps1, then "
            "`python3 -m playwright install chromium`."
        )
    import tempfile
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as tf:
        tf.write(html)
        html_path = Path(tf.name)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(f"file://{html_path}", wait_until="load", timeout=30000)
                page.pdf(
                    path=str(pdf_path),
                    format="Letter",
                    print_background=True,
                    margin={"top": "0.5in", "bottom": "0.5in", "left": "0.6in", "right": "0.6in"},
                )
            finally:
                browser.close()
    finally:
        html_path.unlink(missing_ok=True)


def _render_url_to_pdf(url: str, pdf_path: Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed. Run setup.sh / setup.ps1, then "
            "`python3 -m playwright install chromium`."
        )
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.evaluate(
                "document.querySelectorAll("
                "'header, footer, nav, .header, .footer, .nav, "
                "[data-testid=\"header\"], [data-testid=\"footer\"], "
                ".cookie-banner, .cookie-consent').forEach(el => el.remove());"
            )
            page.pdf(
                path=str(pdf_path),
                format="Letter",
                print_background=True,
                margin={"top": "0.5in", "bottom": "0.5in", "left": "0.6in", "right": "0.6in"},
            )
        finally:
            browser.close()


def _render_corpus_section_pdf(filename: str, anchor: str | None) -> tuple[Path, dict[str, Any]]:
    section = _resolve_corpus_section(filename, anchor)
    key = _hash_key(
        "corpus", filename, section["anchor"],
        str(section["byte_start"]), str(section["byte_end"]),
    )
    pdf_path = RENDERED_DIR / f"{key}.pdf"
    if not pdf_path.exists():
        banner = (
            f'<div class="source-banner">📄 <strong>{filename}</strong> · '
            f'{section["anchor"]} · classification: {section["classification"]}</div>'
        )
        content = _markdown_to_html(section["text"])
        html = _DOC_HTML.format(
            title=f"{filename} — {section['anchor']}",
            banner=banner, content=content,
        )
        _render_html_to_pdf(html, pdf_path)
    return pdf_path, section


def _render_url_pdf(url: str) -> tuple[Path, dict[str, Any]]:
    key = _hash_key("url", url)
    pdf_path = RENDERED_DIR / f"{key}.pdf"
    if not pdf_path.exists():
        _render_url_to_pdf(url, pdf_path)
    return pdf_path, {"source_url": url, "classification": "verify-required"}


_COMMENT_REF_RE = re.compile(r"^(?P<owner>[^/]+)/(?P<repo>[^#]+)#(?P<num>\d+)(?:::comment_(?P<cid>\d+))?$")


def _render_issue_comment_pdf(source_ref: str) -> tuple[Path, dict[str, Any]]:
    m = _COMMENT_REF_RE.match(source_ref.strip())
    if not m:
        raise ValueError(f"Unrecognized issue source ref: {source_ref}")
    owner, repo, num, comment_id = m.group("owner"), m.group("repo"), m.group("num"), m.group("cid")
    facts = _load_facts(f"{owner}/{repo}#{num}")

    if comment_id:
        comment = next(
            (c for c in facts.get("comments", []) if str(c.get("id")) == comment_id),
            None,
        )
        if not comment:
            raise KeyError(f"Comment {comment_id} not found in {source_ref}")
        body_md = comment.get("body", "")
        author = comment.get("author", "(unknown)")
        timestamp = comment.get("created_at", "")
        item_url = comment.get("url", facts.get("url"))
        item_label = "Comment"
    else:
        body_md = facts.get("body", "") or ""
        author = facts.get("author", "(unknown)")
        timestamp = facts.get("created_at", "")
        item_url = facts.get("url")
        item_label = "Issue body"

    body_hash = hashlib.sha256(body_md.encode("utf-8")).hexdigest()
    key = _hash_key("issue", source_ref, body_hash)
    pdf_path = RENDERED_DIR / f"{key}.pdf"

    if not pdf_path.exists():
        banner_html = (
            f'<div class="issue-banner">'
            f'<div class="title">{facts.get("title", "")}</div>'
            f'<div class="meta">{facts["ref"]} · {item_label} · {timestamp}<br>'
            f'<a href="{item_url}">{item_url}</a></div>'
            f'</div>'
        )
        content_html = (
            f'<div class="comment">'
            f'<div class="comment-header"><span class="comment-author">@{author}</span> '
            f'commented on {timestamp}</div>'
            f'<div class="comment-body">{_markdown_to_html(body_md)}</div>'
            f'</div>'
        )
        html = _COMMENT_HTML.format(title=source_ref, banner=banner_html, content=content_html)
        _render_html_to_pdf(html, pdf_path)

    return pdf_path, {
        "source_url": item_url,
        "classification": "github-public",
        "ref": source_ref,
    }


def _render_source_pdf(source_ref: str) -> tuple[Path, dict[str, Any]]:
    """Dispatcher: chooses the right renderer based on source_ref shape."""
    ref = source_ref.strip()
    if ref.startswith(("http://", "https://")):
        return _render_url_pdf(ref)
    if _COMMENT_REF_RE.match(ref):
        return _render_issue_comment_pdf(ref)
    if "#" in ref:
        filename, anchor = ref.split("#", 1)
        return _render_corpus_section_pdf(filename.strip(), anchor.strip())
    if ref.endswith(".md"):
        return _render_corpus_section_pdf(ref, None)
    if ref.startswith("manual:"):
        raise ValueError(
            "manual: source refs cannot be screenshotted yet — "
            "save the raw text to a markdown file in the corpus and refresh-corpus."
        )
    raise ValueError(f"Unrecognized source_ref shape: {ref}")


def _quote_search_candidates(quote: str) -> list[str]:
    """Progressively shorter search strings — falls back when the full quote
    doesn't match because of soft hyphens, smart quotes, or wrapping."""
    quote = quote.strip()
    candidates = [quote]
    sentences = [s.strip() for s in re.split(r"(?<=[.;])\s+", quote) if len(s.strip()) > 20]
    candidates.extend(sentences)
    if len(quote) > 80:
        head = quote[:80].rsplit(" ", 1)[0]
        if head and head not in candidates:
            candidates.append(head)
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _render_pdf_region(doc, pg_idx: int, hits: list, context_padding: int, zoom: float):
    import fitz
    from PIL import Image, ImageDraw
    page = doc[pg_idx]
    page_rect = page.rect
    all_rects = [h for _, h in hits]
    min_y = min(r.y0 for r in all_rects)
    max_y = max(r.y1 for r in all_rects)
    crop_rect = fitz.Rect(
        page_rect.x0 + 12,
        max(page_rect.y0, min_y - context_padding),
        page_rect.x1 - 12,
        min(page_rect.y1, max_y + context_padding),
    )
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, clip=crop_rect)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    draw = ImageDraw.Draw(img, "RGBA")
    pad = max(2, round(2 * zoom))
    for _anchor, rect in hits:
        if rect.y0 >= crop_rect.y0 - 5 and rect.y1 <= crop_rect.y1 + 5:
            x0 = (rect.x0 - crop_rect.x0) * zoom
            y0 = (rect.y0 - crop_rect.y0) * zoom
            x1 = (rect.x1 - crop_rect.x0) * zoom
            y1 = (rect.y1 - crop_rect.y0) * zoom
            draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad], fill=(255, 255, 0, 110))
    ImageDraw.Draw(img).rectangle(
        [0, 0, img.width - 1, img.height - 1], outline=(160, 160, 160), width=2,
    )
    return img


def _screenshot_quote(pdf_path: Path, quote: str, png_path: Path,
                      context_padding: int = 30, dpi: int = 200) -> dict[str, Any]:
    """Find the verbatim quote in the rendered PDF and write a highlighted PNG."""
    try:
        import fitz
        from PIL import Image, ImageDraw
    except ImportError:
        return {"ok": False, "reason": "pymupdf or pillow not installed (run setup.sh)"}

    doc = fitz.open(str(pdf_path))
    try:
        candidates = _quote_search_candidates(quote)
        page_hits: dict[int, list] = {}
        anchor_used: str | None = None
        tried: list[str] = []
        for anchor in candidates:
            tried.append(anchor[:80])
            for pg_idx in range(doc.page_count):
                page = doc[pg_idx]
                hits = page.search_for(anchor)
                if hits:
                    page_hits.setdefault(pg_idx, []).extend([(anchor, h) for h in hits])
            if page_hits:
                anchor_used = anchor
                break

        if not page_hits:
            return {
                "ok": False,
                "reason": "quote text not found in rendered source",
                "anchors_tried": tried[:5],
            }

        zoom = dpi / 72
        pages_used = sorted(page_hits.keys())
        images = [
            _render_pdf_region(doc, pg, page_hits[pg], context_padding, zoom)
            for pg in pages_used
        ]
        if len(images) == 1:
            final_img = images[0]
        else:
            total_h = sum(im.height for im in images) + 6 * (len(images) - 1)
            max_w = max(im.width for im in images)
            final_img = Image.new("RGB", (max_w, total_h), (255, 255, 255))
            y = 0
            for im in images:
                final_img.paste(im, (0, y))
                y += im.height + 6
            ImageDraw.Draw(final_img).rectangle(
                [0, 0, final_img.width - 1, final_img.height - 1],
                outline=(160, 160, 160), width=2,
            )
        png_path.parent.mkdir(parents=True, exist_ok=True)
        final_img.save(png_path, format="PNG")
        return {
            "ok": True,
            "png": str(png_path),
            "pages": [p + 1 for p in pages_used],
            "anchor_used": anchor_used[:80] if anchor_used else None,
            "size": list(final_img.size),
        }
    finally:
        doc.close()


def _screenshot_for_quote(quote: str, source_ref: str, png_path: Path) -> dict[str, Any]:
    """End-to-end: render source PDF (cached), then crop+highlight PNG for quote."""
    pdf_path, meta = _render_source_pdf(source_ref)
    result = _screenshot_quote(pdf_path, quote, png_path)
    result["source_pdf"] = str(pdf_path)
    result["source_meta"] = meta
    return result


# --- subcommand: screenshot --------------------------------------------------

def cmd_screenshot(args: argparse.Namespace) -> int:
    out = Path(args.output).expanduser().resolve()
    try:
        result = _screenshot_for_quote(args.quote, args.source, out)
    except (ValueError, KeyError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    elif result.get("ok"):
        print(f"✓ Wrote {out}  (pages: {result.get('pages')}, size: {result.get('size')})")
    else:
        print(f"✗ {result.get('reason')}", file=sys.stderr)
        if "anchors_tried" in result:
            for a in result["anchors_tried"]:
                print(f"    tried: {a!r}", file=sys.stderr)
    return 0 if result.get("ok") else 2


# --- subcommand: build -------------------------------------------------------

@dataclasses.dataclass
class CardQuote:
    label: str          # e.g. "LEGAL PROVISION", "GITHUB COMMITMENT", "PRODUCT FACT"
    quote: str          # verbatim text
    source_ref: str     # filename.md#Anchor or URL or owner/repo#N::comment_id
    classification: str  # public-law | public-guidance | github-public | github-internal | verify-required
    verified: bool      # set true only after cmd_verify succeeded


def _risk_emoji(tier: str) -> str:
    return {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(tier.lower(), "⚪")


def cmd_build(args: argparse.Namespace) -> int:
    try:
        from docx import Document
        from docx.shared import Pt, Inches, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        print("error: python-docx not installed. Run setup.sh / setup.ps1.", file=sys.stderr)
        return 3

    cards_path = Path(args.cards).expanduser().resolve()
    if not cards_path.exists():
        print(f"error: cards file not found: {cards_path}", file=sys.stderr)
        return 1
    cards = json.loads(cards_path.read_text(encoding="utf-8"))

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    doc = Document()
    title = doc.add_heading(cards.get("title", "Citation Cards"), level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT
    if subtitle := cards.get("subtitle"):
        para = doc.add_paragraph(subtitle)
        para.runs[0].italic = True

    doc.add_paragraph(
        "This document was generated by cite-check. Each Citation Card anchors a "
        "flagged risk in verbatim quotes from the cited legal source(s) and the "
        "product fact. Quotes shown were byte-for-byte verified against their "
        "sources before this document was written, then captured from the rendered "
        "source with the cited language highlighted in yellow. Quotes tagged "
        "[VERIFY] were fetched from the public web and should be confirmed against "
        "the canonical source. This is a drafting aid — not final legal advice."
    )
    doc.add_paragraph()

    risks = cards.get("risks") or []
    refusals: list[str] = []
    render_failures: list[str] = []
    no_screenshot = bool(args.no_screenshots)
    img_width = Inches(float(args.image_width)) if args.image_width else Inches(6.0)

    for i, risk in enumerate(risks, start=1):
        tier = risk.get("tier", "medium")
        heading = doc.add_heading(
            f"{_risk_emoji(tier)} RISK {i} of {len(risks)} — {risk.get('summary', 'Untitled risk')}",
            level=1,
        )
        heading.runs[0].font.color.rgb = RGBColor(0x20, 0x20, 0x20)

        for q in risk.get("quotes", []):
            if not q.get("verified"):
                refusals.append(
                    f"Risk {i} ({risk.get('summary', '')}) — quote labeled '{q.get('label')}' "
                    f"is not marked verified. Run `cite.py verify` first and set verified=true."
                )
                continue

            block = doc.add_paragraph()
            run = block.add_run(f"[{q.get('label', 'SOURCE').upper()}]")
            run.bold = True
            run.font.size = Pt(10)

            tag_bits = []
            if q.get("classification") == "verify-required":
                tag_bits.append("[VERIFY]")
            if q.get("classification") == "github-internal":
                tag_bits.append("[INTERNAL CONTEXT — background only]")
            if tag_bits:
                tag_run = block.add_run("   " + "  ".join(tag_bits))
                tag_run.italic = True
                tag_run.font.size = Pt(9)

            screenshot_ok = False
            source_ref = q.get("source_ref", "")
            if not no_screenshot and source_ref:
                png_path = RENDERED_DIR / "shots" / f"risk{i}_{_hash_key(source_ref, q.get('quote',''))}.png"
                try:
                    print(f"  rendering screenshot: risk {i} · {q.get('label','')} · {source_ref[:60]}", file=sys.stderr)
                    shot = _screenshot_for_quote(q.get("quote", ""), source_ref, png_path)
                    if shot.get("ok"):
                        pic_para = doc.add_paragraph()
                        pic_para.add_run().add_picture(str(png_path), width=img_width)
                        screenshot_ok = True
                    else:
                        render_failures.append(
                            f"Risk {i} · {q.get('label','')} · {source_ref}: {shot.get('reason')}"
                        )
                except Exception as e:  # noqa: BLE001 — render failure must not abort the whole build
                    render_failures.append(
                        f"Risk {i} · {q.get('label','')} · {source_ref}: {type(e).__name__}: {e}"
                    )

            if not screenshot_ok:
                quote_para = doc.add_paragraph(style="Intense Quote")
                quote_para.add_run(q.get("quote", "").strip())
                if not no_screenshot and source_ref:
                    note = doc.add_paragraph()
                    note_run = note.add_run("(screenshot unavailable — verbatim text shown above)")
                    note_run.italic = True
                    note_run.font.size = Pt(8)
            else:
                # Always print the verbatim text below the screenshot for searchability
                vt = doc.add_paragraph()
                vt_label = vt.add_run("Verbatim quote: ")
                vt_label.bold = True
                vt_label.font.size = Pt(9)
                vt_text = vt.add_run(q.get("quote", "").strip())
                vt_text.font.size = Pt(9)

            src_para = doc.add_paragraph()
            src_run = src_para.add_run(f"Source: {source_ref or '(unknown)'}")
            src_run.italic = True
            src_run.font.size = Pt(9)

        if nexus := risk.get("nexus"):
            p = doc.add_paragraph()
            p.add_run("[NEXUS] ").bold = True
            p.add_run(nexus)

        if action := risk.get("action"):
            p = doc.add_paragraph()
            p.add_run("[RECOMMENDED ACTION] ").bold = True
            p.add_run(action)

        doc.add_paragraph()
        doc.add_paragraph("─" * 60)

    if refusals or render_failures:
        doc.add_page_break()
        if refusals:
            doc.add_heading("Refusals (verification not satisfied)", level=1)
            for r in refusals:
                doc.add_paragraph(r, style="List Bullet")
        if render_failures:
            doc.add_heading("Screenshot render notes", level=1)
            for r in render_failures:
                doc.add_paragraph(r, style="List Bullet")
        if refusals:
            print("warning: some quotes were not verified — see Refusals page in the output document.", file=sys.stderr)
            if args.strict:
                print("strict mode: refusing to write output with unverified quotes.", file=sys.stderr)
                return 2

    doc.save(output)
    print(f"✓ Wrote {output}")
    print(f"  risks: {len(risks)}, refusals: {len(refusals)}, render notes: {len(render_failures)}")
    return 0


# --- entrypoint --------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cite", description=__doc__.strip().splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup-check", help="verify Python deps and gh CLI")
    sp.set_defaults(func=cmd_setup_check)

    sp = sub.add_parser("refresh-corpus", help="mirror the legal reference library into the local cache")
    sp.add_argument("--only", nargs="*", help="optional subset of filenames to refresh")
    sp.set_defaults(func=cmd_refresh_corpus)

    sp = sub.add_parser("list-corpus", help="show the current cache state")
    sp.add_argument("--file", help="show every section anchor in this cached file")
    sp.set_defaults(func=cmd_list_corpus)

    sp = sub.add_parser("extract-source", help="resolve a citation reference to verbatim text")
    sp.add_argument("--ref", required=True, help="e.g. '03-reg-eu-gdpr.md#Article 6' or a public URL")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_extract_source)

    sp = sub.add_parser("extract-facts", help="pull a GitHub issue (body + comments) into a structured fact corpus")
    sp.add_argument("--issue", required=True, help="owner/repo#N")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_extract_facts)

    sp = sub.add_parser("verify", help="byte-for-byte check that a quote appears in a source (exit 2 if not)")
    sp.add_argument("--quote", required=True)
    sp.add_argument("--source", required=True, help="filename.md#Anchor or URL")
    sp.add_argument("--strict", action="store_true", help="exact byte match (default: whitespace-normalized)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("screenshot", help="render one quote → highlighted PNG (for testing/debugging)")
    sp.add_argument("--quote", required=True)
    sp.add_argument("--source", required=True, help="filename.md#Anchor, owner/repo#N[::comment_<id>], or URL")
    sp.add_argument("--output", required=True, help="output .png path")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_screenshot)

    sp = sub.add_parser("build", help="render Citation Cards to a Word document")
    sp.add_argument("--cards", required=True, help="path to a JSON cards file (see SKILL.md for schema)")
    sp.add_argument("--output", required=True, help="output .docx path")
    sp.add_argument("--strict", action="store_true", help="refuse to write output if any quotes are unverified")
    sp.add_argument("--no-screenshots", action="store_true", help="skip screenshot rendering; emit text-only quote blocks")
    sp.add_argument("--image-width", type=float, default=6.0, help="screenshot width in inches (default: 6.0)")
    sp.set_defaults(func=cmd_build)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
