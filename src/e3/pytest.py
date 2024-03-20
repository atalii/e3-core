from __future__ import annotations

from tempfile import mkdtemp, NamedTemporaryFile

import logging
import os

from e3.config import Config
from e3.env import Env
from e3.fs import rm
from e3.os.fs import cd, mv, which

import e3.log

from coverage.sqldata import CoverageData
from coverage.files import PathAliases
from coverage import Coverage


import pytest

import typing

if typing.TYPE_CHECKING:
    from typing import Callable


test_errors = False

# Detect that we're in CI mode, most providers set the $CI environment variable
IN_CI_MODE = "CI" in os.environ

DEFAULT_EXCLUDE_LIST = (
    "all: no cover",
    "if TYPE_CHECKING:",
    "@abstractmethod",
    "# os-specific",
    "defensive code",
    "assert_never()",
)


def require_tool(toolname: str) -> Callable:
    """Require a specific tool to run the test.

    When in "CI" mode, a missing tool generates an error. In other
    modes the test is just skipped.

    :param toolname: name of a tool, e.g. git
    """

    def wrapper(request: pytest.FixtureRequest) -> None:
        if not which(toolname):
            if IN_CI_MODE:
                pytest.fail(f"{toolname} not available")
            else:
                pytest.skip(f"{toolname} not available")

    return pytest.fixture(wrapper)


def pytest_addoption(
    parser: pytest.Parser, pluginmanager: pytest.PytestPluginManager
) -> None:
    group = parser.getgroup("e3")
    group.addoption("--e3", action="store_true", help="Use e3 fixtures and reporting")
    group.addoption("--e3-cov-rewrite", nargs=2, help="Use e3 fixtures and reporting")


@pytest.fixture(autouse=True)
def env_protect(request: pytest.FixtureRequest) -> None:
    """Protection against environment change.

    The fixture is enabled for all tests and does the following:

    * store/restore env between each tests
    * create a temporary directory and do a cd to it before each
      test. The directory is automatically removed when test ends
    """
    if request.config.getoption("e3"):
        Env().store()
        tempd = mkdtemp()
        cd(tempd)
        Config.data = {}

        os.environ["TZ"] = "UTC"
        os.environ["E3_ENABLE_FEATURE"] = ""
        os.environ["E3_CONFIG"] = "/dev/null"
        if "E3_HOSTNAME" in os.environ:
            del os.environ["E3_HOSTNAME"]

        def restore_env() -> None:
            Env().restore()
            rm(tempd, True)

        e3.log.activate(level=logging.DEBUG, e3_debug=True)
        request.addfinalizer(restore_env)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Manage the exit code depending on if errors were detected or not."""
    if not session.config.getoption("e3"):
        return
    global test_errors
    if test_errors:
        # Return with an exit code of `3` if we encountered errors (not failures).
        # This is the exit code that corresponds to an "internal error" according to the
        # pytest docs, which is the closest thing to having an actual Python error in
        # test code.
        session.exitstatus = 3

    if session.config.getoption("cov_source"):
        cov_file = str(session.config.rootpath / ".coverage")
        if session.config.getoption("e3_cov_rewrite"):
            origin_dir, new_dir = session.config.getoption("e3_cov_rewrite")
            fix_coverage_paths(origin_dir=origin_dir, new_dir=new_dir, cov_db=cov_file)

        os_name = Env().build.os.name

        cov = Coverage(data_file=cov_file)

        # Exclude all lines matching the DEFAULT_EXCLUDE_LIST
        for regex in DEFAULT_EXCLUDE_LIST:
            cov.exclude(regex)

        # Exclude all <os>-only line (unless that's for this OS)
        for regex in (
            "%s-only" % o
            for o in ("darwin", "linux", "solaris", "windows", "bsd", "aix")
            if o != os_name
        ):
            cov.exclude(regex)

        # Handling of Windows
        if os_name == "windows":
            cov.exclude('if sys.platform != "win32":')
        else:
            cov.exclude('if sys.platform == "win32":')
            cov.exclude("unix: no cover")

        # Exclude no cover for this OS
        cov.exclude(f"{os_name}: no cover")

        cov.load()

        # Read configuration files in <root>/tests/coverage to fix the list
        # of files to omit for OS specific list
        # We're relying on the default coverage configuration for the
        # platform-agnostic list
        omit_files: list[str] = cov.get_option("run:omit") or []  # type: ignore
        coverage_conf_dir = session.config.rootpath / "tests" / "coverage"
        conf_file = coverage_conf_dir / f"omit-files-{os_name}"
        if conf_file.exists():
            with conf_file.open() as f:
                for line in f:
                    omit_files.append(line.rstrip())

        # cov.html_report(directory=str(session.config.rootpath ), omit=omit_files)
        cov.html_report(omit=omit_files)
        cov.report(omit=omit_files, precision=3)
        cov.xml_report()


def fix_coverage_paths(origin_dir: str, new_dir: str, cov_db: str) -> None:
    """Fix coverage paths.

    :param origin_dir: path to the package directory, e.g.
        .tox/py311-cov-xdist/lib/python3.11/site-packages/e3
    :param new_dir: path to the dir that should be visible instead of origin_dir
        e.g. src/
    :param cov_db: path to the .coverage data base
    """
    paths = PathAliases()
    paths.add(origin_dir, new_dir)

    old_cov_file = NamedTemporaryFile(dir=os.path.dirname(cov_db))
    old_cov_file.close()
    try:
        mv(cov_db, old_cov_file.name)

        old_coverage_data = CoverageData(old_cov_file.name)
        old_coverage_data.read()
        new_coverage_data = CoverageData(cov_db)
        new_coverage_data.update(old_coverage_data, aliases=paths)
        new_coverage_data.write()
    finally:
        os.unlink(old_cov_file.name)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> None:  # type: ignore
    """Generate results file.

    When the variable results_dir is set to an existing directory, the testsuite
    will generate results file in "anod" format.
    """
    global test_errors
    # execute all other hooks to obtain the report object
    outcome = yield
    rep = outcome.get_result()
    results_dir = os.environ.get("RESULTS_DIR")

    if not results_dir or not os.path.isdir(results_dir):
        return

    # we only look at actual test calls, not setup/teardown
    if rep.when == "call":
        outcome = rep.outcome.upper()
        test_name = rep.nodeid.replace("/", ".").replace("::", "--")
        if rep.longreprtext:
            with open(os.path.join(results_dir, f"{test_name}.diff"), "w") as f:
                f.write(rep.longreprtext)

        with open(os.path.join(results_dir, "results"), "a") as f:
            f.write(f"{test_name}:{outcome}\n")
    else:
        # If we detect a failure in an item that is not a "proper" test call, it's most
        # likely an error.
        # For example, this could be a failing assertion or a syntax error in a
        # setup/teardown context.
        if rep.outcome == "failed":
            test_errors = True
