# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""libFuzzer engine interface."""

import os
import re

from bot.fuzzers import dictionary_manager
from bot.fuzzers import engine
from bot.fuzzers import engine_common
from bot.fuzzers import libfuzzer
from bot.fuzzers import strategy_selection
from bot.fuzzers import utils as fuzzer_utils
from bot.fuzzers.libFuzzer import constants
from bot.fuzzers.libFuzzer import fuzzer
from bot.fuzzers.libFuzzer import launcher
from bot.fuzzers.libFuzzer import stats
from fuzzing import strategy
from metrics import logs
from metrics import profiler
from system import shell


class LibFuzzerError(Exception):
  """Base libFuzzer error."""


class MergeError(LibFuzzerError):
  """Merge error."""


class LibFuzzerOptions(engine.FuzzOptions):
  """LibFuzzer engine options."""

  def __init__(self, corpus_dir, arguments, strategies, fuzz_corpus_dirs,
               extra_env, use_dataflow_tracing, is_mutations_run):
    super(LibFuzzerOptions, self).__init__(corpus_dir, arguments, strategies)
    self.fuzz_corpus_dirs = fuzz_corpus_dirs
    self.extra_env = extra_env
    self.use_dataflow_tracing = use_dataflow_tracing
    self.is_mutations_run = is_mutations_run


class LibFuzzerEngine(engine.Engine):
  """LibFuzzer engine implementation."""

  @property
  def name(self):
    return 'libFuzzer'

  def prepare(self, corpus_dir, target_path, _):
    """Prepare for a fuzzing session, by generating options. Returns a
    FuzzOptions object.

    Args:
      corpus_dir: The main corpus directory.
      target_path: Path to the target.
      build_dir: Path to the build directory.

    Returns:
      A FuzzOptions object.
    """
    arguments = fuzzer.get_arguments(target_path)
    strategy_pool = strategy_selection.generate_weighted_strategy_pool(
        strategy_list=strategy.LIBFUZZER_STRATEGY_LIST,
        use_generator=True,
        engine_name=self.name)
    strategy_info = launcher.pick_strategies(strategy_pool, target_path,
                                             corpus_dir, arguments)

    arguments.extend(strategy_info.arguments)

    # Check for seed corpus and add it into corpus directory.
    engine_common.unpack_seed_corpus_if_needed(target_path, corpus_dir)

    # Pick a few testcases from our corpus to use as the initial corpus.
    subset_size = engine_common.random_choice(
        engine_common.CORPUS_SUBSET_NUM_TESTCASES)

    if (not strategy_info.use_dataflow_tracing and
        strategy_pool.do_strategy(strategy.CORPUS_SUBSET_STRATEGY) and
        shell.get_directory_file_count(corpus_dir) > subset_size):
      # Copy |subset_size| testcases into 'subset' directory.
      corpus_subset_dir = self._create_temp_corpus_dir('subset')
      launcher.copy_from_corpus(corpus_subset_dir, corpus_dir, subset_size)
      strategy_info.fuzzing_strategies.append(
          strategy.CORPUS_SUBSET_STRATEGY.name + '_' + str(subset_size))
      strategy_info.additional_corpus_dirs.append(corpus_subset_dir)
    else:
      strategy_info.additional_corpus_dirs.append(corpus_dir)

    # Check dict argument to make sure that it's valid.
    dict_argument = fuzzer_utils.extract_argument(
        arguments, constants.DICT_FLAG, remove=False)
    if dict_argument and not os.path.exists(dict_argument):
      logs.log_error('Invalid dict %s for %s.' % (dict_argument, target_path))
      fuzzer_utils.extract_argument(arguments, constants.DICT_FLAG)

    # If there's no dict argument, check for %target_binary_name%.dict file.
    if (not fuzzer_utils.extract_argument(
        arguments, constants.DICT_FLAG, remove=False)):
      default_dict_path = dictionary_manager.get_default_dictionary_path(
          target_path)
      if os.path.exists(default_dict_path):
        arguments.append(constants.DICT_FLAG + default_dict_path)

    return LibFuzzerOptions(
        corpus_dir, arguments, strategy_info.fuzzing_strategies,
        strategy_info.additional_corpus_dirs, strategy_info.extra_env,
        strategy_info.use_dataflow_tracing, strategy_info.is_mutations_run)

  def _artifact_prefix(self, prefix):
    """Returns the artifact prefix argument."""
    return constants.ARTIFACT_PREFIX_FLAG + prefix + '/'

  def _create_temp_corpus_dir(self, name):
    """Create temporary corpus directory."""
    new_corpus_directory = os.path.join(fuzzer_utils.get_temp_dir(), name)
    engine_common.recreate_directory(new_corpus_directory)
    return new_corpus_directory

  def _merge_new_units(self, target_path, corpus_dir, new_corpus_dir,
                       fuzz_corpus_dirs, arguments, stat_overrides):
    """Merge new units."""
    # Make a decision on whether merge step is needed at all. If there are no
    # new units added by libFuzzer run, then no need to do merge at all.
    new_units_added = shell.get_directory_file_count(new_corpus_dir)
    if not new_units_added:
      stat_overrides['new_units_added'] = 0
      logs.log('Skipped corpus merge since no new units added by fuzzing.')
      return

    # If this times out, it's possible that we will miss some units. However, if
    # we're taking >10 minutes to load/merge the corpus something is going very
    # wrong and we probably don't want to make things worse by adding units
    # anyway.
    merge_corpus = self._create_temp_corpus_dir('merge-corpus')

    merge_dirs = [new_corpus_dir]
    merge_dirs.extend(fuzz_corpus_dirs)

    # Merge the new units with the initial corpus.
    if corpus_dir not in merge_dirs:
      merge_dirs.append(corpus_dir)

    old_corpus_len = shell.get_directory_file_count(corpus_dir)

    new_units_added = 0
    try:
      result = self.minimize_corpus(
          target_path=target_path,
          arguments=arguments,
          output_dir=merge_corpus,
          input_dirs=merge_dirs,
          max_time=engine_common.get_merge_timeout(
              launcher.DEFAULT_MERGE_TIMEOUT))

      launcher.move_mergeable_units(merge_corpus, corpus_dir)
      new_corpus_len = shell.get_directory_file_count(corpus_dir)
      new_units_added = new_corpus_len - old_corpus_len

      if result.logs:
        stat_overrides.update(
            stats.parse_stats_from_merge_log(result.logs.splitlines()))
    except MergeError:
      logs.log_error('Merge failed.')

    stat_overrides['new_units_added'] = new_units_added

  def fuzz(self, target_path, options, reproducers_dir, max_time):
    """Run a fuzz session.

    Args:
      target_path: Path to the target.
      options: The FuzzOptions object returned by prepare().
      reproducers_dir: The directory to put reproducers in when crashes
          are found.
      max_time: Maximum allowed time for the fuzzing to run.

    Returns:
      A Result object.
    """
    profiler.start_if_needed('libfuzzer_fuzz')
    runner = libfuzzer.get_runner(target_path)
    launcher.set_sanitizer_options(target_path)

    artifact_prefix = self._artifact_prefix(os.path.abspath(reproducers_dir))

    # Directory to place new units.
    new_corpus_dir = self._create_temp_corpus_dir('new')

    corpus_directories = [new_corpus_dir] + options.fuzz_corpus_dirs
    fuzz_timeout = launcher.get_fuzz_timeout(
        options.is_mutations_run, total_timeout=max_time)
    fuzz_result = runner.fuzz(
        corpus_directories,
        fuzz_timeout=fuzz_timeout,
        additional_args=options.arguments + [artifact_prefix],
        extra_env=options.extra_env)

    log_lines = fuzz_result.output.splitlines()
    # Output can be large, so save some memory by removing reference to the
    # original output which is no longer needed.
    fuzz_result.output = None

    # Check if we crashed, and get the crash testcase path.
    crash_testcase_file_path = None
    for line in log_lines:
      match = re.match(launcher.CRASH_TESTCASE_REGEX, line)
      if match:
        crash_testcase_file_path = match.group(1)
        break

    # Parse stats information based on libFuzzer output.
    parsed_stats = launcher.parse_log_stats(log_lines)

    # Extend parsed stats by additional performance features.
    parsed_stats.update(
        stats.parse_performance_features(log_lines, options.strategies,
                                         options.arguments))

    # Set some initial stat overrides.
    timeout_limit = fuzzer_utils.extract_argument(
        options.arguments, constants.TIMEOUT_FLAG, remove=False)

    expected_duration = runner.get_max_total_time(fuzz_timeout)
    actual_duration = int(fuzz_result.time_executed)
    fuzzing_time_percent = 100 * actual_duration / float(expected_duration)
    parsed_stats.update({
        'timeout_limit': int(timeout_limit),
        'expected_duration': expected_duration,
        'actual_duration': actual_duration,
        'fuzzing_time_percent': fuzzing_time_percent,
    })

    # Remove fuzzing arguments before merge and dictionary analysis step.
    arguments = options.arguments[:]
    launcher.remove_fuzzing_arguments(arguments)

    self._merge_new_units(target_path, options.corpus_dir, new_corpus_dir,
                          options.fuzz_corpus_dirs, arguments, parsed_stats)

    fuzz_logs = '\n'.join(log_lines)
    crashes = []
    if crash_testcase_file_path:
      # Write the new testcase.
      # Copy crash testcase contents into the main testcase path.
      crashes.append(
          engine.Crash(crash_testcase_file_path, fuzz_logs, arguments,
                       actual_duration))

    # TODO(ochang): Custom crash state.
    # TODO(ochang): Recommended dictionary.

    return engine.Result(fuzz_logs, fuzz_result.command, crashes, parsed_stats,
                         fuzz_result.time_executed)

  def reproduce(self, target_path, input_path, arguments, max_time):
    """Reproduce a crash given an input.

    Args:
      target_path: Path to the target.
      input_path: Path to the reproducer input.
      arguments: Additional arguments needed for reproduction.
      max_time: Maximum allowed time for the reproduction.

    Returns:
      A ReproduceResult.
    """
    runner = libfuzzer.get_runner(target_path)
    launcher.set_sanitizer_options(target_path)

    result = runner.run_single_testcase(
        input_path, timeout=max_time, additional_args=arguments)
    return engine.ReproduceResult(result.return_code, result.time_executed,
                                  result.output)

  def minimize_corpus(self, target_path, arguments, output_dir, input_dirs,
                      max_time):
    """Optional (but recommended): run corpus minimization.

    Args:
      target_path: Path to the target.
      arguments: Additional arguments needed for corpus minimization.
      output_dir: Output directory to place minimized corpus.
      input_dirs: Input corpora.
      max_time: Maximum allowed time for the minimization.

    Returns:
      A Result object.
    """
    runner = libfuzzer.get_runner(target_path)
    launcher.set_sanitizer_options(target_path)
    merge_tmp_dir = self._create_temp_corpus_dir('merge-workdir')

    merge_result = runner.merge(
        [output_dir] + input_dirs,
        merge_timeout=max_time,
        tmp_dir=merge_tmp_dir,
        additional_args=arguments)

    if merge_result.timed_out:
      raise MergeError('Merging new testcases timed out')

    if merge_result.return_code != 0:
      raise MergeError('Merging new testcases failed')

    # TODO(ochang): Get crashes found during merge.
    return engine.Result(merge_result.output, merge_result.command, [], {},
                         merge_result.time_executed)

  def minimize_testcase(self, target_path, arguments, input_path, output_path,
                        max_time):
    """Optional (but recommended): Minimize a testcase.

    Args:
      target_path: Path to the target.
      arguments: Additional arguments needed for testcase minimization.
      input_path: Path to the reproducer input.
      output_path: Path to the minimized output.
      max_time: Maximum allowed time for the minimization.

    Returns:
      A boolean indicating success.
    """
    runner = libfuzzer.get_runner(target_path)
    launcher.set_sanitizer_options(target_path)

    minimize_tmp_dir = self._create_temp_corpus_dir('minimize-workdir')
    artifact_prefix = self._artifact_prefix(minimize_tmp_dir)
    result = runner.minimize_crash(
        input_path,
        output_path,
        max_time,
        additional_args=arguments + [artifact_prefix])

    return result.return_code == 0

  def cleanse(self, target_path, arguments, input_path, output_path, max_time):
    """Optional (but recommended): Cleanse a testcase.

    Args:
      target_path: Path to the target.
      arguments: Additional arguments needed for testcase cleanse.
      input_path: Path to the reproducer input.
      output_path: Path to the cleansed output.
      max_time: Maximum allowed time for the cleanse.

    Returns:
      A boolean indicating success.
    """
    runner = libfuzzer.get_runner(target_path)
    launcher.set_sanitizer_options(target_path)

    cleanse_tmp_dir = self._create_temp_corpus_dir('cleanse-workdir')
    artifact_prefix = self._artifact_prefix(cleanse_tmp_dir)
    result = runner.cleanse_crash(
        input_path,
        output_path,
        max_time,
        additional_args=arguments + [artifact_prefix])

    return result.return_code == 0
