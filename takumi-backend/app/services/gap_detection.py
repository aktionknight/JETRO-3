from app.ai.schemas import ClarificationCandidate

DECISION_IMPORTANCE = 3
MISSING_REASONING_WEIGHT = {"interpretation": 3, "decision": 3, "cue": 1}
CHANGED_CONDITION_WEIGHT = 2
NOVICE_CONFUSION_WEIGHT = 2
MAX_QUESTIONS = 3
TYPE_ORDER = ["missing_reasoning", "changed_condition", "beginner_mistake"]
TEMPLATES = {
    "missing_reasoning": (
        "What observation prompts you to {action} at this point, and what does it tell you?"
    ),
    "changed_condition": (
        "After you {action}, what observation would make you change your next decision?"
    ),
    "beginner_mistake": (
        "What mistake could a beginner make when they {action}, and how would you recognize it?"
    ),
}
FALLBACKS = {
    "missing_reasoning": "What observation prompts this step, and what does it tell you?",
    "changed_condition": "What observation would make you change your next decision at this step?",
    "beginner_mistake": (
        "What mistake could a beginner make at this step, and how would you recognize it?"
    ),
}


def detect_gaps(knowledge_units, *, excluded_pairs=()):
    candidates = []
    for index, unit in enumerate(knowledge_units):
        unit = unit.model_dump() if hasattr(unit, "model_dump") else unit
        gaps = []
        if unit.get("interpretation") is None or unit.get("decision") is None:
            priority = DECISION_IMPORTANCE + sum(
                weight
                for field, weight in MISSING_REASONING_WEIGHT.items()
                if unit.get(field) is None
            )
            gaps.append(("missing_reasoning", priority))
        if unit.get("decision") is not None and unit.get("exception") is None:
            gaps.append(("changed_condition", DECISION_IMPORTANCE + CHANGED_CONDITION_WEIGHT))
        if unit.get("exception") is None:
            gaps.append(("beginner_mistake", NOVICE_CONFUSION_WEIGHT))
        action = unit["action"]
        action = action[:1].lower() + action[1:]
        for gap_type, priority in gaps:
            if (unit["id"], gap_type) in excluded_pairs:
                continue
            question = TEMPLATES[gap_type].format(action=action) if action else FALLBACKS[gap_type]
            candidates.append(
                (
                    index,
                    ClarificationCandidate(
                        knowledge_unit_id=unit["id"],
                        gap_type=gap_type,
                        priority=priority,
                        question=question,
                    ),
                )
            )
    candidates.sort(
        key=lambda pair: (-pair[1].priority, pair[0], TYPE_ORDER.index(pair[1].gap_type))
    )
    selected, types = [], set()
    for _, candidate in candidates:
        if candidate.gap_type not in types:
            selected.append(candidate)
            types.add(candidate.gap_type)
        if len(selected) == MAX_QUESTIONS:
            break
    return selected
