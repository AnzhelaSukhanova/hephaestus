# pylint: disable=too-few-public-methods
import sys
from collections.abc import Mapping
from typing import Protocol

from src.generators.generator import EscalationLog, Generator
from src.ir import ast
from src.ir.context import Context
from src.transformations.base import Transformation
from src.transformations.type_erasure import TypeErasure
from src.transformations.type_overwriting import TypeOverwriting
from src.utils import random, read_lines, load_program
from src.modules.logging import Logger


class ProcessorArgs(Protocol):
    transformation_types: list[str]
    transformations: int | None
    transformation_schedule: str
    replay: str | None
    log: bool
    debug: bool
    name: str
    test_directory: str
    language: str
    options: Mapping[str, Mapping[str, int]]


class ProgramProcessor():

    # Correctness-preserving transformations
    CP_TRANSFORMATIONS: dict[str, type[Transformation]] = {
        'TypeErasure': TypeErasure,
    }

    # Non correctness-preserving transformations
    NCP_TRANSFORMATIONS: dict[str, type[TypeOverwriting]] = {
        'TypeOverwriting': TypeOverwriting,
    }

    def __init__(self, proc_id: int, args: ProcessorArgs,
                 target_module: str | None = None) -> None:
        self.proc_id = proc_id
        self.args = args
        self.target_module = target_module
        self.transformations: list[type[Transformation]] = [
            ProgramProcessor.CP_TRANSFORMATIONS[t]
            for t in self.args.transformation_types
        ]
        self.ncp_transformations: list[type[TypeOverwriting]] = list(
            ProgramProcessor.NCP_TRANSFORMATIONS.values())
        self.transformation_schedule: list[type[Transformation]] = self._get_transformation_schedule()
        self.current_transformation: int = 0
        self.escalations: list[EscalationLog] = []

    def _apply_transformation[T: Transformation](self, transformation_cls: type[T],
                                                 transformation_number: int, program: ast.Program
                                                 ) -> tuple[ast.Program, T]:
        if self.args.log:
            logger = Logger(self.args.name, self.args.test_directory,
                            self.proc_id, transformation_cls.get_name(),
                            transformation_number)
        else:
            logger = None
        transformer = transformation_cls(
            program, self.args.language, logger,
            self.args.options[transformation_cls.get_name()])
        if self.args.debug:
            print("Transformation " + str(transformation_number) + ": " +
                  transformer.get_name())
        transformer.transform()
        program = transformer.result()
        return program, transformer

    def _get_transformation_schedule(self) -> list[type[Transformation]]:
        if self.args.transformations is not None:
            # Randomly generate a transformation schedule.
            return [
                random.choice(self.transformations)
                for i in range(self.args.transformations)
            ]
        # Get transformation schedule from file.
        lines = read_lines(self.args.transformation_schedule)
        schedule: list[type[Transformation]] = []
        for line in lines:
            transformation = self.CP_TRANSFORMATIONS.get(line)
            if transformation is None:
                sys.exit(
                    "Transformation " + line
                    + " found in schedule is not valid.")
            schedule.append(transformation)
        return schedule

    def get_program(self, pre_existing_context: Context | None = None
                    ) -> tuple[ast.Program, bool]:
        if self.args.replay:
            if self.args.debug:
                print("\nLoading program: " + self.args.replay)
            # Load the program from the given file.
            return load_program(self.args.replay), True
        else:
            # Generate a new program.
            return self.generate_program(pre_existing_context=pre_existing_context)

    def get_transformations(self) -> list[type[Transformation]]:
        return self.transformation_schedule[:self.current_transformation]

    def generate_program(self, pre_existing_context: Context | None = None
                         ) -> tuple[ast.Program, bool]:
        if self.args.debug:
            print("\nGenerating program: " + str(self.proc_id))
        if self.args.log:
            logger = Logger(self.args.name, self.args.test_directory,
                            self.proc_id, "Generator",
                            self.proc_id)
        else:
            logger = None
        generator = Generator(
            language=self.args.language,
            logger=logger,
            options=self.args.options["Generator"],
            target_module=self.target_module)
        program = generator.generate(context=pre_existing_context)
        self.escalations = generator.escalations
        return program, True

    def can_transform(self) -> bool:
        return self.current_transformation < len(self.transformation_schedule)

    def transform_program(self, program: ast.Program
                          ) -> tuple[ast.Program, bool | None] | None:
        transformer_cls = (
            self.transformation_schedule[self.current_transformation])
        program, transformer = self._apply_transformation(
            transformer_cls, self.current_transformation + 1, program)
        self.current_transformation += 1
        if not transformer.is_transformed:
            return None
        return program, transformer.preserve_correctness()

    def inject_fault(self, program: ast.Program
                     ) -> tuple[ast.Program, str | None] | None:
        transformer_cls = random.choice(self.ncp_transformations)
        program, transformer = self._apply_transformation(
            transformer_cls, self.current_transformation + 1, program)
        self.current_transformation += 1
        if not transformer.is_transformed:
            return None
        return program, transformer.error_injected
