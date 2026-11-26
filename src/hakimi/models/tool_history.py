"""References to repeated evidence in native Vertex tool results."""

import json


# Only read results with known evidence fields. Receipt metadata, errors, and
# media stay in the form returned by the tool.
_EVIDENCE_FIELDS = {
    'context.read': (('messages',), ('data', 'messages')),
    'diary.read': (('text',),),
    'files.list': (('files',),),
    'memory.graph': (('nodes',), ('edges',)),
    'memory.read': (('text',),),
    'memory.search': (('memories',),),
    'voice.read': (('text',),),
    'web.read': (('text',),),
    'web.search': (('results',),),
    'web.sources': (('sources',),),
}
_RECEIPT_PREFIXES = ((), ('result',), ('stdout',), ('stdout', 'result'))
_MIN_FIELD_CHARS = 512
_MIN_SAVED_CHARS = 128


def _successful_object(value):
    return isinstance(value, dict) and not value.get('error') and value.get('ok') is not False


def _read_field(value, path):
    for key in path:
        if not _successful_object(value):
            return None
        value = value.get(key)
    return value


def _large_fields(part):
    if not isinstance(part, dict) or part.keys() != {'functionResponse'}:
        return
    receipt = part['functionResponse']
    if not isinstance(receipt, dict) or 'parts' in receipt:
        return
    name = receipt.get('name')
    fields = _EVIDENCE_FIELDS.get(name) if isinstance(name, str) else None
    response = receipt.get('response')
    if not fields or not _successful_object(response) or response.get('stderr'):
        return
    for prefix in _RECEIPT_PREFIXES:
        for field in fields:
            path = prefix + field
            value = _read_field(response, path)
            if isinstance(value, str):
                if len(value) >= _MIN_FIELD_CHARS:
                    yield name, path, str, value
            elif isinstance(value, (dict, list)) and value:
                try:
                    encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                except (TypeError, ValueError, RecursionError):
                    continue
                if len(encoded) >= _MIN_FIELD_CHARS:
                    yield name, path, type(value), encoded


def _replace_field(obj, path, value):
    key, *rest = path
    return {**obj, key: _replace_field(obj[key], rest, value) if rest else value}


def _reference(name, path, content_index, part_index, receipt):
    location = {
        'tool': name,
        'content_number': content_index + 1,
        'part_number': part_index + 1,
        'response_field': '/' + '/'.join(path),
    }
    call_id = receipt.get('id')
    if isinstance(call_id, str) and 0 < len(call_id) <= 80:
        location['call_id'] = call_id
    return {'identical_value_in_this_request': location}


def compact_contents(contents):
    """Keep the latest receipt batch intact and reference full copies."""
    if not isinstance(contents, list) or len(contents) < 5:
        return contents
    batches = []
    for index, turn in enumerate(contents):
        if not isinstance(turn, dict) or turn.get('role') != 'user':
            continue
        parts = turn.get('parts')
        if isinstance(parts, list) and any(isinstance(p, dict) and 'functionResponse' in p for p in parts):
            batches.append((index, parts))
    if len(batches) < 2:
        return contents

    occurrences = {}
    for content_index, parts in batches:
        for part_index, part in enumerate(parts):
            for key in _large_fields(part):
                occurrences.setdefault(key, []).append((content_index, part_index, part['functionResponse']))

    latest_index = batches[-1][0]
    changes = {}
    for (name, path, _, value), locations in occurrences.items():
        first_index = locations[0][0]
        if locations[-1][0] == first_index:
            continue
        # The first full copy in a later batch stays the reference target as
        # the history grows. Revising earlier anchors would churn the prefix.
        anchor = next(location for location in locations if location[0] != first_index)
        reference = _reference(name, path, *anchor)
        if len(json.dumps(reference)) + _MIN_SAVED_CHARS > len(value):
            continue
        for content_index, part_index, _ in locations:
            if content_index != anchor[0] and content_index != latest_index:
                parts = changes.setdefault(content_index, {})
                parts.setdefault(part_index, []).append((path, reference))

    if not changes:
        return contents
    updated = list(contents)
    for content_index, parts in changes.items():
        updated_parts = list(contents[content_index]['parts'])
        for part_index, fields in parts.items():
            receipt = updated_parts[part_index]['functionResponse']
            response = receipt['response']
            for path, reference in fields:
                response = _replace_field(response, path, reference)
            updated_parts[part_index] = {'functionResponse': dict(receipt, response=response)}
        updated[content_index] = dict(contents[content_index], parts=updated_parts)
    return updated
