from src.generators.noinline_analysis import NoInlineAnalysis
from src.ir import ast, builtins, types
from src.ir.context import Context


def _func2_type():
    return types.ParameterizedType(
        builtins.FunctionType(2),
        [builtins.Short, builtins.Double, builtins.Any],
    )


def _run_analysis(*declarations):
    program = ast.Program(Context(), "kotlin")
    for declaration in declarations:
        program.add_declaration(declaration)
    NoInlineAnalysis().visit(program)


def test_receiver_call_marks_inline_parameter_as_noinline():
    callback_type = _func2_type()

    target_method = ast.FunctionDeclaration(
        name="criminals",
        params=[
            ast.ParameterDeclaration("bugling", callback_type),
            ast.ParameterDeclaration("retched", builtins.Double),
        ],
        ret_type=builtins.Integer,
        body=ast.BottomConstant(builtins.Integer),
        func_type=ast.FunctionDeclaration.CLASS_METHOD,
    )
    revoked_cls = ast.ClassDeclaration("Revoked", superclasses=[], functions=[target_method])
    revoked_type = revoked_cls.get_type()

    tailwind = ast.VariableDeclaration(
        "tailwind",
        ast.BottomConstant(revoked_type),
        var_type=revoked_type,
    )

    inline_method = ast.FunctionDeclaration(
        name="criminals",
        params=[
            ast.ParameterDeclaration("bugling", callback_type),
            ast.ParameterDeclaration("retched", builtins.Double),
        ],
        ret_type=builtins.Integer,
        body=ast.FunctionCall(
            "criminals",
            [
                ast.CallArgument(ast.Variable("bugling")),
                ast.CallArgument(ast.Variable("retched")),
            ],
            receiver=ast.Variable("tailwind"),
        ),
        func_type=ast.FunctionDeclaration.CLASS_METHOD,
        is_inline=True,
    )
    obits_cls = ast.ClassDeclaration("Obits", superclasses=[], functions=[inline_method])

    _run_analysis(revoked_cls, tailwind, obits_cls)

    assert inline_method.params[0].noinline


def test_direct_inline_forwarding_stays_inline():
    callback_type = _func2_type()

    target = ast.FunctionDeclaration(
        name="target",
        params=[ast.ParameterDeclaration("cb", callback_type)],
        ret_type=builtins.Integer,
        body=ast.BottomConstant(builtins.Integer),
        func_type=ast.FunctionDeclaration.FUNCTION,
        is_inline=True,
    )

    caller = ast.FunctionDeclaration(
        name="caller",
        params=[ast.ParameterDeclaration("cb", callback_type)],
        ret_type=builtins.Integer,
        body=ast.FunctionCall("target", [ast.CallArgument(ast.Variable("cb"))]),
        func_type=ast.FunctionDeclaration.FUNCTION,
        is_inline=True,
    )

    _run_analysis(target, caller)

    assert not caller.params[0].noinline

