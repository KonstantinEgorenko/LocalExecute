"""Bounded Python regression-artifact comparison; test execution stays separate."""
from __future__ import annotations

import ast


def python_test_functions(source: str) -> dict[str, str]:
    """Inventory test function syntax, ignoring locations/comments/docstrings."""
    tree = ast.parse(source)
    result = {}

    def visit(body, prefix=""):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                if (node.body and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)):
                    node.body = node.body[1:]
                result[prefix + node.name] = ast.dump(node, include_attributes=False)
    visit(tree.body)
    return result
