import pytest

from src.ir import ast, kotlin_types as kt
from src.ir.context import Context


def _function(name, visibility):
    return ast.FunctionDeclaration(
        name,
        [],
        kt.Integer,
        ast.IntegerConstant(1, kt.Integer),
        ast.FunctionDeclaration.FUNCTION,
        visibility=visibility,
    )


@pytest.mark.parametrize(
    ("visibility", "can_cross", "can_cross_as_friend"),
    [
        (ast.Visibilities.PRIVATE, False, False),
        (ast.Visibilities.INTERNAL, False, True),
        (ast.Visibilities.PROTECTED, True, True),
        (ast.Visibilities.PUBLIC, True, True),
        (ast.Visibilities.UNKNOWN, True, True),
    ],
)
def test_visibility_cross_module_boundary(
        visibility, can_cross, can_cross_as_friend):
    assert visibility.can_cross_module_boundary() is can_cross
    assert (visibility.can_cross_module_boundary(as_friend_module=True) is
            can_cross_as_friend)

    declaration = _function("function", visibility)
    assert declaration.can_cross_module_boundary() is can_cross
    assert (declaration.can_cross_module_boundary(as_friend_module=True) is
            can_cross_as_friend)


def test_unknown_visibility_resolves_to_public():
    context = ast.VisibilityResolutionContext(is_local=True)

    assert ast.Visibilities.UNKNOWN.resolve(context) is ast.Visibilities.PUBLIC
    assert ast.Visibilities.UNKNOWN.can_cross_module_boundary(
        context=context)


def test_declaration_without_visibility_can_cross_module_boundary():
    declaration = ast.ClassDeclaration("Box", [])

    assert not hasattr(declaration, "visibility")
    assert declaration.can_cross_module_boundary()
    assert declaration.can_cross_module_boundary(as_friend_module=True)


def test_program_get_exported_names_filters_and_preserves_order():
    program = ast.Program(Context(), "kotlin")
    declarations = [
        _function("privateFunction", ast.Visibilities.PRIVATE),
        _function("internalFunction", ast.Visibilities.INTERNAL),
        _function("publicFunction", ast.Visibilities.PUBLIC),
        _function("protectedFunction", ast.Visibilities.PROTECTED),
        _function("unknownFunction", ast.Visibilities.UNKNOWN),
        ast.ClassDeclaration("ClassWithoutVisibility", []),
    ]

    for declaration in declarations:
        program.add_declaration(declaration)

    assert program.get_exported_names() == [
        "publicFunction",
        "protectedFunction",
        "unknownFunction",
        "ClassWithoutVisibility",
    ]