"""Optional snapshots cannot become process or transport authority."""
import logging
from .receipts import SupervisionConflict


def unavailable(journal, identity, component, exc):
    """Best-effort explicit gap; failure to persist is never called durable."""
    gap = {'component': component, 'error_type': type(exc).__name__,
           'observation_status': 'UNAVAILABLE'}
    seen=getattr(journal,'diagnostic_gaps',set())
    key=(component,type(exc).__name__)
    if key in seen:
        return gap
    seen.add(key)
    journal.diagnostic_gaps=seen
    try:
        journal.append('DIAGNOSTIC_OBSERVATION_UNAVAILABLE', identity, **gap)
    except SupervisionConflict:
        raise
    except Exception as publication:
        gap['diagnostic_publication'] = 'UNAVAILABLE'
        gap['publication_error_type'] = type(publication).__name__
    # A broken log handler is also optional. No raw response or error secrets.
    try:
        logging.getLogger(__name__).warning('Supervision diagnostic gap: %s', gap)
    except Exception:
        pass
    return gap
