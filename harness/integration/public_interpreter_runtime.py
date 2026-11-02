"""Qualified PublicInterpreter transport admitted by existing MODEL_RUNTIME policy."""
from contextlib import ExitStack

from ..executor.client import OllamaNativeClient, OllamaTimeouts
from ..protocol_transport import ollama_compatible_schema
from ..verification.witness_grammar import LocalWitnessGrammarCompiler, project_witness_schema
from ..judge.public_interpreter import PublicInterpreterError, PublicInterpreterModelV1


class ProductionPublicInterpreterRuntime:
    def __init__(self, *, controller, registry, locks, client=None, grammar_compiler=None):
        self.controller = controller
        self.registry = registry
        self.locks = locks
        self.client = client or OllamaNativeClient(OllamaTimeouts(generation_timeout_seconds=180))
        self.model = PublicInterpreterModelV1()
        self.dispatch_started = False
        self.last_generation_metadata = {}
        self.grammar_compiler = grammar_compiler or LocalWitnessGrammarCompiler()

    def qualify_witness_schema(self, schema):
        return self.grammar_compiler.qualify(schema)

    def _definition(self):
        definition = self.controller.resolve_definition(self.model.model_id)
        endpoint = self.registry.get_server(definition.server).ollama_base_url.rstrip('/')
        if (definition.ollama_model != self.model.model_tag
                or definition.context.get('default') != 32768
                or definition.runtime.get('num_gpu') != 39
                or definition.runtime.get('execution') != 'gpu_worker'):
            raise PublicInterpreterError('PUBLIC_INTERPRETER_CAPABILITY_UNAVAILABLE','qualified profile mismatch')
        if endpoint != self.registry.get_server('lead').ollama_base_url.rstrip('/'):
            raise PublicInterpreterError('PUBLIC_INTERPRETER_CAPABILITY_UNAVAILABLE','single endpoint required')
        return definition,endpoint

    def _verify_identity(self,endpoint,path):
        value = self.client._request(endpoint,path,timeout_seconds=5)
        rows = value.get('models')
        if type(rows) is not list or any(type(row) is not dict for row in rows):
            raise PublicInterpreterError('PUBLIC_INTERPRETER_CAPABILITY_UNAVAILABLE','malformed residency/catalog')
        exact = [row for row in rows if (row.get('name') or row.get('model')) == self.model.model_tag]
        if len(exact) != 1 or exact[0].get('digest') != self.model.model_digest:
            raise PublicInterpreterError('PUBLIC_INTERPRETER_CAPABILITY_UNAVAILABLE','exact tag/digest unavailable')
        if path == '/api/ps':
            if len(rows) != 1 or exact[0].get('context_length') != 32768 or not exact[0].get('size_vram',0):
                raise PublicInterpreterError('SEQUENTIAL_RESIDENCY_FAILURE','qualified residency mismatch')
        return exact[0]

    def complete(self,system,user,schema,*,identity):
        self.dispatch_started = False
        self.last_generation_metadata = {}
        if len((system+user).encode('utf-8')) > 262144:
            raise PublicInterpreterError('PUBLIC_INTERPRETER_CONTEXT_UNSUPPORTED')
        # Only the new tuple representation needs this conversion gate. Legacy
        # DIRECT contracts keep their existing bounded happy path.
        projected_schema = project_witness_schema(schema)
        if projected_schema != ollama_compatible_schema(schema):
            try:
                self.qualify_witness_schema(schema)
            except ValueError as exc:
                raise PublicInterpreterError('PUBLIC_WITNESS_BACKEND_GRAMMAR_INCOMPATIBLE', str(exc)) from exc
        definition,endpoint = self._definition()
        with self.controller.model_transaction('PUBLIC_INTERPRETER',self.model.model_id):
            with ExitStack() as stack:
                stack.enter_context(self.controller.gpu_lease.held('PUBLIC_INTERPRETER',
                    timeout_seconds=self.controller.policy.acquire_timeout_seconds))
                stack.enter_context(self.locks.gpu_worker_lock.held(self.model.model_id))
                if definition.resources.get('heavy_cpu_offload'):
                    stack.enter_context(self.locks.cpu_bandwidth_lock.held(self.model.model_id))
                # Catalog verification precedes any residency mutation. Existing
                # policy handles known idle residents and refuses foreign GPU owners.
                self._verify_identity(endpoint,'/api/tags')
                self.controller.pre_load(endpoint,self.model.model_tag)
                try:
                    self.controller.backend.preload(endpoint,self.model.model_tag,32768,39)
                    self.controller.post_load(endpoint,self.model.model_tag)
                    self._verify_identity(endpoint,'/api/ps')
                    self.dispatch_started = True
                    response = self.client._request(endpoint,'/api/chat',timeout_seconds=180,
                        payload={'model':self.model.model_tag,
                            'messages':[{'role':'system','content':system},{'role':'user','content':user}],
                            'format':projected_schema,'stream':False,'think':False,
                            'keep_alive':self.controller.policy.worker_keep_alive,
                            'options':{'num_ctx':32768,'num_gpu':39,'temperature':0.0,'num_predict':4096}})
                    message = response.get('message')
                    content = message.get('content') if type(message) is dict else None
                    if (response.get('model') != self.model.model_tag or response.get('done') is not True
                            or response.get('done_reason') not in ('stop',None)
                            or type(content) is not str or len(content.encode('utf-8')) > 65536):
                        raise PublicInterpreterError('PUBLIC_INTERPRETER_PROTOCOL_INVALID','invalid/truncated response')
                    self._verify_identity(endpoint,'/api/ps')
                    self.last_generation_metadata = {key:response.get(key) for key in (
                        'prompt_eval_count','eval_count','prompt_eval_duration','eval_duration','load_duration')}
                    return content
                finally:
                    # Lock order and verified unload belong to existing policy.
                    self.controller.backend.unload(endpoint,self.model.model_tag)
