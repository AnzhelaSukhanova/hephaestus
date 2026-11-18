import pytest

from src.generators.config import VisibilityProbabilities, cfg
from src.generators.generator import Generator
from src.ir import ast, kotlin_types as kt
from src.ir.context import Context


def _generator_in_child() -> Generator:
    generator = Generator('kotlin')
    generator.context = Context()
    child = ast.ClassDeclaration(
        'Child', [], ast.ClassDeclaration.REGULAR,
        fields=[], functions=[], is_final=True,
    )
    generator.context.add_class(ast.GLOBAL_NAMESPACE, child.name, child)
    generator.namespace = ast.GLOBAL_NAMESPACE + (child.name,)
    return generator


def _configure_private_only(
        monkeypatch: pytest.MonkeyPatch,
        probabilities: VisibilityProbabilities) -> None:
    monkeypatch.setattr(probabilities, 'public', 0.0)
    monkeypatch.setattr(probabilities, 'private', 1.0)
    monkeypatch.setattr(probabilities, 'not_specified', 0.0)


@pytest.mark.parametrize('declaration', ['function', 'property'])
def test_override_with_private_only_probability_uses_unspecified_visibility(
        monkeypatch: pytest.MonkeyPatch, declaration: str) -> None:
    probabilities = (cfg.prob.function_visibility if declaration == 'function'
                     else cfg.prob.property_visibility)
    _configure_private_only(monkeypatch, probabilities)
    generator = _generator_in_child()

    if declaration == 'function':
        monkeypatch.setattr(
            Generator, '_gen_func_body',
            lambda _self, return_type, _function: ast.BottomConstant(return_type),
        )
        function = generator.gen_func_decl(
            etype=kt.Integer, func_name='work', params=[], type_params=[],
            class_is_final=True, declared_as_override=True,
        )
        visibility = function.visibility
    else:
        field = generator.gen_field_decl(
            kt.Integer, declared_as_override=True, add_to_parent=False,
        )
        visibility = field.visibility

    assert visibility is ast.Visibilities.UNKNOWN
