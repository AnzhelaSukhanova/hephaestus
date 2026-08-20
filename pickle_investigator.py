import pickle
import sys

# Add the repo to the path
sys.path.insert(0, '/Users/nika/IdeaProjects/hephaestus')

from src.generators.generator import Generator, InliningSource, IrFunctionBodyStub
from src.ir import ast
from src.ir.data_structures import IncrementalDAGTransitiveClosure
from src import utils as ut

def main():
    # Load the saved AST
    with open('/Users/nika/IdeaProjects/hephaestus/bugs/'
              'bwasm_inline_cycles_metrics_leaf_defaults/11722/program.kt.bin',
              'rb') as f:
        program = pickle.load(f)

    # Hook up a generator to the saved context
    gen = Generator("kotlin")
    gen.context = program.context
    gen.inline_call_graph = IncrementalDAGTransitiveClosure()
    gen.namespace = ("src", "Destroyer", "playpens")
    gen.depth = 2  # body is one level below the function

    # Find Destroyer.playpens
    destroyer = next(d for d in program.declarations if d.name == "Destroyer")
    playpens = next(fn for fn in destroyer.functions if fn.name == "playpens")

    print(f"Before regen: commences.inlining_scope = {playpens.params[0].inlining_scope.name}")

    # Force body regeneration
    playpens.body = ast.BottomConstant(playpens.ret_type)

    # Enter the same call-context stack _gen_func_body uses
    with gen.context.call_contexts(
            subtree_pushed_call_context=[
                InliningSource(playpens, IrFunctionBodyStub()),
                None,
            ]
    ):
        new_body = gen._gen_func_body(playpens.ret_type, playpens)

    playpens.body = new_body

    print(f"After regen:  commences.inlining_scope = {playpens.params[0].inlining_scope.name}")
    print("Body:")
    print(new_body)


if __name__ == "__main__":
    main()