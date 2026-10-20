"""Bounded, deterministic arithmetic for local chat; never eval or exec expressions."""
from __future__ import annotations

import ast
from decimal import Decimal, InvalidOperation, localcontext
import math

MAX_EXPRESSION = 240
MAX_NODES = 85
MAX_DIGITS = 95


def calculate(expression: str) -> str:
    """Evaluate arithmetic with a strict AST allowlist and finite magnitude bounds.

    Supported: + - * / // % **, parentheses, pi/e, sqrt, abs, round,
    sin/cos/tan, log/ln. Transcendentals are approximate; plain decimal
    arithmetic preserves sensible exactness (e.g. 0.1 + 0.2 = 0.3).
    """
    if not isinstance(expression, str) or not (1 <= len(expression.strip()) <= MAX_EXPRESSION):
        raise ValueError('Expression must contain 1-240 characters.')
    try:
        tree = ast.parse(expression, mode='eval')
    except SyntaxError as error:
        raise ValueError('Invalid arithmetic expression.') from error
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise ValueError('Expression too complex.')

    def bounded(value):
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError('Result must be a finite number.')
        if value and value.adjusted() > MAX_DIGITS:
            raise ValueError('Number exceeds calculator magnitude limit.')
        if value and value.adjusted() < -MAX_DIGITS:
            raise ValueError('Number is below calculator precision limit.')
        return value

    def walk(node, depth=0):
        if depth > 24:
            raise ValueError('Expression nesting too deep.')
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            raw = str(node.value)
            if len(raw) > 100:
                raise ValueError('Numeric literal too long.')
            return bounded(Decimal(raw))
        if isinstance(node, ast.Name) and node.id in ('pi', 'e'):
            return Decimal(str(math.pi if node.id == 'pi' else math.e))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            val = walk(node.operand, depth+1)
            return bounded(val if isinstance(node.op, ast.UAdd) else -val)
        if isinstance(node, ast.BinOp) and type(node.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div,
                                                              ast.FloorDiv, ast.Mod, ast.Pow):
            left = walk(node.left, depth+1)
            right = walk(node.right, depth+1)
            op = node.op
            if isinstance(op, ast.Add): out = left + right
            elif isinstance(op, ast.Sub): out = left - right
            elif isinstance(op, ast.Mult): out = left * right
            elif isinstance(op, ast.Div): out = left / right
            elif isinstance(op, ast.FloorDiv): out = left // right
            elif isinstance(op, ast.Mod): out = left % right
            else:
                if right != right.to_integral_value() or not -25 <= right <= 25:
                    raise ValueError('Exponents must be integers between -25 and 25.')
                if left and abs(left.adjusted()) * abs(int(right)) > MAX_DIGITS:
                    raise ValueError('Exponent result would be too large or too small.')
                out = left ** int(right)
            return bounded(out)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            allowed = {'sqrt': 1, 'abs': 1, 'sin': 1, 'cos': 1, 'tan': 1, 'ln': 1, 'log': 1, 'round': (1, 2)}
            if name not in allowed:
                raise ValueError('Unsupported calculator function.')
            n = allowed[name]
            if (len(node.args) not in n) if isinstance(n, tuple) else (len(node.args) != n):
                raise ValueError('Incorrect calculator function arguments.')
            args = [walk(a, depth+1) for a in node.args]
            val = args[0]
            if name == 'sqrt':
                return bounded(val.sqrt())
            if name == 'abs':
                return bounded(abs(val))
            if name == 'round':
                places = 0 if len(args) == 1 else args[1]
                if places != places.to_integral_value() or not -20 <= places <= 20:
                    raise ValueError('round precision must be -20 to 20.')
                return bounded(val.quantize(Decimal(1).scaleb(-int(places))))
            x = float(val)
            try:
                if name == 'sin': val2 = math.sin(x)
                elif name == 'cos': val2 = math.cos(x)
                elif name == 'tan': val2 = math.tan(x)
                elif name in ('ln', 'log'): val2 = math.log(x) if name == 'ln' else math.log10(x)
                else: raise ValueError('Unsupported function.')
            except (ValueError, OverflowError) as error:
                raise ValueError('Invalid function domain.') from error
            return bounded(Decimal(str(val2)))
        raise ValueError('Only arithmetic and approved calculator functions are allowed.')

    try:
        with localcontext() as ctx:
            ctx.prec = 34
            result = walk(tree.body)
            text = format(result.normalize(), 'f')
            if text == '-0': text = '0'
            if len(text) > 240:
                raise ValueError('Result too long.')
            return text
    except (InvalidOperation, ZeroDivisionError, OverflowError, ArithmeticError) as error:
        raise ValueError('Math error (division by zero, invalid domain, or overflow).') from error
