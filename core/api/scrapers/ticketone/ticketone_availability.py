from typing import Dict, Any, Optional


AVAILABLE_KEYWORDS = [
    "biglietti",
    "acquista",
    "disponibile",
    "disponibili",
    "ultimi biglietti",
]

UNAVAILABLE_KEYWORDS = [
    "sold out",
    "non disponibile",
    "non disponibili",
    "annullato",
    "evento cancellato",
]


def normalize_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return " ".join(str(value).split()).strip().lower()


def check_ticketone_list_availability(container_text: Optional[str]) -> Dict[str, Any]:
    """
    Determina lo stato disponibilità partendo dal testo della card/lista evento TicketOne.
    Versione MVP locale:
    - se trova keyword positive => available
    - se trova keyword negative => unavailable
    - altrimenti => unknown
    """
    text = normalize_text(container_text)

    if not text:
        return {
            "available": None,
            "status": "unknown",
            "reason": "empty_container_text",
        }

    for kw in UNAVAILABLE_KEYWORDS:
        if kw in text:
            return {
                "available": False,
                "status": "unavailable",
                "reason": f"found_negative_keyword:{kw}",
            }

    for kw in AVAILABLE_KEYWORDS:
        if kw in text:
            return {
                "available": True,
                "status": "available",
                "reason": f"found_positive_keyword:{kw}",
            }

    return {
        "available": None,
        "status": "unknown",
        "reason": "no_known_keyword",
    }