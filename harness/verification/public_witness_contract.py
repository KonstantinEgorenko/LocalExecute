"""Bounded callable contracts from frozen public claims and API shape only.

This is capability eligibility, never proof that a claim or model hypothesis is
true. No implementation expression, candidate output or expected patch is used.
"""
import ast
from dataclasses import dataclass, asdict
from .public_reference import bounded_scalar, bounded_mapping, evaluate_public_reference_value


@dataclass(frozen=True, slots=True)
class PublicWitnessContractV1:
    kind: str
    parameters: tuple[str,...]
    expression: str = ''

    def argument_schema(self, generic):
        scalar={'anyOf':[{'type':t} for t in ('null','boolean','number','string')]}
        scalar['anyOf'][-1]['maxLength']=256
        string={'type':'string','maxLength':256}
        if self.kind=='ASCII_NORMALIZE':return {self.parameters[0]:string}
        if self.kind=='ASCII_MAPPING_LOOKUP':
            return {self.parameters[0]:{'type':'object','maxProperties':16,'propertyNames':string,'additionalProperties':scalar},self.parameters[1]:string,self.parameters[2]:scalar}
        if self.kind=='ASCII_NORMALIZED_PAIRS':
            return {self.parameters[0]:{'type':'array','maxItems':16,'items':{'type':'array','minItems':2,'maxItems':2,'prefixItems':[string,scalar],'items':False}}}
        return {name:generic(name) for name in self.parameters}

    def validate(self, arguments, expression):
        if self.kind=='LEGACY':return
        if set(arguments)!=set(self.parameters):raise ValueError('public witness parameter binding mismatch')
        p=self.parameters
        if self.kind=='ASCII_MAPPING_LOOKUP':
            bounded_mapping(arguments[p[0]]);bounded_scalar(arguments[p[2]])
            key=arguments[p[1]]
        elif self.kind=='ASCII_NORMALIZED_PAIRS':
            records=arguments[p[0]]
            if type(records) is not list or len(records)>16:raise ValueError('public witness requires bounded pairs')
            for row in records:
                if type(row) is not list or len(row)!=2:raise ValueError('public witness requires two-item JSON pairs')
                if type(row[0]) is not str or not row[0].isascii() or len(row[0].encode())>256:raise ValueError('public witness requires ASCII keys')
                bounded_scalar(row[1])
            key=None
        else:key=arguments[p[0]]
        if key is not None and (type(key) is not str or not key.isascii() or len(key.encode())>256):
            raise ValueError('public witness requires bounded ASCII string')
        # Parameter-bound canonical relationship, not a guessed output. The
        # relationship remains a hypothesis assessed by the independent Judge.
        if ast.dump(ast.parse(expression,mode='eval'),include_attributes=False)!=ast.dump(ast.parse(self.expression,mode='eval'),include_attributes=False):
            raise ValueError('public witness expression does not match supported parameter-bound relationship')

    def readiness(self):
        if self.kind=='LEGACY':return {'status':'SUPPORTED_LEGACY','contract':asdict(self),
            'PUBLIC_SEMANTICS_EXPRESSIBLE':True,'ARGUMENT_SCHEMA_VALID':True,
            'BACKEND_GRAMMAR_COMPATIBLE':'NOT_PROVEN','WITNESS_READY_FOR_DISPATCH':False}
        p=self.parameters
        variables=({p[0]:' MiXeD '} if self.kind=='ASCII_NORMALIZE' else
            {p[0]:{'mixed':2},p[1]:' MiXeD ',p[2]:7} if self.kind=='ASCII_MAPPING_LOOKUP' else
            {p[0]:[[' MiXeD ',1],['mixed',2]]})
        self.validate(variables,self.expression)
        evaluate_public_reference_value(self.expression,variables,required_names=p)
        return {'status':'PUBLIC_WITNESS_EXECUTABLE','contract':asdict(self),'candidate_output_used':False,
            'PUBLIC_SEMANTICS_EXPRESSIBLE':True,'ARGUMENT_SCHEMA_VALID':True,
            'BACKEND_GRAMMAR_COMPATIBLE':'NOT_PROVEN','WITNESS_READY_FOR_DISPATCH':False}


def callable_witness_contract(frozen, surface):
    if 'declaration' in surface:
        return DeclaredWitnessContract(surface['declaration'])
    from .acceptance import SemanticProvenanceV1 as P
    # Text is only public authority, never the OBSERVED_IMPLEMENTATION body.
    text='\n'.join(e.content.lower() for e in frozen.evidence if e.provenance in
        (P.AUTHORITATIVE_REQUIREMENT,P.PUBLIC_CONTRACT_EVIDENCE))
    doc=(surface.get('docstring') or '').lower();p=tuple(surface['parameters'])
    if not surface['keyword_witness_supported'] or not 1<=len(p)<=16:
        raise ValueError('PUBLIC_WITNESS_CAPABILITY_UNSUPPORTED: callable parameter shape')
    normalized='ascii' in text and ('case-insensitive' in text or 'case insensitive' in text)
    if normalized and 'lookup' in doc and ('default' in doc or 'missing' in doc):
        if len(p)!=3 or p[2] not in surface['literal_defaults']:
            raise ValueError('PUBLIC_WITNESS_CAPABILITY_UNSUPPORTED: normalized mapping lookup shape')
        return PublicWitnessContractV1('ASCII_MAPPING_LOOKUP',p,f'map_get({p[0]}, trim_casefold({p[1]}), {p[2]})')
    if normalized and ('key/value pairs' in doc or 'key-value pairs' in doc) and ('last' in doc and 'wins' in doc):
        if len(p)!=1:raise ValueError('PUBLIC_WITNESS_CAPABILITY_UNSUPPORTED: normalized pair shape')
        return PublicWitnessContractV1('ASCII_NORMALIZED_PAIRS',p,f'pairs_map_normalized({p[0]})')
    if normalized and 'key' in doc and ('normalize' in doc or 'normalise' in doc):
        if len(p)!=1:raise ValueError('PUBLIC_WITNESS_CAPABILITY_UNSUPPORTED: normalization shape')
        return PublicWitnessContractV1('ASCII_NORMALIZE',p,f'trim_casefold({p[0]})')
    if any(word in doc for word in ('mapping','dictionary','nested object')):
        raise ValueError('PUBLIC_WITNESS_CAPABILITY_UNSUPPORTED: public collection semantics')
    return PublicWitnessContractV1('LEGACY',p)


@dataclass(frozen=True, slots=True)
class DeclaredWitnessContract:
    row: dict
    kind: str = 'DECLARED_PUBLIC_CALLABLE'

    @property
    def expression(self):
        return self.row['reference_expression']

    def argument_schema(self, generic):
        result = {}
        for name, domain in self.row['arguments'].items():
            scalar = {'type':'integer','minimum':domain['minimum'],'maximum':domain['maximum']}
            result[name] = scalar if domain['kind']=='INTEGER' else {
                'type':'array','minItems':2,'maxItems':2,'prefixItems':[scalar,scalar],'items':False}
        return result

    def validate(self, arguments, expression, expected_exception=None):
        from .callable_authority import validate_arguments
        from .public_reference import PublicReferenceExceptionV1
        result = evaluate_public_reference_value(expression,arguments,required_names=self.row['arguments'])
        expected = result.exception_class if isinstance(result,PublicReferenceExceptionV1) else ''
        return validate_arguments(self.row,{'args':[],'kwargs':arguments},expression,
            expected if expected_exception is None else expected_exception)

    def readiness(self):
        variables = {name: domain['minimum'] if domain['kind']=='INTEGER' else [domain['minimum'],domain['maximum']]
            for name,domain in self.row['arguments'].items()}
        self.validate(variables,self.expression)
        return {'status':'PUBLIC_WITNESS_EXECUTABLE','contract':self.row,'candidate_output_used':False,
            'PUBLIC_SEMANTICS_EXPRESSIBLE':True,'ARGUMENT_SCHEMA_VALID':True,
            'BACKEND_GRAMMAR_COMPATIBLE':'NOT_PROVEN','WITNESS_READY_FOR_DISPATCH':False}
