"""Small public-reference arithmetic language. Never eval/exec model text.

An expression is a frozen model hypothesis, not an authoritative requirement.
This interpreter only removes numerical guessing from that hypothesis.
"""
import ast
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


_FUNCTIONS = {'sum_products', 'round_half_up', 'text', 'sum', 'len', 'min', 'max', 'abs', 'bool_or', 'trim_casefold', 'pairs_map', 'map_get', 'pairs_map_normalized', 'half_open_overlap', 'half_open_pair_overlap'}


@dataclass(frozen=True, slots=True)
class PublicReferenceExceptionV1:
    """Expected public outcome; not an evaluator or candidate exception."""
    exception_class: str


def half_open_overlap(values):
    if len(values) != 4 or any(type(v) is not int or abs(v) > 1000000 for v in values):
        raise ValueError('half-open reference requires four bounded JSON integers')
    a, b, c, d = values
    if a > b or c > d:
        return PublicReferenceExceptionV1('ValueError')
    return max(0, min(b, d) - max(a, c))


def public_reference_language_contract():
    """Model-facing grammar for the evaluator below; never an output repair."""
    functions = {
        'half_open_overlap': 'half_open_overlap(start_a,end_a,start_b,end_b): four JSON integers in [-1000000,1000000]; half-open intersection cardinality, zero for empty/touching/disjoint intervals; reversed intervals yield a typed expected ValueError outcome',
        'half_open_pair_overlap': 'half_open_pair_overlap(left,right): two two-item ordered JSON integer arrays; same half-open intersection cardinality',
        'map_get': 'map_get(mapping,key,default): lookup a bounded JSON object using a string key, return the supplied scalar default when absent; map_get(mapping,trim_casefold(key),default) normalizes the query only',
        'pairs_map_normalized': 'pairs_map_normalized(records): bounded ASCII string/scalar JSON pairs, trim and casefold each key in original order, last normalized key wins',
        'trim_casefold': 'trim_casefold(name): trim whitespace and casefold one ASCII JSON string, at most 256 UTF-8 bytes',
        'pairs_map': 'pairs_map(name): map up to 16 JSON key/value pairs with exact string keys; last identical key wins; scalar JSON values only; no key normalization',
        'sum': 'sum(name): sum up to 16 numeric items in an existing JSON-array parameter',
        'len': 'len(name): length of an existing JSON-array parameter with at most 16 items',
        'sum_products': 'sum_products(name): sum products of up to 16 numeric pairs in an existing JSON-array parameter; each pair is a two-item JSON array',
        'min': 'min(a,...): minimum of 1 to 16 numeric expressions',
        'max': 'max(a,...): maximum of 1 to 16 numeric expressions',
        'abs': 'abs(value): absolute value of one numeric expression',
        'round_half_up': 'round_half_up(value,decimal_places): Decimal ROUND_HALF_UP with integer decimal_places from 0 to 8',
        'text': 'text(value): render one value as text',
        'bool_or': 'bool_or(a,b): Boolean OR of exactly two Boolean parameters; never coerces strings or numbers',
    }
    if set(functions) != _FUNCTIONS:
        raise ValueError('public reference function documentation differs from evaluator allowlist')
    return ('Allowed: supplied parameter names; decimal numeric/string literals; binary + - * /; unary + -; '
        'and parentheses for grouping scalar expressions. + also joins two strings. '
        + '; '.join(functions[name] for name in sorted(_FUNCTIONS)) + '. '
        'sum(name), len(name), and sum_products(name) accept only an existing sequence-valued '
        'parameter supplied as a JSON array in arguments. Nested JSON arrays are data for sum_products, '
        'not expression syntax. List literals [...], tuple literals (comma-separated items), nested literal sequences, '
        'comprehensions, generators, dictionaries, sets, subscripts, attributes, lambdas, imports, '
        'starred arguments, keyword call arguments and calls outside this allowlist are forbidden. '
        'To combine scalar expressions use + directly, not a constructed sequence. '
        'Numeric arithmetic does not coerce strings or booleans. For Boolean contracts return bool_or(parameter_a,parameter_b) directly, not text(...). JSON true/false are input values; Python True/False are expression literals. Boolean literals may be rendered '
        'with text but are not numeric operands. Reference expressions must use supplied parameters '
        'and every required numeric option. Bounds: 1024 UTF-8 expression bytes, 64 AST nodes, '
        'depth 8, finite numeric operands with magnitude at most 1e18, and 256 UTF-8 result/string bytes. ')


def _number(value):
    if type(value) not in (int, float, Decimal):
        raise ValueError('reference arithmetic requires numeric operands')
    result = value if type(value) is Decimal else Decimal(str(value))
    if not result.is_finite() or abs(result) > Decimal('1e18'):
        raise ValueError('reference arithmetic operand exceeds bounds')
    return result


def bounded_scalar(value):
    if type(value) not in (str,int,float,bool,type(None)):
        raise ValueError('reference requires JSON scalar values')
    if type(value) is str and len(value.encode('utf-8'))>256:
        raise ValueError('reference string exceeds bound')
    if type(value) in (int,float): _number(value)
    return value


def bounded_mapping(value):
    if type(value) is not dict or len(value)>16:
        raise ValueError('reference requires bounded JSON object')
    for key,item in value.items():
        if type(key) is not str or not key.isascii() or len(key.encode())>256:
            raise ValueError('reference mapping requires bounded ASCII string keys')
        bounded_scalar(item)
    return value


def evaluate_public_reference_value(expression, variables, *, required_names=()):
    if type(expression) is not str or not expression or len(expression.encode('utf-8')) > 1024:
        raise ValueError('public reference expression exceeds bounds')
    try:
        tree = ast.parse(expression, mode='eval')
    except SyntaxError as exc:
        raise ValueError('invalid public reference expression syntax') from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > 64:
        raise ValueError('public reference expression node limit')
    names = {n.id for n in nodes if isinstance(n, ast.Name)} - _FUNCTIONS
    if not names or not names <= set(variables) or not set(required_names) <= names:
        raise ValueError('reference must use supplied parameters and required numeric options')

    def visit(node, depth=0):
        if depth > 8:
            raise ValueError('public reference expression depth limit')
        child = lambda n: visit(n, depth + 1)
        if isinstance(node, ast.Constant):
            if type(node.value) in (int, float):
                return _number(Decimal(ast.get_source_segment(expression, node)))
            if type(node.value) in (str, bool) and len(str(node.value).encode()) <= 256:
                return node.value
        elif isinstance(node, ast.Name) and node.id in variables:
            return variables[node.id]
        elif isinstance(node, ast.BinOp):
            left, right = child(node.left), child(node.right)
            if isinstance(node.op, ast.Add) and type(left) is str and type(right) is str:
                result = left + right
                if len(result.encode()) > 256:raise ValueError('reference string exceeds bound')
                return result
            left, right = _number(left), _number(right)
            if isinstance(node.op, ast.Add):return _number(left + right)
            if isinstance(node.op, ast.Sub):return _number(left - right)
            if isinstance(node.op, ast.Mult):return _number(left * right)
            if isinstance(node.op, ast.Div):return _number(left / right)
        elif isinstance(node, ast.UnaryOp):
            value = _number(child(node.operand))
            if isinstance(node.op, ast.USub):return -value
            if isinstance(node.op, ast.UAdd):return value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS and not node.keywords:
            values = [child(n) for n in node.args]
            name = node.func.id
            if name == 'half_open_overlap':
                return half_open_overlap(values)
            if name == 'half_open_pair_overlap':
                if (len(values) != 2 or any(type(v) is not list or len(v) != 2 for v in values)
                        or any(type(x) is not int for v in values for x in v)
                        or any(v[0] > v[1] for v in values)):
                    raise ValueError('half-open pair reference requires two ordered integer pairs')
                return half_open_overlap(values[0] + values[1])
            if name=='map_get' and len(values)==3:
                mapping=bounded_mapping(values[0]);key=values[1];default=bounded_scalar(values[2])
                if type(key) is not str or not key.isascii() or len(key.encode())>256:
                    raise ValueError('map_get requires bounded ASCII key')
                return mapping.get(key,default)
            if name=='trim_casefold' and len(values)==1:
                value=values[0]
                if type(value) is not str or not value.isascii() or len(value.encode())>256:
                    raise ValueError('trim_casefold requires bounded ASCII string')
                return value.strip().casefold()
            if name in ('pairs_map','pairs_map_normalized') and len(values)==1:
                value=values[0]
                if type(value) is not list or len(value)>16:
                    raise ValueError('pairs_map requires bounded JSON array')
                result={}
                for row in value:
                    if (type(row) is not list or len(row)!=2 or type(row[0]) is not str
                            or len(row[0].encode('utf-8'))>256
                            or type(row[1]) not in (str,int,float,bool,type(None))):
                        raise ValueError('pairs_map requires string keys and scalar values')
                    bounded_scalar(row[1])
                    key=row[0]
                    if name=='pairs_map_normalized':
                        if not key.isascii():raise ValueError('normalized pairs require ASCII keys')
                        key=key.strip().casefold()
                    result[key]=row[1]
                if len(str(result).encode())>256:
                    raise ValueError('pairs_map result exceeds bounds')
                return result
            if name == 'bool_or' and len(values) == 2 and all(type(v) is bool for v in values):
                return values[0] or values[1]
            if name == 'text' and len(values) == 1:
                return str(values[0])
            if name == 'round_half_up' and len(values) == 2:
                precision = _number(values[1])
                if precision != precision.to_integral_value() or not 0 <= precision <= 8:
                    raise ValueError('reference precision must be an integer in [0,8]')
                return _number(values[0]).quantize(Decimal(1).scaleb(-int(precision)), rounding=ROUND_HALF_UP)
            if name in ('sum', 'len', 'sum_products') and len(values) == 1 and type(values[0]) is list and len(values[0]) <= 16:
                rows = values[0]
                if name == 'len':return Decimal(len(rows))
                if name == 'sum':return _number(sum((_number(v) for v in rows), Decimal(0)))
                if any(type(row) is not list or len(row) != 2 for row in rows):
                    raise ValueError('sum_products requires bounded numeric pairs')
                return _number(sum((_number(row[0]) * _number(row[1]) for row in rows), Decimal(0)))
            if name == 'abs' and len(values) == 1:return abs(_number(values[0]))
            if name in ('min', 'max') and 1 <= len(values) <= 16:
                return (min if name == 'min' else max)(_number(v) for v in values)
        raise ValueError('unsupported public reference expression')

    try:
        result = visit(tree.body)
    except ArithmeticError as exc:
        raise ValueError('public reference arithmetic failed') from exc
    text = str(result)
    if len(text.encode('utf-8')) > 256:
        raise ValueError('public reference result exceeds bound')
    return result


def evaluate_public_reference(expression, variables, *, required_names=()):
    """Historical text rendering; typed consumers use the same evaluator."""
    return str(evaluate_public_reference_value(expression, variables, required_names=required_names))
