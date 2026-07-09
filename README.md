# Scholar Report — `scholar_report.py`

Given a list of researchers and a list of keywords, search free scholarly APIs, collect
each author's most-cited keyword-relevant papers plus a total hit count, verify
titles/journals against the live landing page, and emit a compilable LaTeX report.

### What it does

For each author the script:

1. **Resolves the name to one author profile** on **OpenAlex**, requiring a
   **lastname + first-initial** match. It captures the author's **ORCID** (keyless, from
   OpenAlex). If several candidates match, it ranks them (exact first name, then ORCID,
   then keyword/concept fit, then works count) and — when run in an interactive terminal —
   **shows a numbered menu and asks which author to use**; in a non-interactive run it
   auto-picks the top candidate and prints a warning. Semantic Scholar is queried as
   supplementary coverage.
2. **Pulls works** from **OpenAlex** (also reads `meta.count` as the author's
   *total hits*), **Semantic Scholar**, and **arXiv** (preprints), capturing each paper's
   title and abstract.
3. **Merges + dedups** across sources by DOI (fallback: normalized title), keeping the
   maximum citation count.
3b. **Authorship harness.** Every merged paper must actually list the queried author by
   **lastname + first initial** (with ORCID as a tie-breaker when a source supplies one);
   wrong-person papers — e.g. an *Andrei* Constantinescu preprint surfacing under *Emil*
   Constantinescu — are dropped and counted (`spurious_dropped`). The arXiv and Semantic
   Scholar lookups apply the same lastname+initial test at the source.
4. **Keyword matching (title/abstract only).** For each paper it records where each
   keyword was found — `title` or `abstract` (the full text/body is **never** searched).
   Papers with **no** keyword match in their title or abstract are **dropped**. An
   optional **publication-year range** further filters the papers.
5. **Ranks title-hits before abstract-hits, then by citation count.** For the top
   candidates it **verifies** each by fetching the DOI/landing page and fuzzy-matching the
   title (and journal, when present) against the API metadata — dropping/flagging
   mismatches — until *N* verified papers are collected.
6. Emits a LaTeX report (`report.tex`) and a sidecar `report.json`.

> **Why these sources?** The originally requested Google Scholar (no API, blocks scraping)
> and Web of Science (paid key) cannot be queried freely/reliably for citation counts, so
> the tool uses OpenAlex + Semantic Scholar + arXiv — all free, no keys, with real
> citation counts.

### Dependencies

Only one third-party package; everything else is the Python standard library.

```bash
pip install -r requirements.txt    # installs: requests
```

LaTeX (`pdflatex`) is needed to compile the generated report.

### Usage

```bash
python3 scholar_report.py authors.txt keywords.txt \
    -n 5 \
    -o report.tex \
    --mailto leyffer@anl.gov \
    --cache .scholar_cache

pdflatex report.tex    # run twice (manual thebibliography; no bibtex/biber needed)
```

#### Arguments and options

| Argument / option        | Default                | Description                                                        |
|--------------------------|------------------------|--------------------------------------------------------------------|
| `authors` (positional)   | —                      | Input file: one `Lastname Firstname` per line                      |
| `keywords` (positional)  | —                      | Input file: one keyword/phrase per line                            |
| `-n`, `--nresults`       | `5`                    | Most-cited papers to keep per author                               |
| `-o`, `--output`         | `scholar_report.tex`   | Output `.tex` path (a sibling `.json` is also written)             |
| `--year-min`             | _none (all)_           | Earliest publication year to include                               |
| `--year-max`             | _none (all)_           | Latest publication year to include                                 |
| `--mailto`               | `leyffer@anl.gov`      | Email for the OpenAlex "polite pool"                               |
| `--cache`                | _none_                 | Directory for cached API/HTTP responses (fast, rate-limit-safe re-runs) |
| `--no-verify`            | off                    | Skip landing-page title/journal verification (faster, debugging)   |
| `--max-candidates`       | `20`                   | Top-cited papers to fetch/verify before settling on *N*            |
| `--sleep`                | `1.0`                  | Min seconds between requests **to the same host** (per-host throttle; raise to avoid 429s) |

### File Conventions: Input & Output

**Input — `authors.txt`** (one author per line, `Lastname Firstname`; blank lines and
`#` comments are ignored):

```
Leyffer Sven
Wright Stephen
```

**Input — `keywords.txt`** (one keyword or phrase per line; matched as case-insensitive
substrings, combined with OR):

```
mixed integer
optimization
```

**Output — `report.tex`**: a standalone LaTeX document (letterpaper, 1in margins). The
header lists the **keywords** and the **publication-year range**, and notes the ranking
rule. Then, per author, a `\section*{Lastname, Firstname}` header, the author's **ORCID**
(linked), a total-hits line, and
an `itemize` list of the top-*N* papers — each showing title, venue, year, citation count,
**which keyword was matched and where** (`Matched: keyword (title|abstract)`), and an
`\href` link — followed by a single combined, deduplicated, hyperlinked
`thebibliography`. Papers that could not be fully verified against their landing page are
flagged with a dagger (†).

**Output — `report.json`**: a sidecar dump of the run (`keywords`, `year_range`, and per
author the `canonical_name`, `orcid`, total hits, `spurious_dropped` (wrong-author papers
removed by the authorship harness), and the kept and dropped papers, each with its
`matches` and `match_location`) for reproducibility and inspection without re-querying.

### Notes

- **Rate limits:** the key-less endpoints rate-limit (HTTP 429). The script (1) **throttles
  proactively** — at least `--sleep` seconds between requests *to the same host* — to avoid
  the bursts that trigger 429s, and (2) on a 429/5xx retries with backoff that **starts at
  4 s, doubles over 5 retries (4→8→16→32→64 s), capped at 120 s** (a sane `Retry-After` is
  honored; absurd values ignored). Each warning names the host, e.g.
  `Warning: HTTP 429 from api.openalex.org ...`, and a startup line reports whether the S2
  key and OpenAlex mailto were detected.
- **Which service is throttling matters:**
  - **OpenAlex** (the *first* call per author) uses the free **polite pool** via
    `--mailto <your email>` (already default). There is no free "raise my limit" key. If
    you get a burst of `... from api.openalex.org` 429s right away, your IP is in a
    short cool-down — wait a few minutes, keep `--cache`, and raise `--sleep`.
  - **Semantic Scholar**: set a free **`S2_API_KEY`** env var (sent as `x-api-key`) to
    raise its limits. **This key does *not* affect OpenAlex.**
- Using `--cache` serves prior responses from disk (0 API calls on re-run) and is the
  single most effective way to avoid limits. If a host stays throttled, that author falls
  back to the other sources (OpenAlex failure → no ORCID).
- **Author disambiguation:** matching requires lastname + first initial. Run in a terminal
  to get the interactive selection menu when a name is ambiguous; non-interactive runs
  auto-pick the top candidate and warn.
- **Robustness:** network/source failures degrade gracefully — one dead source warns and
  is skipped, never aborting the run.
- **Verification:** papers failing verification are marked † in the report and recorded
  under `dropped` in the JSON rather than silently discarded.
- **Keyword matching is title/abstract only** — the full text/body is never searched, and
  papers with no title/abstract keyword match are excluded. Ranking always places
  title-hits above abstract-hits before considering citation count.

See `../plans/2026-06-26-scholar_report.md` for the full design rationale and the testing
performed, and `../STATUS.md`/`../MEMORY.md` for session history.
