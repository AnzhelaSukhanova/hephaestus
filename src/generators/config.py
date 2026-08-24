"""
This file contains the classes that are responsible for configuring the
generation policies.
"""
import json
from dataclasses import dataclass, fields, is_dataclass


class Singleton(type):
    _instances = {}
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


def process_arg(config, name, value):
    assert hasattr(config, name), \
        f"{type(config).__name__} has not {name} argument"
    old_value = getattr(config, name)
    if isinstance(old_value, bool):
        assert isinstance(value, bool), f"{name}={value} is not bool"
        setattr(config, name, value)
    elif isinstance(old_value, int):
        assert isinstance(value, int) and not isinstance(value, bool), \
            f"{name}={value} is not int"
        setattr(config, name, value)
    elif isinstance(old_value, float):
        assert isinstance(value, (int, float)) and not isinstance(value, bool), \
            f"{name}={value} is not int or float"
        setattr(config, name, float(value))
    else:
        assert isinstance(value, dict), \
            f"{name}={value} is not a nested config object"
        for key, val in value.items():
            process_arg(old_value, key, val)


def validate_config(config):
    if is_dataclass(config):
        for field in fields(config):
            validate_config(getattr(config, field.name))
        post_init = getattr(config, "__post_init__", None)
        if post_init:
            post_init()
    elif hasattr(config, "__dict__"):
        for value in vars(config).values():
            validate_config(value)


@dataclass
class ClassLimits:
    max_fields: int
    max_funcs: int


@dataclass
class FunctionLimits:
    max_side_effects: int
    max_params: int


@dataclass
class GenLimits:
    cls: ClassLimits
    fn: FunctionLimits
    max_var_decls: int  # max variable declarations in a scope.
    max_type_params: int # max type parameters in parameterized classes and functions
    max_functional_params: int # max number of parameters in functional interfaces
    max_top_level: int # max number of top-level declarations
    min_top_level: int # min number of top-level declarations
    max_depth: int # max depth of leaves in programs
    inline_default_depth: int # generation depth at the inlined default
    ordinary_default_depth: int # generation depth at the ordinary (not inlined) default

@dataclass
class VisibilityProbabilities:
    public: float
    private: float
    not_specified: float

    def __post_init__(self):
        assert abs(self.public + self.private + self.not_specified - 1.0) <= 1e-9

@dataclass
class ModalityProbabilities:
    override_final: float
    declaration_final: float

@dataclass
class ClassTypeProbabilities:
    regular: float
    abstract: float
    interface: float

    def __post_init__(self):
        assert abs(self.regular + self.abstract + self.interface - 1.0) <= 1e-9

@dataclass
class HelperFunctionProbabilities:
    is_global_method: float
    is_global_function: float
    is_local_function: float

    def __post_init__(self):
        assert abs(self.is_global_method + self.is_global_function + self.is_local_function - 1.0) <= 1e-9

@dataclass
class TopLevelDeclarationProbabilities:
    function_declaration: float
    class_declaration: float
    variable_declaration: float

    def __post_init__(self):
        assert abs(self.function_declaration + self.class_declaration + self.variable_declaration - 1.0) <= 1e-9

# In many scenarios like func_ref_call, there may be a slighter change that
# we will generate the specified expression based on the current program
@dataclass
class Probabilities:
    function_expr: float # functions that their body are expressions
    bounded_type_parameters: float
    parameterized_functions: float
    reified_type_parameters: float
    func_ref_call: float # use function reference call instead of function call
    func_ref: float # generate func_ref instead of lambda
    sam_coercion: float # perform sam coercion whenever possible
    function_visibility: VisibilityProbabilities
    property_visibility: VisibilityProbabilities
    class_methods_modality: ModalityProbabilities
    class_fields_modality: ModalityProbabilities
    class_declaration_modality: ModalityProbabilities # used only for regular classes
    class_field_is_immutable: float # val / var (kotlin)
    override_also_adding_setter: float # override val (getter only), adding additional setter var
    class_type: ClassTypeProbabilities # regular / abstract / interface
    receiver_ascribe_prob_open_regular: float # probability of enforcing hephaestus type in Kotlin vs restricted generation
    receiver_ascribe_prob_interface_abstract: float # probability of enforcing hephaestus type in Kotlin vs restricted generation
    helper_functions: HelperFunctionProbabilities # functions created where we can't find existing function returning this type or with this signature
    top_level_declarations: TopLevelDeclarationProbabilities # top level class / func / variable

# Features that we want to either disable or enable
# If something is set to True then it means it is disabled.
@dataclass
class Disabled:
    use_site_variance: bool
    use_site_contravariance: bool


class GenConfig(metaclass=Singleton):
    def __init__(self):
        self.limits = GenLimits(
            cls=ClassLimits(
                max_fields=2,
                max_funcs=2
            ),
            fn=FunctionLimits(
                max_side_effects=1,
                max_params=2
            ),
            max_var_decls=3,
            max_type_params=3,
            max_functional_params=3,
            max_top_level=10,
            min_top_level=5,
            max_depth=6,
            inline_default_depth=2,
            ordinary_default_depth=2
        )
        self.prob=Probabilities(
            function_expr=1.0,
            bounded_type_parameters=0.5,
            parameterized_functions=0.3,
            reified_type_parameters=0.7,
            func_ref_call=1.0,
            func_ref=0.5,
            sam_coercion=1.0,
            function_visibility=VisibilityProbabilities(
                public=0.2,
                private=0.5,
                not_specified=0.3
            ),
            property_visibility=VisibilityProbabilities(
                public=0.2,
                private=0.5,
                not_specified=0.3
            ),
            class_methods_modality=ModalityProbabilities(
                override_final=0.5,
                declaration_final=0.5
            ),
            class_fields_modality=ModalityProbabilities(
                override_final=0.5,
                declaration_final=0.5
            ),
            class_declaration_modality=ModalityProbabilities(
                override_final=0.5, # currently, due to way how hephaestus generates superclasses, we use `declaration_final` for overrides too
                declaration_final=0.5
            ),
            class_field_is_immutable=0.5,
            override_also_adding_setter=0.5,
            class_type=ClassTypeProbabilities(
                regular=0.5,
                abstract=0.25,
                interface=0.25
            ),
            receiver_ascribe_prob_open_regular=0.5,
            receiver_ascribe_prob_interface_abstract=0.8,
            helper_functions=HelperFunctionProbabilities(
                is_global_method=0.5,
                is_global_function=0.25,
                is_local_function=0.25
            ),
            top_level_declarations=TopLevelDeclarationProbabilities(
                function_declaration=1/3,
                class_declaration=1/3,
                variable_declaration=1/3
            )
        )
        self.dis=Disabled(
            use_site_variance=False,
            use_site_contravariance=False
        )

    def json_config(self, kwargs):
        assert isinstance(kwargs, dict)
        for key, value in kwargs.items():
            process_arg(self, key, value)
        validate_config(self)

    def to_json(self):
        return json.dumps(self, default=lambda o: o.__dict__)


cfg = GenConfig()


def main():
    __import__('pprint').pprint(cfg.to_json())


if __name__ == "__main__":
    main()
