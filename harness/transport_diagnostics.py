"""Bounded, redacted transport evidence. Never retries or interprets acceptance."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

PREVIEW_BYTES = 2048
BODY_READ_LIMIT = 1024 * 1024
_context = ContextVar('transport_diagnostic_context', default={})
_sensitive = re.compile(r'(?i)token|secret|authorization|password|api.?key|cookie|credential|email|person|username')


def safe_text(text):
    text = str(text)
    for key, value in os.environ.items():
        if (_sensitive.search(key) or key.upper() in {'USERPROFILE','USERNAME','USER','HOME'}) and len(value) >= 4:
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)(?:api.?key|token|secret|password|authorization|cookie|email|username)\s*[=:]\s*["\']?[^\s,;"\']+', '[REDACTED]', text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[REDACTED_EMAIL]', text)
    text = re.sub(r'(?i)https?://[^\s"\']+', '[REDACTED_URL]', text)
    text = re.sub(r'(?i)[A-Z]:[\\/][^\r\n"\']*', '[REDACTED_PATH]', text)
    text = re.sub(r'/(?:home|Users|var)/[^\s"\']+', '[REDACTED_PATH]', text)
    return text.encode('utf-8')[:PREVIEW_BYTES].decode('utf-8', errors='ignore')


def _secret_values(value):
    if isinstance(value,dict):
        for key,child in value.items():
            if _sensitive.search(str(key)) and isinstance(child,str) and child:
                yield child
            yield from _secret_values(child)
    elif isinstance(value,list):
        for child in value:
            yield from _secret_values(child)


@contextmanager
def transport_context(**identity):
    token = _context.set({**_context.get(), **identity})
    try:
        yield
    finally:
        _context.reset(token)


@dataclass(frozen=True)
class TransportErrorRecord:
    category: str
    endpoint: str
    model_tag: str | None
    request_sha256: str | None
    elapsed_ms: int
    physical_request_counted: bool
    causal_exception: str
    role: str | None = None
    stage: str | None = None
    request_id: str | None = None
    run_id: str | None = None
    todo_id: str | None = None
    model_digest: str | None = None
    status_code: int | None = None
    reason: str | None = None
    content_type: str | None = None
    server_error: str | None = None
    body_sha256: str | None = None
    body_bytes: int | None = None
    body_hash_scope: str | None = None
    body_representation: str | None = None
    bounded_body_preview: str | None = None
    body_truncated: bool = False
    capture_error: str | None = None
    stream_close_error: str | None = None

    def to_dict(self):
        return {'schema_version':1, 'kind':'TRANSPORT_HTTP_ERROR' if self.status_code is not None and self.status_code >= 400 else 'TRANSPORT_ERROR', **asdict(self)}


def record(*, category, url, payload, started, causal_exception, request_bytes=None, **values):
    context = {k:v for k,v in _context.get().items() if k in
        {'role','stage','request_id','run_id','todo_id','model_digest'}}
    context.update(values)
    return TransportErrorRecord(category, urlsplit(url).path,
        payload.get('model') if payload else None,
        hashlib.sha256(request_bytes if request_bytes is not None else json.dumps(payload).encode('utf-8')).hexdigest() if payload is not None else None,
        max(0, int((time.monotonic()-started)*1000)), bool(payload and payload.get('messages')),
        causal_exception, **context)


def capture_http_error(exc, *, url, payload, started, causal_exception='ExecutionHttpError', model_digest=None, request_bytes=None):
    """Read once before raising. Hash in constant memory; distinguish prefix hashes."""
    cached = getattr(exc, '_swarm_http_capture', None)
    if cached is not None:
        return cached
    full = hashlib.sha256(); legacy = hashlib.sha256(); preview = bytearray()
    count = 0; capture_error = None
    try:
        while count <= BODY_READ_LIMIT:
            chunk = exc.read(min(8192, BODY_READ_LIMIT+1-count))
            if not chunk:
                break
            previous=count; count += len(chunk); full.update(chunk)
            legacy.update(chunk[:max(0,65537-previous)])
            if len(preview) < 65536:
                preview.extend(chunk[:65536-len(preview)])
    except (OSError, ValueError) as error:
        capture_error=type(error).__name__
    stream_close_error=None
    try:
        exc.close()
    except (OSError,ValueError) as error:
        stream_close_error=type(error).__name__
    complete = count <= BODY_READ_LIMIT and capture_error is None
    server_error=None; protocol_reason=None; error_kind=None
    if count == 0 and capture_error is None:
        representation='BODY_EMPTY'; sanitized=''; server_error='BODY_EMPTY'
    else:
        try:
            text=bytes(preview).decode('utf-8')
        except UnicodeDecodeError:
            representation='BODY_NON_UTF8'; sanitized='[NON_UTF8_BODY; see fingerprint]'
        else:
            representation='BODY_TEXT'; obj=None
            try:
                if count <= 65536 and complete:
                    obj=json.loads(text)
            except (ValueError, TypeError):
                pass
            if isinstance(obj,dict):
                representation='BODY_JSON'
                secret_values=list(_secret_values(obj))
                error=obj.get('error')
                if isinstance(error,str):
                    for value in secret_values:
                        error=error.replace(value,'[REDACTED]')
                    server_error=safe_text(error)
                    if error.startswith('llama-server returned invalid tool call arguments for '):
                        error_kind='TOOL_CALL_PROTOCOL_ERROR'
                        protocol_reason='JSON_EOF' if error.endswith('unexpected end of JSON input') else 'INVALID_NATIVE_ARGUMENTS'
                # Only the error field is public, never arbitrary echoed JSON fields.
                sanitized=server_error or '[JSON_BODY; no string error field]'
            else:
                if text.lstrip().startswith(('{','[')):
                    representation='BODY_JSON_UNPARSED'
                    sanitized='[UNPARSED_JSON_BODY; see fingerprint]'
                else:
                    sanitized=safe_text(text)
    if count <= 65536 and complete and error_kind is None:
        error_kind='HTTP_SERVER_ERROR' if 500 <= exc.code <= 599 else 'HTTP_ERROR'
    value=record(category='TRANSPORT_HTTP_5XX' if 500 <= exc.code <= 599 else 'TRANSPORT_HTTP_4XX',
        url=url,payload=payload,started=started,causal_exception=causal_exception,request_bytes=request_bytes,
        model_digest=model_digest or _context.get().get('model_digest'),status_code=exc.code,
        reason=safe_text(exc.reason),content_type=safe_text(exc.headers.get('Content-Type','')) if exc.headers else None,
        server_error=server_error,body_sha256=full.hexdigest(),body_bytes=count,
        body_hash_scope='FULL' if complete else 'CAPTURED_PREFIX',body_representation=representation,
        bounded_body_preview=sanitized,body_truncated=count>PREVIEW_BYTES or not complete,
        capture_error=capture_error,stream_close_error=stream_close_error)
    legacy_values=dict(response_error_kind=error_kind,protocol_error_reason=protocol_reason,
        response_body_sha256=legacy.hexdigest(),response_body_bytes=min(count,65537),response_body_truncated=count>65536)
    exc._swarm_http_capture=(value,legacy_values)
    return value,legacy_values


def exception_chain(exc):
    rows=[]; seen=set()
    while exc is not None and id(exc) not in seen and len(rows)<8:
        seen.add(id(exc))
        rows.append({'type':type(exc).__name__,'message_sha256':hashlib.sha256(str(exc).encode('utf-8')).hexdigest()})
        exc=exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return rows


def error_projection(exc):
    cursor=exc; seen=set()
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        diagnostic=getattr(cursor,'transport_error',None)
        if isinstance(diagnostic,TransportErrorRecord):
            return {'transport_error':diagnostic.to_dict(),'exception_chain':exception_chain(exc)}
        durable=getattr(cursor,'failure_diagnostic',None)
        if durable:
            return {'failure_diagnostic':durable,'exception_chain':exception_chain(exc),
                    **({'transport_error':durable['transport_error']} if 'transport_error' in durable else {})}
        cursor=cursor.__cause__ or (None if cursor.__suppress_context__ else cursor.__context__)
    return {'exception_chain':exception_chain(exc)}


def failure_category(exc):
    projection=error_projection(exc)
    if 'transport_error' in projection:
        return projection['transport_error']['category']
    from .errors import WorkerProtocolError, ExecutionServerUnavailableError
    if isinstance(exc,WorkerProtocolError):return 'MODEL_RESPONSE_PARSE_ERROR'
    if isinstance(exc,TimeoutError):return 'TRANSPORT_TIMEOUT'
    if isinstance(exc,(ExecutionServerUnavailableError,ConnectionError)):return 'TRANSPORT_CONNECTION_ERROR'
    return getattr(exc,'category','TRANSPORT_PARSE_FAIL')
