# Copyright 2019 The Pigweed Authors
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.
"""Tools for running presubmit checks in a Git repository.

A presubmit checks are defined as a function or other callable. The function may
take either no arguments or a list of the paths on which to run. Presubmit
checks communicate failure by raising any exception.

For example, either of these functions may be used as presubmit checks:

  def file_contains_ni(paths):
      for path in paths:
          with open(path) as file:
            contents = file.read()
            if 'ni' not in contents and 'nee' not in contents:
              raise PresumitFailure('Files must say "ni"!', path=path)

  def run_the_build():
      subprocess.run(['make', 'release'], check=True)

Presubmit checks are provided to the parse_args_and_run_presubmit or
run_presubmit function as a list. For example,

  PRESUBMIT_CHECKS = [file_contains_ni, run_the_build]
  sys.exit(0 if parse_args_and_run_presubmit(PRESUBMIT_CHECKS) else 1)

Presubmit checks that accept a list of paths may use the filter_paths decorator
to automatically filter the paths list for file types they care about. See the
pragma_once function for an example.
"""

import argparse
import collections
import enum
import functools
import re
import os
import shlex
import subprocess
import sys
import time
from typing import Callable, Dict, Iterable, List, Optional, Sequence
from inspect import signature


def plural(items_or_count, singular: str, count_format='') -> str:
    """Returns the singular or plural form of a word based on a count."""

    try:
        count = len(items_or_count)
    except TypeError:
        count = items_or_count

    num = f'{count:{count_format}}'

    if singular.endswith('y'):
        return f'{num} {singular[:-1]}{"y" if count == 1 else "ies"}'
    if singular.endswith('s'):
        return f'{num} {singular}{"" if count == 1 else "es"}'
    return f'{num} {singular}{"" if count == 1 else "s"}'


def git_stdout(*args: str) -> str:
    return subprocess.run(('git', ) + args, stdout=subprocess.PIPE,
                          check=True).stdout.decode().strip()


def git_list_files(*args: str) -> Sequence[str]:
    return git_stdout('ls-files', *args).split()


def git_diff_names(against: str = 'HEAD',
                   paths: Sequence[str] = ()) -> Sequence[str]:
    return git_stdout('diff', '--name-only', against, '--', *paths).split()


def list_files(commit: Optional[str] = None,
               paths: Sequence[str] = (),
               exclude: Sequence = ()) -> Sequence[str]:
    """Lists files changed since the specified commit."""

    return [
        path for path in
        (git_diff_names(commit, paths) if commit else git_list_files(*paths))
        if not any(pattern.search(path) for pattern in exclude)
    ]


def git_repo_root(path: str = './') -> str:
    return git_stdout('-C', path, 'rev-parse', '--show-toplevel')


def _make_color(*codes: int):
    start = ''.join(f'\033[{code}m' for code in codes)
    return f'{start}{{}}\033[0m'.format if os.name == 'posix' else str


color_red = _make_color(31)
color_bold_red = _make_color(31, 1)
color_black_on_red = _make_color(30, 41)
color_yellow = _make_color(33, 1)
color_green = _make_color(32)
color_black_on_green = _make_color(30, 42)


def _make_box(section_alignments: Sequence[str]) -> str:
    indices = [i + 1 for i in range(len(section_alignments))]
    top_sections = '{2}'.join('{1:{1}^{width%d}}' % i for i in indices)
    mid_sections = '{5}'.join('{section%d:%s{width%d}}' %
                              (i, section_alignments[i - 1], i)
                              for i in indices)
    bot_sections = '{9}'.join('{8:{8}^{width%d}}' % i for i in indices)

    # yapf: disable
    return ''.join(['{0}', *top_sections, '{3}\n',
                    '{4}', *mid_sections, '{6}\n',
                    '{7}', *bot_sections, '{10}'])
    # yapf: enable


_DOUBLE = '╔═╦╗║║║╚═╩╝'
_TOP = '┏━┯┓┃│┃┖┄┴┚'
_BOTTOM = '┎┄┬┒┃│┃┗━┷┛'

WIDTH = 80


def _title(msg, style=_DOUBLE):
    msg = f' {msg} '.center(WIDTH - 4)
    return _make_box('^').format(*style, section1=msg, width1=len(msg))


def _start_box(style, count, title, box=_make_box('><')):
    info = f' {title} '.ljust(WIDTH - 13)

    return box.format(*style,
                      section1=count + ' ',
                      width1=8,
                      section2=info,
                      width2=len(info))


def _result_box(style, result, color, title, time_s, box=_make_box('^<^')):
    minutes, seconds = divmod(time_s, 60)
    time_str = f' {int(minutes)}:{seconds:04.1f} '
    info = f' {title} '.ljust(WIDTH - 14 - len(time_str))

    return box.format(*style,
                      section1=f' {color(result.center(6))} ',
                      width1=8,
                      section2=info,
                      width2=len(info),
                      section3=time_str,
                      width3=len(time_str))


class PresubmitFailure(Exception):
    """Optional exception to use for presubmit failures."""
    def __init__(self, description: str = '', path: Optional[str] = None):
        super().__init__(f'{path}: {description}' if path else description)


class _Result(enum.Enum):

    PASS = 'PASSED'  # Check completed successfully.
    FAIL = 'FAILED'  # Check failed.
    CANCEL = 'CANCEL'  # Check didn't complete.

    def color(self) -> Callable:
        if self is _Result.PASS:
            return color_green
        if self is _Result.FAIL:
            return color_bold_red
        if self is _Result.CANCEL:
            return color_yellow


class Presubmit:
    """Runs a series of presubmit checks on a list of files."""
    def __init__(self, root: str, paths: Sequence[str]):
        self.root = root
        self.paths = paths
        self.log = print

    def run(self, program: Sequence[Callable],
            continue_on_error: bool = False) -> bool:
        """Executes a series of presubmit checks on the paths."""

        self.log(_title(f'Presubmit checks for {os.path.basename(self.root)}'))
        self._log_presubmit_start(program)

        passed: List[Callable] = []
        failed: List[Callable] = []
        skipped: List[Callable] = []

        start_time: float = time.time()

        for i, check in enumerate(program):
            self.log(_start_box(_TOP, f'{i+1}/{len(program)}', check.__name__))

            time_s = time.time()
            result = self._run_check(check)
            time_s = time.time() - time_s

            self.log(
                _result_box(_BOTTOM, result.value, result.color(),
                            check.__name__, time_s))

            if result is _Result.PASS:
                passed.append(check)
            elif result is _Result.CANCEL:
                skipped = list(program[i:])
                break
            else:
                failed.append(check)

                if not continue_on_error:
                    skipped = list(program[i + 1:])
                    break

        self._log_summary(time.time() - start_time, len(passed), len(failed),
                          len(skipped))
        return not failed and not skipped

    def _run_check(self, check: Callable) -> _Result:
        try:
            check(*([self.paths] if signature(check).parameters else []))
            return _Result.PASS
        except Exception as failure:
            if str(failure):
                self.log(failure)
            return _Result.FAIL
        except KeyboardInterrupt:
            self.log()
            return _Result.CANCEL

    def _log_presubmit_start(self, program: Sequence[Callable]) -> None:
        self.log(f'Running {plural(program, "check")} on '
                 f'{plural(self.paths, "file")} in {self.root}\n')

        files = collections.Counter(
            os.path.splitext(path)[1] or os.path.basename(path)
            for path in self.paths)
        if files:
            width = max(len(f) for f in files) + 2
            max_count_width = len(str(max(files.values())))

            for ext, count in sorted(files.items()):
                self.log(
                    f'{ext:>{width}}: {plural(count, "file", max_count_width)}'
                )

    def _log_summary(self, time_s: float, passed: int, failed: int,
                     skipped: int) -> None:
        text = 'FAILED' if failed else 'PASSED'
        color = color_black_on_red if failed else color_black_on_green

        summary = []
        if passed:
            summary.append(f'{passed} passed')
        if failed:
            summary.append(f'{failed} failed')
        if skipped:
            summary.append(f'{skipped} skipped')

        self.log(
            _result_box(
                _DOUBLE, text, color,
                f'{passed + failed + skipped} checks: {", ".join(summary)}',
                time_s))


def add_parser_arguments(parser) -> None:
    """Adds common presubmit check options to an argument parser."""

    parser.add_argument(
        'paths',
        nargs='*',
        help=('Paths to which to restrict the presubmit checks. '
              'Used in conjunction with --base, if provided.'))
    parser.add_argument(
        '-b',
        '--base',
        metavar='COMMIT',
        help=('Git revision against which to diff for changed files. '
              'If none is provided, the entire repository is used.'))
    parser.add_argument(
        '-r',
        '--repository',
        default='.',
        help=('Path to the repository in which to run the checks; '
              "defaults to the current directory's Git repo."))
    parser.add_argument(
        '-e',
        '--exclude',
        metavar='REGULAR_EXPRESSION',
        default=[],
        action='append',
        type=re.compile,
        help='Exclude paths matching any of these regular expressions.')
    parser.add_argument('--continue',
                        dest='continue_on_error',
                        action='store_true',
                        help='Continue instead of aborting when errors occur.')


def run_presubmit(
        program: Sequence[Callable],
        base: Optional[str] = None,
        paths: Sequence[str] = (),
        exclude: Sequence = (),
        repository: str = '.',
        continue_on_error: bool = False,
) -> bool:
    """Lists files in the current Git repo and runs a Presubmit with them."""

    root = git_repo_root(repository)
    os.chdir(root)
    return Presubmit(root, list_files(base, paths,
                                      exclude)).run(program, continue_on_error)


def parse_args_and_run_presubmit(
        program: Sequence[Callable],
        arg_parser: Optional[argparse.ArgumentParser] = None) -> bool:
    """Parses the command line arguments and calls run_presubmit with them."""

    if arg_parser is None:
        arg_parser = argparse.ArgumentParser(
            description='Runs presubmit checks on a Git repository.',
            formatter_class=argparse.RawDescriptionHelpFormatter)

    add_parser_arguments(arg_parser)
    return run_presubmit(program, **vars(arg_parser.parse_args()))


def find_python_packages(python_paths) -> Dict[str, List[str]]:
    """Returns Python package directories for the files in python_paths."""
    setup_pys = [
        os.path.dirname(file)
        for file in git_list_files('setup.py', '*/setup.py')
    ]

    package_dirs: dict = collections.defaultdict(list)

    for path in python_paths:
        try:
            setup_dir = max(setup for setup in setup_pys
                            if path.startswith(setup))
            package_dirs[os.path.abspath(setup_dir)].append(path)
        except ValueError:
            continue

    return package_dirs


def _wrap_if_str(value: Iterable[str]) -> Iterable[str]:
    return [value] if isinstance(value, str) else value


def filter_paths(endswith: Iterable[str] = (''),
                 exclude: Iterable = (),
                 skip_if_empty: bool = True):
    """Decorator for filtering the files list for a presubmit check function."""
    endswith = frozenset(_wrap_if_str(endswith))
    exclude = [re.compile(exp) for exp in _wrap_if_str(exclude)]

    def filter_paths_for_function(function: Callable):
        if len(signature(function).parameters) != 1:
            raise TypeError('Functions wrapped with @filter_paths must take '
                            f'exactly one argument: {function.__name__} takes '
                            f'{len(signature(function).parameters)}.')

        @functools.wraps(function)
        def wrapped_function(paths: Iterable[str]):
            paths = [
                path for path in paths
                if any(path.endswith(end) for end in endswith) and not any(
                    exp.fullmatch(path) for exp in exclude)
            ]

            if not paths and skip_if_empty:
                print('Skipping check: no affected files')
            else:
                return function(paths)

        return wrapped_function

    return filter_paths_for_function


def call(*args, print_output=True, **kwargs) -> None:
    """Optional subprocess wrapper with helpful output."""
    print('[COMMAND]',
          ', '.join(f'{k}={v}' for k, v in sorted(kwargs.items())))
    print(' '.join(shlex.quote(arg) for arg in args))

    process = subprocess.run(args,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             **kwargs)
    if process.returncode:
        print(f'[RESULT] Failed with return code {process.returncode}.')
        output = process.stdout.decode(errors='backslashreplace')

        if print_output:
            print('[OUTPUT]')
            print(output)
            raise PresubmitFailure

        raise PresubmitFailure(output)


@filter_paths(endswith='.h')
def pragma_once(paths: Iterable[str]) -> None:
    """Checks that all header files contain '#pragma once'."""

    for path in paths:
        with open(path) as file:
            for line in file:
                if line.startswith('#pragma once'):
                    break
            else:
                raise PresubmitFailure('#pragma once is missing!', path=path)


if __name__ == '__main__':
    # As an example, run a presubmit with the pragma_once check.
    sys.exit(0 if parse_args_and_run_presubmit([pragma_once]) else 1)