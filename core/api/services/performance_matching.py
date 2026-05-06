from datetime import timedelta
from difflib import SequenceMatcher

from api.models import Performance


def normalize_for_match(value):
    """
    Normalizza una stringa per il matching.
    Non è una normalizzazione perfetta, ma serve per confrontare nomi evento
    provenienti da piattaforme diverse.
    """

    if not value:
        return ""

    value = str(value).strip().lower()

    replacements = [
        ("-", " "),
        ("_", " "),
        ("'", " "),
        ("’", " "),
        ("\"", " "),
        (".", " "),
        (",", " "),
        (":", " "),
        (";", " "),
        ("  ", " "),
    ]

    for old, new in replacements:
        value = value.replace(old, new)

    return " ".join(value.split())


def similarity(a, b):
    """
    Restituisce una similarità tra 0 e 1.
    1 significa testi identici.
    """

    a = normalize_for_match(a)
    b = normalize_for_match(b)

    if not a or not b:
        return 0.0

    return SequenceMatcher(None, a, b).ratio()


def find_matching_performances(
    *,
    event_name,
    starts_at_utc,
    city=None,
    hours_window=12,
    min_similarity=0.65,
    max_time_diff_hours=None,
):
    """
    Cerca performance compatibili con evento/data/città.

    Criteri:
    1. data entro +/- hours_window
    2. città uguale, se disponibile
    3. similarità nome >= min_similarity
    4. opzionale: differenza oraria massima

    max_time_diff_hours serve per evitare falsi match tra repliche
    dello stesso evento nella stessa giornata ma a orari diversi.
    """

    if not event_name or not starts_at_utc:
        return []

    start = starts_at_utc - timedelta(hours=hours_window)
    end = starts_at_utc + timedelta(hours=hours_window)

    qs = (
        Performance.objects
        .select_related("evento", "luogo")
        .filter(starts_at_utc__gte=start)
        .filter(starts_at_utc__lte=end)
    )

    if city:
        qs = qs.filter(luogo__citta__iexact=city)

    results = []

    for perf in qs:
        candidate_name = perf.evento.nome_evento if perf.evento else ""
        score = similarity(event_name, candidate_name)

        if score < min_similarity:
            continue

        time_diff_seconds = abs((perf.starts_at_utc - starts_at_utc).total_seconds())
        time_diff_hours = time_diff_seconds / 3600

        if max_time_diff_hours is not None and time_diff_hours > max_time_diff_hours:
            continue

        results.append({
            "performance": perf,
            "score": score,
            "candidate_name": candidate_name,
            "time_diff_hours": time_diff_hours,
        })

    results.sort(key=lambda x: (x["score"], -x["time_diff_hours"]), reverse=True)

    return results


def find_best_matching_performance(
    *,
    event_name,
    starts_at_utc,
    city=None,
    hours_window=12,
    min_similarity=0.65,
    max_time_diff_hours=None,
):
    """
    Restituisce la migliore performance compatibile oppure None.
    """

    matches = find_matching_performances(
        event_name=event_name,
        starts_at_utc=starts_at_utc,
        city=city,
        hours_window=hours_window,
        min_similarity=min_similarity,
        max_time_diff_hours=max_time_diff_hours,
    )

    if not matches:
        return None

    return matches[0]["performance"]