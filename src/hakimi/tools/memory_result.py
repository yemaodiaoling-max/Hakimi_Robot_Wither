"""Model-facing memory.read view; storage and operator receipts stay complete."""

# Keep scope, readable sources, lifecycle, source availability and pagination.
# Provider attribution, hashes and redundant counters remain in StoreRead.
_READ_FIELDS = (
    'id', 'kind', 'session_key', 'user_id', 'message_id', 'text', 'text_kind',
    'status', 'superseded_by', 'supersedes', 'evidence',
    'time', 'observed_at', 'recorded_at', 'event_time', 'expires_at',
    'source', 'relations',
    'origin_label', 'source_label', 'identity_label',
    'source_view', 'raw_available',
    'offset', 'total_chars', 'next_offset',
)
_REQUIRED_VALUES = {'text', 'offset', 'total_chars', 'next_offset', 'raw_available'}


def memory_read_result(record):
    """Project an already-authorized read without changing text or offsets."""
    result = {key: record[key] for key in _READ_FIELDS if key in record
              and (key in _REQUIRED_VALUES or record[key] not in (None, '', [], {}, 0))}
    if 'status' not in result and 'state' in record:
        result['status'] = record['state']  # Legacy readers only expose state.
    if result.get('evidence') == result.get('text'):
        result.pop('evidence', None)
    # These are message/recording times, not a guessed date of the event.
    if result.get('observed_at') == result.get('time'):
        result.pop('observed_at', None)
    if result.get('recorded_at') == result.get('time'):
        result.pop('recorded_at', None)
    if record.get('kind') == 'episode' and not record.get('event_time'):
        result['event_time'] = '未知'
    if result.get('source_label') == result.get('origin_label'):
        result.pop('source_label', None)
    return result
