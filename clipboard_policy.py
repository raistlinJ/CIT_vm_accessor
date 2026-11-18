"""Clipboard policy shared with AccessForge; quotas count Unicode code points."""
DIRECTIONS = ('to_vm', 'from_vm')
MAX_CHARACTERS = 1_000_000_000


def parse_clipboard_policy(metadata):
    result = {}
    for direction in DIRECTIONS:
        key = 'clipboard_' + direction
        enabled = isinstance(metadata, dict) and metadata.get(key, True) is True
        limit = metadata.get(key + '_limit') if isinstance(metadata, dict) else None
        if limit is not None and (type(limit) is not int or not 0 <= limit <= MAX_CHARACTERS):
            enabled, limit = False, 0
        epoch = metadata.get(key + '_reset', '') if isinstance(metadata, dict) else ''
        if not isinstance(epoch, str) or len(epoch) > 64:
            enabled, epoch = False, ''
        scope = metadata.get('clipboard_id', '') if isinstance(metadata, dict) else ''
        if not isinstance(scope, str) or len(scope) > 64:
            enabled, scope = False, ''
        result[direction] = {'enabled': enabled, 'limit': limit, 'reset': epoch, 'scope': scope}
    return result


def validate_clipboard_update(data):
    fields = {}
    for direction in DIRECTIONS:
        key = 'clipboard_' + direction
        if key in data:
            if type(data[key]) is not bool:
                raise ValueError(key + ' must be a boolean')
            fields[key] = data[key]
        if key + '_limit' in data:
            limit = data[key + '_limit']
            if limit is not None and (type(limit) is not int or not 0 <= limit <= MAX_CHARACTERS):
                raise ValueError(key + '_limit must be null or a whole number from 0 to 1,000,000,000')
            fields[key + '_limit'] = limit
    if 'reset_counts' in data and type(data['reset_counts']) is not bool:
        raise ValueError('reset_counts must be a boolean')
    if not fields and data.get('reset_counts') is not True:
        raise ValueError('Choose a clipboard setting to update')
    return fields
