import re

from app.ai.schemas import ApprovedContext, Provenance, QAProviderResult
from app.models.schemas import Claim

INSUFFICIENT_MSG = "The approved knowledge pack does not contain this information."
NEEDS_CONTEXT_MSG = "Specify which step or component of the task the question refers to."
LANGUAGE_MSG = "Only English (en) questions are supported in this version."
SPEC_TERMS = {
    "torque",
    "tolerance",
    "tolerances",
    "clearance",
    "specification",
    "specifications",
    "spec",
    "specs",
    "rating",
    "rated",
    "pressure",
    "temperature",
    "rpm",
    "speed",
    "preload",
    "newton",
    "newtons",
    "nm",
    "psi",
    "mm",
    "micron",
    "microns",
    "diameter",
    "dimension",
    "dimensions",
    "lubricant",
    "measurement",
    "measurements",
    "force",
}
STOPWORDS = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "you",
    "i",
    "we",
    "they",
    "he",
    "she",
    "it",
    "this",
    "that",
    "these",
    "those",
    "to",
    "of",
    "for",
    "in",
    "on",
    "at",
    "by",
    "with",
    "and",
    "or",
    "what",
    "why",
    "how",
    "when",
    "which",
    "who",
    "should",
    "would",
    "could",
    "can",
    "expert",
    "experts",
    "be",
    "there",
    "my",
    "your",
    "their",
    "perform",
    "performs",
    "performed",
    "step",
    "first",
    "then",
    "next",
    "into",
    "from",
    "about",
    "its",
}
INTENTS = [
    (r"\b(mistakes?|wrong|avoid|exception|unless|escalat\w*|beginners?|novices?)\b", ["exception"]),
    (r"\b(why|reason|purpose|because)\b", ["interpretation", "cue", "decision"]),
    (r"\b(look for|notice|signs?|cue|observ\w*|see)\b", ["cue"]),
    (r"\b(next|decide|decision)\b", ["decision"]),
    (r"\b(how|procedure|action)\b", ["action"]),
]
CONTENT_FIELDS = ["action", "cue", "interpretation", "decision", "exception"]


def normalize(question):
    return re.sub(r"[^a-z0-9 ]", " ", question.lower()).split()


def stem(token):
    if token.endswith("ing") and len(token) > 5:
        return token[:-3]
    if token.endswith("ed") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and len(token) > 3 and not token.endswith("ss"):
        return token[:-1]
    return token


def _value(unit, field):
    return unit.get(field) if isinstance(unit, dict) else getattr(unit, field)


def _terms(text):
    equivalents = {
        "gage": "gauge",
        "id": "identification",
        "rotat": "turn",
        "rotate": "turn",
        "fastener": "bolt",
    }
    return {
        equivalents.get(stem(token), stem(token))
        for token in normalize(text)
        if token not in STOPWORDS
    }


def rank_units(question, units, limit=3):
    query = _terms(question)
    documents = [
        {field: _terms(_value(unit, field) or "") for field in CONTENT_FIELDS} for unit in units
    ]
    frequency = {
        term: sum(any(term in words for words in document.values()) for document in documents)
        for term in query
    }
    scores = [
        sum(
            (3 if field == "action" else 1) / frequency[term]
            for field, words in document.items()
            for term in query & words
        )
        for document in documents
    ]
    return [
        units[index]
        for index in sorted(range(len(units)), key=lambda index: -scores[index])
        if scores[index] > 0
    ][:limit]


def question_guard(question, language, units):
    """Return server-owned abstention status/message, or allow retrieval."""
    if language != "en":
        return "needs_context", LANGUAGE_MSG
    if not _terms(question):
        return "needs_context", NEEDS_CONTEXT_MSG
    selected = rank_units(question, units)
    if not selected:
        return "insufficient_evidence", INSUFFICIENT_MSG
    approved_tokens = {
        token
        for unit in selected
        for field in CONTENT_FIELDS
        for token in normalize(_value(unit, field) or "")
    }
    tokens = set(normalize(question))
    if (tokens & SPEC_TERMS) - approved_tokens:
        return "insufficient_evidence", INSUFFICIENT_MSG
    physical_cues = {"feel", "felt", "tactile"}
    if tokens & physical_cues and not approved_tokens & physical_cues:
        return "insufficient_evidence", INSUFFICIENT_MSG
    return None


def deterministic_answer(question, language, context: ApprovedContext) -> QAProviderResult:
    def result(status, message=None, claims=None):
        return QAProviderResult(
            status=status,
            claims=claims or [],
            missing_information=[message] if message else [],
            provenance=Provenance(provider="fixture"),
        )

    guard = question_guard(question, language, context.knowledge_units)
    if guard:
        return result(*guard)
    fields = next(
        (fields for regex, fields in INTENTS if re.search(regex, question.lower())), CONTENT_FIELDS
    )
    unit = rank_units(question, context.knowledge_units, limit=1)[0]
    claims = []
    for field in fields:
        value = getattr(unit, field)
        evidence_ids = unit.field_evidence.get(field, [])
        if value is not None and evidence_ids and set(evidence_ids) <= context.evidence_ids:
            claims.append(
                Claim(text=value, knowledge_unit_id=unit.id, field=field, evidence_ids=evidence_ids)
            )
    if not claims:
        return result(
            "insufficient_evidence",
            f"The approved knowledge for '{unit.action}' does not record "
            f"the {' or '.join(fields)}.",
        )
    return result("answered", claims=claims)
