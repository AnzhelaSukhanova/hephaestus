"""Reference script for inspecting a saved IR pickle.

Run from the repository root with a trusted pickle file:
    python -m tools.investigations.pickle_investigator PATH_TO_PROGRAM_KT_BIN

Pickle loading can execute code, so only use files from trusted sources.
"""

import argparse
import pickle
from pathlib import Path

from src.generators.generator import Generator
from src.ir import ast
from src.ir.data_structures import IncrementalDAGTransitiveClosure
from src.ir.generation_context import InliningSource, IrFunctionBodyStub


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Regenerate Destroyer.playpens from a saved IR pickle.'
    )
    parser.add_argument(
        'pickle_path',
        type=Path,
        help='path to a trusted saved program.kt.bin file',
    )
    args = parser.parse_args()

    with args.pickle_path.open('rb') as pickle_file:
        program = pickle.load(pickle_file)

    # Hook up a generator to the saved context.
    context = program.context
    gen = Generator('kotlin')
    gen.context = context
    gen.inline_call_graph = IncrementalDAGTransitiveClosure()
    gen.namespace = ('src', 'Destroyer', 'playpens')
    gen.depth = 2  # The body is one level below the function.

    # Find Destroyer.playpens in the supplied pickle.
    declarations = program.context.get_declarations(
        ast.GLOBAL_NAMESPACE, only_current=True
    ).values()
    destroyer = next(d for d in declarations if d.name == 'Destroyer')
    playpens = next(fn for fn in destroyer.functions if fn.name == 'playpens')

    print(f'Before regen: commences.inlining_scope = {playpens.params[0].inlining_scope.name}')

    # Force body regeneration.
    playpens.body = ast.BottomConstant(playpens.ret_type)

    # Enter the same call-context stack _gen_func_body uses.
    with context.call_contexts(
        subtree_pushed_call_context=[
            InliningSource(playpens, IrFunctionBodyStub()),
            None,
        ]
    ):
        new_body = gen._gen_func_body(playpens.ret_type, playpens)

    playpens.body = new_body

    print(f'After regen:  commences.inlining_scope = {playpens.params[0].inlining_scope.name}')
    print('Body:')
    print(new_body)


if __name__ == '__main__':
    main()
