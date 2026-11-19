"""
This file includes various generators that do not require the state of the
generator.
Currently, it contains only generators for constant AST nodes.

NOTE: generators should declare a parameter named `expr_type`,
even if they don't need it, for API compatibility reasons.
"""
from src import utils
from src.ir import ast
from src.ir import types as tp
from src.generators import utils as gu


# pylint: disable=unused-argument
def gen_string_constant(expr_type: tp.Type) -> ast.StringConstant:
    """Generate a string constant.
    """
    return ast.StringConstant(gu.gen_identifier())


# pylint: disable=unused-argument
def gen_integer_constant(expr_type: tp.Type) -> ast.IntegerConstant:
    """Generate an integer constant.

    The generated integers are between -100 and 100.
    """
    return ast.IntegerConstant(utils.random.integer(-100, 100), expr_type)


def gen_real_constant(expr_type: tp.Type) -> ast.RealConstant:
    """Generate a real constant.

    The generated reals are between `-100.1000` and `100.1000`.
    """
    prefix = str(utils.random.integer(0, 100))
    suffix = str(utils.random.integer(0, 1000))
    sign = utils.random.choice(['', '-'])
    return ast.RealConstant(sign + prefix + "." + suffix, expr_type)


# pylint: disable=unused-argument
def gen_bool_constant(expr_type: tp.Type) -> ast.BooleanConstant:
    """Generate a boolean constant.
    """
    return ast.BooleanConstant(utils.random.choice(['true', 'false']))


# pylint: disable=unused-argument
def gen_char_constant(expr_type: tp.Type) -> ast.CharConstant:
    """Generate a character constant.
    """
    return ast.CharConstant(utils.random.char())
