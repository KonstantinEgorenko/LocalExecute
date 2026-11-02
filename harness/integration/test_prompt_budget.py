"""Offline tokenizer/template qualification; never loads weights or calls Ollama.

The certified template prints each of the two text messages and each tool once.
All literal framing is bounded by the template source size. The byte-level BPE
basis is verified in the GGUF vocabulary; merges can only reduce symbol count.
This is an upper bound, never an exact token count. Unsupported templates fail.
"""
import hashlib
import json
import os
from pathlib import Path
import struct

# Audited text-only, system/user, native-tools branch, not a model/tag exception.
CERTIFIED_TEMPLATE='e84f32a23fdda27689f868aa4a1a5621f41133e51a48d7f3efcbea2839574259'
MAX_METADATA=32*1024**2
FORMATS={0:'B',1:'b',2:'H',3:'h',4:'I',5:'i',6:'f',7:'?',10:'Q',11:'q',12:'d'}


def metadata(path):
    values={}
    with Path(path).open('rb') as f:
        def read(n):
            if n<0 or f.tell()+n>MAX_METADATA:raise ValueError('GGUF_METADATA_BOUND')
            data=f.read(n)
            if len(data)!=n:raise ValueError('TRUNCATED_GGUF_METADATA')
            return data
        def num(fmt):return struct.unpack('<'+fmt,read(struct.calcsize(fmt)))[0]
        def string():return read(num('Q')).decode('utf8')
        def value(t,keep=False):
            if t==8:return string()
            if t==9:
                dtype=num('I');count=num('Q')
                if count>1000000 or dtype not in (*FORMATS,8):raise ValueError('UNSUPPORTED_GGUF_ARRAY')
                if dtype==8:
                    seq=[string() for _ in range(count)]
                    return set(seq) if keep else None
                read(struct.calcsize(FORMATS[dtype])*count)
                return None
            if t not in FORMATS:raise ValueError('UNSUPPORTED_GGUF_VALUE')
            return num(FORMATS[t])
        if read(4)!=b'GGUF' or num('I') not in (2,3):raise ValueError('UNSUPPORTED_GGUF')
        num('Q');count=num('Q')
        if count>4096:raise ValueError('GGUF_KEY_BOUND')
        for _ in range(count):
            key=string()
            if key in values:raise ValueError('DUPLICATE_GGUF_KEY')
            v=value(num('I'),key in ('tokenizer.ggml.tokens','tokenizer.ggml.merges'))
            if key.startswith('tokenizer.'):values[key]=v
    return values


def byte_basis():
    normal=list(range(33,127))+list(range(161,173))+list(range(174,256))
    extras=[b for b in range(256) if b not in normal]
    return {chr(b) for b in normal}|{chr(256+i) for i in range(len(extras))}


def model_store_root(environ=None, user_value=None):
    """Read existing installation settings only; never write global configuration."""
    env=os.environ if environ is None else environ
    value=env.get('OLLAMA_MODELS')
    if not value and user_value is not None:value=user_value()
    elif not value and os.name=='nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
                value=winreg.QueryValueEx(key,'OLLAMA_MODELS')[0]
        except FileNotFoundError:pass
    return Path(os.path.expandvars(value)) if value else Path.home()/'.ollama/models'


def bound(payload,meta,model_digest):
    messages=payload.get('messages',[])
    if (len(messages)!=2 or [m.get('role') for m in messages]!=['system','user']
        or any(set(m)!={'role','content'} or type(m['content']) is not str for m in messages)
        or payload.get('think') is not False or payload.get('stream') is not False
        or 'format' in payload or len(payload.get('tools',[]))!=1):
        raise ValueError('UNCERTIFIED_REPAIR_CHAT_SHAPE')
    template=meta.get('tokenizer.chat_template','')
    template_sha=hashlib.sha256(template.encode()).hexdigest()
    if (template_sha!=CERTIFIED_TEMPLATE or meta.get('tokenizer.ggml.model')!='gpt2'
        or meta.get('tokenizer.ggml.pre')!='qwen35'
        or not byte_basis()<=meta.get('tokenizer.ggml.tokens',set())):
        raise ValueError('UNCERTIFIED_TOKENIZER_OR_TEMPLATE')
    tokens=meta['tokenizer.ggml.tokens']
    merges=meta.get('tokenizer.ggml.merges')
    if not isinstance(merges,set) or any(' ' not in m or m.replace(' ','',1) not in tokens for m in merges):
        raise ValueError('BPE_MERGE_BASIS_UNPROVEN')
    wire=json.dumps(payload,allow_nan=False).encode('utf8')
    escaped=wire
    for char in (b'<',b'>',b'&',b"'"):
        escaped=escaped.replace(char,('\\u%04x'%char[0]).encode())
    # JSON ASCII escaping also over-bounds HTML escaping used by tojson. Template
    # source bounds all constant framing, including unused branches. BOS/EOS 16
    # covers the two tokenizer-added special tokens conservatively.
    return {'model':payload['model'],'model_digest':model_digest,
        'request_sha256':hashlib.sha256(wire).hexdigest(),
        'scope':'SERIALIZED_CHAT_WITH_TOOLS','method':'VERIFIED_CONSERVATIVE_BOUND',
        'prompt_tokens':len(escaped)+len(template.encode())+16,
        'serialized_bytes':len(wire),'template_bytes':len(template.encode()),
        'template_sha256':template_sha,'byte_basis_verified':256,
        'proof_revision':'GGUF_BYTE_BPE_TEXT_TEMPLATE_V1','weights_loaded':False}


def qualify_local(payload,observed_digests):
    tag=payload['model']
    if '/' in tag or '\\' in tag or '..' in tag:raise ValueError('UNSUPPORTED_LOCAL_MANIFEST_NAME')
    name,version=(tag.rsplit(':',1) if ':' in tag else (tag,'latest'))
    root=model_store_root()
    manifest=root/'manifests/registry.ollama.ai/library'/name/version
    raw=manifest.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    expected={v for (url,model),v in observed_digests.items() if model==tag}
    if expected!={digest}:raise ValueError('TOKENIZER_MODEL_IDENTITY_UNPROVEN')
    doc=json.loads(raw)
    if any(l['mediaType'].endswith(('.template','.system','.adapter')) for l in doc['layers']):
        raise ValueError('CUSTOM_PROMPT_LAYER_UNSUPPORTED')
    layers=[l for l in doc['layers'] if l['mediaType']=='application/vnd.ollama.image.model']
    if len(layers)!=1:raise ValueError('MODEL_LAYER_IDENTITY_UNPROVEN')
    layer=layers[0];blob=root/'blobs'/layer['digest'].replace(':','-')
    before=blob.stat()
    if before.st_size!=layer['size']:raise ValueError('MODEL_LAYER_DRIFT')
    proof=bound(payload,metadata(blob),digest)
    after=blob.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or manifest.read_bytes()!=raw:
        raise ValueError('TOKENIZER_METADATA_DRIFT')
    return proof
