"""Conservative text/value matching between a capital budget-ledger object
and a published procurement contract.

Used by `analysis.rules.unmatched_spending_flags` to decide whether a capital
budget object's recorded spending plausibly corresponds to *some* published
EOP/SIGMA contract. Deliberately biased towards *not* claiming a match when
in doubt: a false "no contract found" is softened by the rule's own hedged
citizen-facing message ("may be contracted below the legal threshold, or
described differently in the register"), but a false *positive* match would
misinform citizens that a specific contract accounts for specific spending
when it may not. See `docs/RULES.md` for the worked examples this was tuned
against (real object/contract title pairs from `data/nessebar.db`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Bulgarian stop words plus generic public-procurement/capital-ledger
#: boilerplate that carries ~no information about *which* object/contract is
#: meant. Kept deliberately broad: including a word here only costs a little
#: recall (softened by the rule's hedged wording below); leaving one out
#: risks two unrelated records matching just because they share bureaucratic
#: phrasing ("доставка", "основен ремонт", "обособена позиция", ...).
STOPWORDS: frozenset[str] = frozenset(
    """
    за на в от до и или по към при с със да не се като през тази този тези то ще е са бр
    план плана планa проект проектиране изграждане основен ремонт текущ аварийно укрепване
    реконструкция преустройство устройство устройството строеж строителство
    на територията територията община общината общ гр град града с кв ул уpи упи м обект обекти
    района район находящ представляващ разположен участък участъка част нов нова нови
    сграда сградата сгради територия терена имот имота
    достав доставка доставки услуга услуги монтаж демонтаж баланс избор изпълнител нуждите
    нужди обособена обособени позиция позиции дейност дейности предмет брой броя вид видове
    смр оп етап етапа
    """.split()  # noqa: SIM905 -- a plain-text word list reads far better than a Python list literal
)

#: Settlement/quarter proper nouns distinctive enough to anchor a match on
#: their own. Deliberately excludes "Несебър"/"община"/"град"/"Слънчев бряг"
#: 's constituent "бряг" alone etc. is still included since it is a genuine
#: named resort area distinct from the municipal seat -- it is "Несебър"
#: itself (and generic "община"/"град") that appear in virtually every
#: title and so carry no distinguishing power.
PLACE_NAMES: frozenset[str] = frozenset(
    {
        "свети", "влас", "равда", "обзор", "оризаре", "гюльовца", "кошарица",
        "баня", "тънково", "слънчев", "бряг", "русалка", "черно", "море",
        "раковсково", "паницово", "приселци",
    }
)

#: Cadastral/parcel identifiers like "51500.506.679" -- kept as one token
#: (not split on their internal separators) since the *full* identifier is
#: what is actually distinctive; a bare leading sub-code (e.g. "51500", the
#: shared cadastral-region prefix for all of Несебър) recurs in almost every
#: record and must not be treated as distinctive on its own.
_CADASTRE_RE = re.compile(r"\d+(?:[.,]\d+){2,}")
_WORD_RE = re.compile(r"[a-zа-я]+", re.IGNORECASE)
#: Matches the abbreviation "ул." or the full word "улица", each as a whole
#: word, followed by the street's proper name. Deliberately does *not*
#: match on a bare "ул" prefix alone -- that also matches unrelated
#: adjectival forms ("улично", "уличен", "уличната", ...: "street-related",
#: not "street named ..."), which an earlier version of this regex
#: mistakenly captured fragments of (e.g. "улично осветление" -> stray
#: token "ично"), diluting/polluting the distinctive-token signal.
_STREET_RE = re.compile(r'\b(?:ул\.|улица)\s*["„]?\s*([a-zа-я]+)', re.IGNORECASE)


def tokenize(text: str | None) -> frozenset[str]:
    """Lowercased, stop-word-filtered token set for Jaccard comparison.

    Cadastral identifiers are kept intact as single `CAD:...` tokens rather
    than split on their internal dots/commas.
    """
    if not text:
        return frozenset()
    low = text.lower()
    tokens: set[str] = {f"CAD:{m.group(0)}" for m in _CADASTRE_RE.finditer(low)}
    stripped = _CADASTRE_RE.sub(" ", low)
    for word in _WORD_RE.findall(stripped):
        if word in STOPWORDS or len(word) <= 2:
            continue
        tokens.add(word)
    return frozenset(tokens)


def distinctive_tokens(text: str | None) -> frozenset[str]:
    """The subset of a title's signal distinctive enough, on its own, to
    anchor a match: cadastral identifiers, named settlements/resort areas,
    and street names (``ул. X``)."""
    if not text:
        return frozenset()
    low = text.lower()
    out: set[str] = {f"CAD:{m.group(0)}" for m in _CADASTRE_RE.finditer(low)}
    out |= {w for w in _WORD_RE.findall(low) if w in PLACE_NAMES}
    out |= {f"ST:{m.group(1)}" for m in _STREET_RE.finditer(low) if m.group(1) not in STOPWORDS}
    return frozenset(out)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """|intersection| / |union|, or 0.0 if either side is empty."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


@dataclass(frozen=True)
class MatchCandidate:
    """A contract-like record considered as a possible match for a budget object."""

    key: Any  # caller-defined identifier, e.g. (source, source_id)
    title: str | None
    value_eur: float | None


@dataclass(frozen=True)
class Match:
    """A successful match: the candidate plus why it was accepted."""

    candidate: MatchCandidate
    jaccard_score: float
    shared_distinctive: frozenset[str]


def find_best_match(
    object_name: str | None,
    object_value_eur: float | None,
    candidates: list[MatchCandidate],
    *,
    jaccard_threshold: float = 0.35,
    value_ratio_low: float = 0.5,
    value_ratio_high: float = 2.0,
) -> Match | None:
    """Return the best matching candidate for `object_name`/`object_value_eur`,
    or `None` if no candidate clears both gates:

    1. Value ratio: `object_value_eur / candidate.value_eur` (or its
       reciprocal) must land in `[value_ratio_low, value_ratio_high]`.
    2. Text overlap: Jaccard similarity >= `jaccard_threshold`, OR the two
       titles share at least one "distinctive" token (a cadastral parcel id,
       a named settlement/resort area, or a street name).

    Callers must treat `None` as "automated matching found nothing", never
    as "no contract exists" -- see the module docstring.
    """
    if not object_value_eur or object_value_eur <= 0:
        return None

    obj_tokens = tokenize(object_name)
    obj_distinct = distinctive_tokens(object_name)

    best: Match | None = None
    for cand in candidates:
        if not cand.value_eur or cand.value_eur <= 0:
            continue
        ratio = object_value_eur / cand.value_eur
        if not (value_ratio_low <= ratio <= value_ratio_high):
            continue

        cand_tokens = tokenize(cand.title)
        score = jaccard(obj_tokens, cand_tokens)
        shared = obj_distinct & distinctive_tokens(cand.title)
        clears_gate = score >= jaccard_threshold or shared
        if clears_gate and (best is None or score > best.jaccard_score):
            best = Match(candidate=cand, jaccard_score=score, shared_distinctive=shared)
    return best
