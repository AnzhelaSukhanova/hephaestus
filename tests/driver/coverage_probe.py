from tests.driver.support import load_driver


def child_marker() -> None:
    driver = load_driver()
    assert driver.artifacts.get_generator_dir(987).endswith('/generator/iter_987')