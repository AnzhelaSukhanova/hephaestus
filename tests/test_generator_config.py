import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from src.generators.config import GenConfig, Probabilities, cfg


ROOT = Path(__file__).parents[1]


def test_default_generator_config_matches_current_defaults():
    with open("tests/resources/default_generator_config.json") as config_file:
        default_config = json.load(config_file)

    assert default_config == json.loads(cfg.to_json())


def test_probability_configuration_always_has_crossmodule_probability():
    config = deepcopy(cfg)

    assert GenConfig.__annotations__['prob'] is Probabilities
    assert isinstance(config.prob, Probabilities)
    assert config.prob.crossmodule_probability == 0.5

    config.json_config({
        'prob': {
            'crossmodule_probability': 0.25,
        },
    })

    assert isinstance(config.prob, Probabilities)
    assert config.prob.crossmodule_probability == 0.25


def test_non_kotlin_arguments_disable_crossmodule_by_default():
    script = """
import json
from src.args import args
from src.generators.config import cfg

print(json.dumps(cfg.prob.crossmodule_probability))
"""
    probability = subprocess.run(
        [sys.executable, '-c', script, '--language', 'java'],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert json.loads(probability) == 0.0


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

    with pytest.raises(AssertionError):
        config.json_config({
            "prob": {
                "class_type": {
                    "regular": 1.0,
                    "abstract": 1.0,
                    "interface": 0.0,
                },
            },
        })
