"""Stable, content-free failure metadata for Carbon turns."""
def failure(exc, detail):
    message = str(exc)
    if message == 'carbon_native_tool_in_json_mode':
        stage, code = 'response_stream', message
    elif message.startswith('carbon_json_'):
        stage = 'tool_validation' if 'tool_call' in message else 'response_decode'
        code = message
    elif message in ('vertex_incomplete_stream', 'vertex_empty_response', 'vertex_incomplete_response'):
        stage, code = 'response_stream', message
    elif message == 'vertex_turn_ownership_lost':
        stage, code = 'ownership', message
    elif message == 'vertex_reply_submission_failed':
        stage, code = 'reply_submission', message
    elif isinstance(exc, TimeoutError):
        stage, code = 'timeout', 'carbon_timeout'
    elif detail.get('http_status'):
        stage, code = 'gateway_http', 'carbon_http_' + str(detail['http_status'])
    else:
        stage, code = 'session', 'carbon_' + type(exc).__name__
    return dict(stage=stage, code=code, **{key: detail[key] for key in
        ('request_id','finish_reason','http_status','request_usage') if key in detail})
