"""
Chunk quality classification — telling a book's content from its packaging.

The problem this solves, concretely. Asked "what is <the book's topic>?", the
retriever returned six passages and every one of them was front matter: the
jacket's endorsements, saying the topic's name in every sentence.

A jacket blurb is the worst passage in the book for answering that question and
simultaneously its strongest lexical match, because saying the topic phrase as
often as possible is the entire job of a blurb. The same holds for prefaces,
tables of contents, and — at the other end — bibliographies, where a page of
`[15.31] D.B. Rawat, S.R. R..., IEEE MST.2016.2633579` matches any query built
from the same vocabulary as the field it cites.

So each chunk is classified at ingest and the label is stored as metadata:

    body            real content, the default retrieval surface
    front_matter    cover, endorsements, foreword, preface, TOC, index pages
    reference_list  bibliography and citation-dense pages

Classification is metadata, not deletion. The chunks stay in the index and the
retriever filters on the label, so changing the policy later is a query-side
change and not a five-thousand-chunk re-embed.

Two signals, deliberately independent:

  - **Position.** Front matter is at the front. A fixed page cut-off is wrong
    across a 60-page paper and a 1182-page handbook, so it is a fraction of the
    document with an absolute floor and ceiling.
  - **Shape.** Position alone mislabels a short paper's first page, and misses
    a bibliography entirely. Blurbs, TOC lines and citation lists each have a
    recognisable texture that position cannot see.

A chunk needs only one signal to be labelled. Precision matters less than it
looks: a body page wrongly labelled front matter costs one passage out of
thousands, while one blurb reaching the model costs an answer.
"""
import re
from typing import Optional

# ----- position -----

# Front matter as a share of the document, bounded at both ends. 4% is ~2 pages
# of a 60-page paper (right: title + abstract are not body prose to retrieve as
# such) and ~47 of the Springer handbook (right: its body starts around p.50).
FRONT_MATTER_FRACTION = 0.04
FRONT_MATTER_MIN_PAGES = 2      # even a short paper has a title page
FRONT_MATTER_MAX_PAGES = 60     # never write off more than this, whatever the length

# Below this, a document has no meaningful front matter to speak of.
MIN_PAGES_FOR_POSITION_RULE = 12

# ----- shape -----

# A passage this short is a heading, a caption or a stub. It cannot answer a
# conceptual question no matter how well it matches one.
MIN_BODY_CHARS = 250

# Citation markers: [15], [15.31], (Smith et al., 2019), DOIs, arXiv ids.
_CITATION_MARKER = re.compile(
    r"\[\d+(?:\.\d+)?\]"
    r"|\b10\.\d{4,}/\S+"
    r"|\barXiv:\d{4}\.\d{4,}"
    r"|\bdoi:\s*\S+"
    r"|\bet\s+al\.",
    re.IGNORECASE,
)
# "D.B. Rawat", "R. Ramaswami" — initial-heavy author names, the giveaway of a
# reference list even when the citation numbering was lost in extraction.
_AUTHOR_INITIALS = re.compile(r"\b(?:[A-Z]\.\s*){1,3}[A-Z][a-z]+")
# Journal/conference boilerplate that only appears in bibliographies.
_BIB_BOILERPLATE = re.compile(
    r"\b(?:pp\.\s*\d+|vol\.\s*\d+|no\.\s*\d+|IEEE\s+(?:J|Trans|Commun|Photon)"
    r"|Proc\.|Opt\.\s*Express|J\.\s*Lightwave)",
    re.IGNORECASE,
)

# Front-matter vocabulary. Matched near the start of a chunk, because "in the
# preface we argued" mid-chapter is body text discussing a preface.
_FRONT_MATTER_HEADING = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)?"          # the [source p.N] provenance header
    r"(?:preface|foreword|contents|table\s+of\s+contents|acknowledg(?:e)?ments"
    r"|about\s+the\s+authors?|praise\s+for|advance\s+praise|copyright"
    r"|dedication|list\s+of\s+(?:figures|tables|acronyms|abbreviations)"
    r"|index|bibliography|references|further\s+reading)\b",
    re.IGNORECASE,
)

# An endorsement is prose that names its endorser: a title and an employer,
# usually after a dash. This is what a jacket blurb has that a chapter doesn't.
_ENDORSEMENT = re.compile(
    r"(?:—|--|-{2,}|—)\s*[A-Z][A-Za-z.\- ]+,\s*"
    r"(?:[A-Z][A-Za-z.\- ]*(?:Director|Professor|Chief|President|VP|Vice President"
    r"|Manager|Fellow|Editor|Chair|Officer|Scientist|Engineer)[A-Za-z.\- ]*|.{0,40}"
    r"(?:University|Institute|Laboratories|Labs|Technologies|Corporation|Inc\.))",
)
_BLURB_VOCABULARY = re.compile(
    r"\b(?:this book|the book|the authors|the text|excellent overview|must[- ]read"
    r"|highly recommend|within easy reach|indispensable|comprehensive treatment"
    r"|belongs on the (?:shelf|desk)|reference again and again)\b",
    re.IGNORECASE,
)

# A table of contents is many short lines ending in a page number, or strung
# together with dot leaders.
_TOC_LINE = re.compile(r"^\s*\S.{0,70}?(?:\.{3,}|\s{2,})\s*\d{1,4}\s*$")
_TOC_NUMBERED = re.compile(r"^\s*\d{1,2}(?:\.\d{1,2})*\s+[A-Z]")


def _lines(text: str) -> list:
    return [l.strip() for l in text.splitlines() if l.strip()]


def citation_density(text: str) -> float:
    """Share of this chunk's characters that sit inside citation-shaped spans.

    Measured by matched length rather than match count so that one long
    reference weighs more than one bracketed cross-reference in a paragraph —
    a body page citing three sources scores low, a page that *is* citations
    scores high.
    """
    if not text:
        return 0.0
    matched = 0
    for pattern in (_CITATION_MARKER, _AUTHOR_INITIALS, _BIB_BOILERPLATE):
        matched += sum(len(m.group(0)) for m in pattern.finditer(text))
    return min(1.0, matched / len(text))


def looks_like_reference_list(text: str) -> bool:
    """Citation-dense enough that the chunk is a bibliography, not prose."""
    body = _strip_provenance(text)
    if _FRONT_MATTER_HEADING.match(text) and re.match(
        r"^\s*(?:\[[^\]]*\]\s*)?(?:references|bibliography)\b", text, re.IGNORECASE
    ):
        return True
    density = citation_density(body)
    if density > 0.40:
        return True
    # Many short lines each carrying a citation marker: a reference list whose
    # entries are short enough that density alone stays under the bar.
    lines = _lines(body)
    if len(lines) >= 5:
        cited = sum(1 for l in lines if _CITATION_MARKER.search(l)
                    or _BIB_BOILERPLATE.search(l))
        if cited / len(lines) > 0.55:
            return True
    return False


def looks_like_front_matter(text: str) -> bool:
    """Blurb, preface, TOC or index by shape alone, wherever it sits."""
    body = _strip_provenance(text)
    if _FRONT_MATTER_HEADING.match(text):
        return True
    if _ENDORSEMENT.search(body):
        return True

    lines = _lines(body)
    if len(lines) >= 4:
        toc = sum(1 for l in lines if _TOC_LINE.match(l) or _TOC_NUMBERED.match(l))
        if toc / len(lines) > 0.50:
            return True

    # Blurb vocabulary is only decisive in short, self-contained text; a
    # chapter may well say "this book" in passing.
    if len(body) < 700 and len(_BLURB_VOCABULARY.findall(body)) >= 2:
        return True
    return False


_PROVENANCE = re.compile(r"^\s*\[[^\]\n]*\]\s*\n?")


def _strip_provenance(text: str) -> str:
    """Drop the `[source p.N]` header the chunker prepends."""
    return _PROVENANCE.sub("", text, count=1)


def front_matter_cutoff(total_pages: Optional[int]) -> int:
    """Last page number still considered front matter, 0 if the rule is off."""
    if not total_pages or total_pages < MIN_PAGES_FOR_POSITION_RULE:
        return 0
    pages = int(total_pages * FRONT_MATTER_FRACTION)
    return max(FRONT_MATTER_MIN_PAGES, min(pages, FRONT_MATTER_MAX_PAGES))


def classify(text: str, page: Optional[int] = None,
             total_pages: Optional[int] = None) -> str:
    """Label one chunk `body`, `front_matter` or `reference_list`.

    Reference lists are tested first: they appear at the back, where the
    position rule has nothing to say, and a bibliography under a "References"
    heading would otherwise be caught by the front-matter heading pattern and
    mislabelled.
    """
    if looks_like_reference_list(text):
        return "reference_list"

    cutoff = front_matter_cutoff(total_pages)
    if cutoff and page is not None and page <= cutoff:
        return "front_matter"

    if looks_like_front_matter(text):
        return "front_matter"
    return "body"
