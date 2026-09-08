from pathlib import Path

from tests._env_guard import missing_distributions, required_distributions


def test_requirements_names_are_parsed_without_their_specifiers(tmp_path: Path):
    req = tmp_path / "requirements.txt"
    req.write_text(
        "requests>=2.32.0\n"
        "python-dotenv>=1.0.1\n"
        "\n"
        "# a comment line\n"
        "pytest>=8.0.0  # trailing comment\n"
        "-e .\n"
        "websockets>=14.0\n"
    )
    assert required_distributions(req) == [
        "requests", "python-dotenv", "pytest", "websockets",
    ]


def test_this_project_declares_the_two_that_went_missing():
    # websockets and playwright are absent from the system interpreter; they are
    # the exact pair whose absence was misread as a project failure.
    names = required_distributions(Path(__file__).resolve().parent.parent / "requirements.txt")
    assert {"websockets", "playwright"} <= set(names)


def test_missing_is_reported_by_distribution_name_not_import_name():
    # python-dotenv imports as `dotenv`. Checking distributions is what keeps
    # this working without a hand-maintained alias map.
    assert missing_distributions(["python-dotenv"]) == []


def test_an_absent_distribution_is_reported():
    assert missing_distributions(["definitely-not-installed-xyz"]) == ["definitely-not-installed-xyz"]


def test_the_running_interpreter_satisfies_the_requirements():
    # If this fails, the suite is running somewhere it cannot produce evidence.
    root = Path(__file__).resolve().parent.parent
    assert missing_distributions(required_distributions(root / "requirements.txt")) == []
