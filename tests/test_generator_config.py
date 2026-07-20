import json
from copy import deepcopy

import pytest

from src.generators.config import cfg


def test_default_generator_config_matches_current_defaults():
    with open("tests/resources/default_generator_config.json") as config_file:
        default_config = json.load(config_file)

    assert default_config == json.loads(cfg.to_json())


def test_generator_config_overrides_nested_values():
    config = deepcopy(cfg)

    config.json_config({
        "limits": {
            "max_depth": 4,
            "max_type_params": 2,
        },
        "prob": {
            "class_type": {
                "regular": 0.5,
                "abstract": 0.25,
                "interface": 0.25,
            },
            "class_methods_modality": {
                "override_final": 0.2,
                "declaration_final": 0.8,
            },
        },
    })

    assert config.limits.max_depth == 4
    assert config.limits.max_type_params == 2
    assert config.prob.class_type.regular == 0.5
    assert config.prob.class_type.abstract == 0.25
    assert config.prob.class_type.interface == 0.25
    assert config.prob.class_methods_modality.override_final == 0.2
    assert config.prob.class_methods_modality.declaration_final == 0.8


def test_generator_config_validates_probability_groups():
    config = deepcopy(cfg)

    with pytest.raises(AssertionError, match="class type probabilities"):
        config.json_config({
            "prob": {
                "class_type": {
                    "regular": 1.0,
                    "abstract": 1.0,
                    "interface": 0.0,
                },
            },
        })
