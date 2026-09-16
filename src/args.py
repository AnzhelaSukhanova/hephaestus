import argparse
import json
import os
import sys
from src.utils import random, mkdir
from src.modules.processor import ProgramProcessor
from src.generators.config import cfg


cwd = os.getcwd()

parser = argparse.ArgumentParser()
parser.add_argument(
    "-s", "--seconds",
    type=int,
    help="Timeout in seconds"
)
parser.add_argument(
    "-i", "--iterations",
    type=int,
    default=1,
    help="Iterations to run (default: 3)"
)
parser.add_argument(
    "-t", "--transformations",
    type=int,
    default=0,
    help="Number of transformations in each round"
)
parser.add_argument(
    "--batch",
    type=int,
    default=1,
    help='Number of programs to generate before invoking the compiler'
)
parser.add_argument(
    "-b", "--bugs",
    default=os.path.join(cwd, "bugs"),
    help="Set bug directory (default: " + str(os.path.join(cwd, "bugs")) + ")"
)
parser.add_argument(
    "-n", "--name",
    default=random.str(),
    help="Set name of this testing instance (default: random string)"
)
parser.add_argument(
    "-T", "--transformation-types",
    default=ProgramProcessor.CP_TRANSFORMATIONS.keys(),
    nargs="*",
    choices=ProgramProcessor.CP_TRANSFORMATIONS.keys(),
    help="Select specific transformations to perform"
)
parser.add_argument(
    "--transformation-schedule",
    default=None,
    type=str,
    help="A file containing the schedule of transformations"
)
parser.add_argument(
    "-R", "--replay",
    help="Give a program to use instead of a randomly generated (pickled)"
)
parser.add_argument(
    "-e", "--examine",
    action="store_true",
    help="Open ipdb for a program (can be used only with --replay option)"
)
parser.add_argument(
    "-k", "--keep-all",
    action="store_true",
    help="Save all programs"
)
parser.add_argument(
    "--keep-everything",
    action="store_true",
    help=("Save generator/transformation artifacts and preserve final "
          "per-program directories for passed programs too")
)
parser.add_argument(
    "-S", "--print-stacktrace",
    action="store_true",
    help="When an error occurs print stack trace"
)
parser.add_argument(
    "-w", "--workers",
    type=int,
    default=None,
    help="Number of workers for processing test programs"
)
parser.add_argument(
    "-d", "--debug",
    action="store_true"
)
parser.add_argument(
    "-r", "--rerun",
    action="store_true",
    help=("Run only the last transformation. If failed, start from the last "
          "and go back until the transformation introduces the error")
)
parser.add_argument(
    "-F", "--log-file",
    default=os.path.join(cwd, "logs"),
    help="Set log file (default: " + str(os.path.join(cwd, "logs")) + ")"
)
parser.add_argument(
    "-L", "--log",
    action="store_true",
    help="Keep logs for each transformation (bugs/session/logs)"
)
parser.add_argument(
    "-N", "--dry-run",
    action="store_true",
    help="Do not compile the programs"
)
parser.add_argument(
    "--language",
    default="kotlin",
    choices=['kotlin', 'groovy', 'java', 'scala'],
    help="Select specific language"
)
parser.add_argument(
    "--backend",
    default="native",
    choices=['native', 'js', 'wasm'],
    help="Select Kotlin backend"
)
parser.add_argument(
    "--max-type-params",
    type=int,
    default=None,
    help="Maximum number of type parameters to generate"
)
parser.add_argument(
    "--max-depth",
    type=int,
    default=None,
    help="Generate programs up to the given depth"
)
parser.add_argument(
    "--inline-default-depth",
    type=int,
    default=None,
    help=("Non-leaf levels allowed inside parameter default values "
          "(0 = leaf-only defaults)")
)
parser.add_argument(
    "--generator-config",
    type=str,
    help="Path to a JSON file overriding generator configuration values"
)
parser.add_argument(
    "-P",
    "--only-correctness-preserving-transformations",
    action="store_true",
    help="Use only correctness-preserving transformations"
)
parser.add_argument(
    "--timeout",
    type=int,
    default=600,
    help="Timeout for transformations (in seconds)"
)
parser.add_argument(
    "--cast-numbers",
    action="store_true",
    help=("Cast numeric constants to their actual type"
          " (this option is used to avoid re-occurrence of"
          " a specific Groovy bug)")
)
parser.add_argument(
    "--disable-use-site-variance",
    action="store_true",
    help="Disable use-site variance"
)
parser.add_argument(
    "--disable-contravariance-use-site",
    action="store_true",
    help="Disable contravariance in use-site variance"
)
parser.add_argument(
    "--disable-bounded-type-parameters",
    action="store_true",
    help="Disable bounded type parameters"
)
parser.add_argument(
    "--disable-parameterized-functions",
    action="store_true",
    help="Disable parameterized functions"
)
parser.add_argument(
    "--disable-reified-type-parameters",
    action="store_true",
    help="Disable reified type parameters"
)
parser.add_argument(
    "--error-filter-patterns",
    default='',
    type=str,
    help=("A file containing regular expressions for filtering compiler error "
          "messages")
)
parser.add_argument(
    "--dump-ir",
    action="store_true",
    help="Dump compiler IR for each saved program"
)
parser.add_argument(
    "--time-metrics",
    action="store_true",
    help="Record per-program generation, compilation, and profiling timings"
)
parser.add_argument(
    "--disable-metrics",
    action="store_true",
    help=("Disable compiler error enrichment, phase profiling, and "
          "escalation reports; --time-metrics output is still recorded "
          "when requested")
)
parser.add_argument(
    "--jacoco-lowerings",
    nargs="+",
    default=None,
    help=("Compile a program with the JaCoCo agent immediately (instead of "
          "via the offline src.tools.ir_coverage tool) whenever its IR "
          "changed in one of these lowering phases, refreshing the merged "
          "coverage report after every such program. Requires --dump-ir "
          "and --keep-everything; safe to combine with --workers.")
)
parser.add_argument(
    "--jacoco-always",
    action="store_true",
    help=("Like --jacoco-lowerings, but skip the IR-changed check and "
          "compile with the JaCoCo agent for every preserved program. "
          "Requires --keep-everything; mutually exclusive with "
          "--jacoco-lowerings.")
)


args = parser.parse_args()

if args.keep_everything:
    args.keep_all = True


args.test_directory = os.path.join(args.bugs, args.name)
args.stop_cond = "timeout" if args.seconds else "iterations"
args.temp_directory = os.path.join(cwd, "temp")
args.options = {
    "Generator": {
        "disable_metrics": args.disable_metrics
    },
    'Translator': {
        'cast_numbers': args.cast_numbers,
    },
    "TypeErasure": {
        "timeout": args.timeout
    },
    "TypeOverwriting": {
        "timeout": args.timeout
    }
}
random.remove_reserved_words(args.language)


# Set configurations

if args.language != 'kotlin':
    cfg.prob.crossmodule_probability = 0.0

if args.generator_config:
    try:
        with open(args.generator_config) as config_file:
            cfg.json_config(json.load(config_file))
    except (AssertionError, json.JSONDecodeError, OSError) as exc:
        sys.exit(f"Error loading --generator-config {args.generator_config}: {exc}")

if args.disable_use_site_variance:
    cfg.dis.use_site_variance = True
if args.disable_contravariance_use_site:
    cfg.dis.use_site_contravariance = True
if args.max_type_params is not None:
    cfg.limits.max_type_params = args.max_type_params
if args.max_depth is not None:
    cfg.limits.max_depth = args.max_depth
if args.inline_default_depth is not None:
    cfg.limits.inline_default_depth = args.inline_default_depth
if args.disable_bounded_type_parameters:
    cfg.prob.bounded_type_parameters = 0
if args.disable_parameterized_functions:
    cfg.prob.parameterized_functions = 0
if args.disable_reified_type_parameters:
    cfg.prob.reified_type_parameters = 0


def validate_args(args):
    # CHECK ARGUMENTS

    #TODO: Since for crossmodule support, a lot of calls become qualified, emitting
    # `package src.d<pid>_inc` doesn't make sense anymore as all objects are still internally named `src.d<pid>.f()`. We
    # can make a modification to incorrect oracle to not name package differently, in order to keep qualified names the same
    if (args.language == "kotlin" and
            not args.only_correctness_preserving_transformations):
        sys.exit("Error: Kotlin requires -P (for now)")

    if cfg.prob.crossmodule_probability > 0:
        if args.language != "kotlin":
            sys.exit("Error: cross-module generation is supported only for Kotlin")
        if not args.keep_all:
            sys.exit("Error: cross-module generation requires --keep-all to accumulate .klib's")
        if args.batch != 1:
            sys.exit("Error: cross-module generation requires --batch 1, as batches compile same module")

        #TODO: These can be threaded into crossmodule, although require a bit of compatibility layers
        # can be done as [UP/U] commit after crossmodule support is stable
        if args.replay:
            sys.exit("Error: cross-module generation does not support --replay (for now)")
        if args.rerun:
            sys.exit("Error: cross-module generation does not support --rerun (for now)")
        if args.dry_run:
            sys.exit("Error: cross-module generation does not support --dry-run (for now)")


    if args.seconds and args.iterations:
        sys.exit("Error: you should only set --seconds or --iterations")

    if os.path.isdir(args.bugs) and args.name in os.listdir(args.bugs):
        sys.exit("Error: --name {} already exists".format(args.name))

    if args.transformation_schedule and args.transformations:
        sys.exit("Options --transformation-schedule and --transformations"
                 " are mutually exclusive. You can't use both.")

    if not args.transformation_schedule and args.transformations is None:
        sys.exit("You have to provide one of --transformation-schedule or"
                 " --transformations.")

    if args.transformation_schedule and (
            not os.path.isfile(args.transformation_schedule)):
        sys.exit("You have to provide a valid file in --transformation-schedule")

    if args.rerun and args.workers:
        sys.exit('You cannot use -r option in parallel mode')

    if args.rerun and not args.keep_all:
        sys.exit("The -r option only works with the option -k")

    if args.rerun and args.batch:
        sys.exit("You cannot use -r option with the option --batch")

    if args.examine and not args.replay:
        sys.exit("You cannot use --examine option without the --replay option")

    if args.dump_ir and args.batch != 1:
        sys.exit("You cannot use --dump-ir option with the option --batch != 1")

    if args.time_metrics and args.batch != 1:
        sys.exit("You cannot use --time-metrics with --batch != 1")

    if args.jacoco_lowerings and not args.dump_ir:
        sys.exit("The --jacoco-lowerings option requires --dump-ir")

    if args.jacoco_lowerings and not args.keep_everything:
        sys.exit("The --jacoco-lowerings option requires --keep-everything")

    if args.jacoco_lowerings and args.jacoco_always:
        sys.exit("Options --jacoco-lowerings and --jacoco-always are "
                 "mutually exclusive. You can't use both.")

    if args.jacoco_always and not args.keep_everything:
        sys.exit("The --jacoco-always option requires --keep-everything")


def pre_process_args(args):
    # PRE-PROCESSING

    if not os.path.isdir(args.bugs):
        mkdir(args.bugs)
