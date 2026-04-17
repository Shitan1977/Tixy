from dataclasses import dataclass
from typing import Optional


@dataclass
class TicketOneEventItem:
    title: str
    event_url: str
    external_id: Optional[str]

    venue: Optional[str] = None
    city: Optional[str] = None
    starts_at_raw: Optional[str] = None
    category_hint: Optional[str] = None
    price_text: Optional[str] = None

    source: str = "list"
    detail_status: str = "not_attempted"