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
"""Environment functions."""

from builtins import range
import ast
import functools
import os
import re
import six
import socket
import sys
import yaml

try:
  from shlex import quote
except ImportError:
  from pipes import quote

# Tools supporting customization of options via ADDITIONAL_{TOOL_NAME}_OPTIONS.
# FIXME: Support ADDITIONAL_UBSAN_OPTIONS and ADDITIONAL_LSAN_OPTIONS in an
# ASAN instrumented build.
SUPPORTED_MEMORY_TOOLS_FOR_OPTIONS = ['ASAN', 'CFI', 'MSAN', 'TSAN', 'UBSAN']

SANITIZER_NAME_MAP = {
    'ASAN': 'address',
    'CFI': 'cfi',
    'MSAN': 'memory',
    'TSAN': 'thread',
    'UBSAN': 'undefined',
}

COMMON_SANITIZER_OPTIONS = {
    'handle_abort': 1,
    'handle_segv': 1,
    'handle_sigbus': 1,
    'handle_sigfpe': 1,
    'handle_sigill': 1,
    'print_summary': 1,
    'use_sigaltstack': 1,
}


def _eval_value(value_string):
  """Returns evaluated value."""
  try:
    return ast.literal_eval(value_string)
  except:
    # String fallback.
    return value_string


def _join_memory_tool_options(options):
  """Joins a dict holding memory tool options into a string that can be set in
  the environment."""
  return ':'.join(
      '%s=%s' % (key, str(value)) for key, value in six.iteritems(options))


def _maybe_convert_to_int(value):
  """Returns the int representation contained by string |value| if it contains
  one. Otherwise returns |value|."""
  try:
    return int(value)
  except ValueError:
    return value


# Matches anything that isn't an unquoted (ie: not between two single or two
# double quotes) colon.
UNQUOTED_COLON_REGEX = re.compile('((?:[^\'":]|\'[^\']*\'|"[^"]*")+)')


def _parse_memory_tool_options(options_str):
  """Parses memory tool options into a dict."""
  parsed = {}

  for item in UNQUOTED_COLON_REGEX.split(options_str):
    # Regex split can give us empty strings at the beginning and the end. Skip
    # these.
    if not item:
      continue
    # Regex split gives us each ':'. Skip these.
    if item == ':':
      continue
    values = item.split('=', 1)
    if len(values) != 2:
      # TODO(mbarbella): Factor this out of environment, and switch to logging
      # an error and continuing. This error should be recoverable.
      raise ValueError('Invalid memory tool option "%s"' % item)

    option_name = values[0]
    option_value = _maybe_convert_to_int(values[1])
    parsed[option_name] = option_value

  return parsed


def _quote_value_if_needed(value):
  """Quote environment value as needed for certain platforms like Windows."""
  result = value

  bot_platform = platform()
  if bot_platform == 'WINDOWS':
    result = '"%s"' % result

  return result


def copy():
  """Return a safe copy of the environment."""
  environment_copy = os.environ.copy()
  return environment_copy


def get_asan_options(redzone_size, malloc_context_size, quarantine_size_mb,
                     bot_platform, leaks):
  """Generates default ASAN options."""
  asan_options = {}

  # Default options needed for all cases.
  asan_options['alloc_dealloc_mismatch'] = 0
  asan_options['print_scariness'] = 1
  asan_options['strict_memcmp'] = 0

  # Set provided redzone size.
  if redzone_size:
    asan_options['redzone'] = redzone_size

    # This value is used in determining whether to report OOM crashes or not.
    set_value('REDZONE', redzone_size)

  # Set maximum number of stack frames to report.
  if malloc_context_size:
    asan_options['malloc_context_size'] = malloc_context_size

  # Set quarantine size.
  if quarantine_size_mb:
    asan_options['quarantine_size_mb'] = quarantine_size_mb

  # Test for leaks if this is an LSan-enabled job type.
  if get_value('LSAN') and leaks:
    lsan_options = _join_memory_tool_options(get_lsan_options())
    set_value('LSAN_OPTIONS', lsan_options)
    asan_options['detect_leaks'] = 1
  else:
    remove_key('LSAN_OPTIONS')
    asan_options['detect_leaks'] = 0

  # FIXME: Support container overflow on Android.
  if bot_platform == 'ANDROID':
    asan_options['detect_container_overflow'] = 0

  # Enable stack use-after-return.
  asan_options['detect_stack_use_after_return'] = 1
  asan_options['max_uar_stack_size_log'] = 16

  # Other less important default options for all cases.
  asan_options.update({
      'allocator_may_return_null': 1,
      'allow_user_segv_handler': 0,
      'check_malloc_usable_size': 0,
      'detect_odr_violation': 0,
      'fast_unwind_on_fatal': 1,
      'print_suppressions': 0,
  })

  # Add common sanitizer options.
  asan_options.update(COMMON_SANITIZER_OPTIONS)

  # FIXME: For Windows, rely on online symbolization since llvm-symbolizer.exe
  # in build archive does not work.
  asan_options['symbolize'] = int(bot_platform == 'WINDOWS')

  # For Android, allow user defined segv handler to work.
  if bot_platform == 'ANDROID':
    asan_options['allow_user_segv_handler'] = 1

  # Check if UBSAN is enabled as well for this ASAN build.
  # If yes, set UBSAN_OPTIONS and enable suppressions.
  if get_value('UBSAN'):
    ubsan_options = _join_memory_tool_options(get_ubsan_options())
    set_value('UBSAN_OPTIONS', ubsan_options)

    # FIXME: Weird bug with UBSAN suppressions. If it is in an ASAN build, it
    # uses the suppression file provided in ASAN_OPTIONS and not in
    # UBSAN_OPTIONS. So, set the same suppressions path in ASAN_OPTIONS as well.
    ubsan_suppressions_path = get_suppressions_file('ubsan')
    if ubsan_suppressions_path:
      asan_options['suppressions'] = ubsan_suppressions_path

  return asan_options


def get_cpu_arch():
  """Return cpu architecture."""
  if platform() == 'ANDROID':
    # FIXME: Handle this import in a cleaner way.
    from platforms import android
    return android.settings.get_cpu_arch()

  # FIXME: Add support for desktop architectures as needed.
  return None


def get_current_memory_tool_var():
  """Get the environment variable name for the current job type's sanitizer."""
  memory_tool_name = get_memory_tool_name(get_value('JOB_NAME'))
  if not memory_tool_name:
    return None

  return memory_tool_name + '_OPTIONS'


def get_memory_tool_options(env_var, default_value=None):
  """Get the current memory tool options as a dict. Returns |default_value| if
  |env_var| isn't set. Otherwise returns a dictionary containing the memory tool
  options and their values."""
  env_value = get_value(env_var)
  if env_value is not None:
    return _parse_memory_tool_options(env_value)

  return default_value


def get_instrumented_libraries_paths():
  """Get the instrumented libraries path for the current sanitizer."""
  memory_tool_name = get_memory_tool_name(get_value('JOB_NAME'))
  if not memory_tool_name:
    return None

  if memory_tool_name == 'MSAN':
    if 'no-origins' in get_value('BUILD_URL', ''):
      memory_tool_name += '_NO_ORIGINS'
    else:
      memory_tool_name += '_CHAINED'

  paths = get_value('INSTRUMENTED_LIBRARIES_PATHS_' + memory_tool_name)
  if not paths:
    return None

  return paths.split(':')


def get_default_tool_path(tool_name):
  """Get the default tool for this platform (from scripts/ dir)."""
  if platform().lower() == 'android':
    # For android devices, we do symbolization on the host machine, which is
    # linux. So, we use the linux version of llvm-symbolizer.
    platform_override = 'linux'
  else:
    # No override needed, use default.
    platform_override = None

  tool_filename = get_executable_filename(tool_name)
  tool_path = os.path.join(
      get_platform_resources_directory(platform_override), tool_filename)
  return tool_path


def get_environment_settings_as_string():
  """Return environment settings as a string. Includes settings for memory
  debugging tools (e.g. ASAN_OPTIONS for ASAN), application binary revision,
  application command line, etc."""
  environment_string = ''

  # Add Android specific variables.
  if platform() == 'ANDROID':
    # FIXME: Handle this import in a cleaner way.
    from platforms import android

    environment_string += '[Environment] Build fingerprint: %s\n' % (
        get_value('BUILD_FINGERPRINT'))

    environment_string += ('[Environment] Patch level: %s\n' %
                           android.settings.get_security_patch_level())

    environment_string += (
        '[Environment] Local properties file "%s" with contents:\n%s\n' %
        (android.device.LOCAL_PROP_PATH,
         android.adb.read_data_from_file(android.device.LOCAL_PROP_PATH)))

    command_line = get_value('COMMAND_LINE_PATH')
    if command_line:
      environment_string += (
          '[Environment] Command line file "%s" with contents:\n%s\n' %
          (command_line, android.adb.read_data_from_file(command_line)))

    asan_options = get_value('ASAN_OPTIONS')
    if asan_options:
      # FIXME: Need better documentation for Chrome builds. Chrome builds use
      # asan_device_setup.sh and we send this options file path as an include
      # to extra-options parameter.
      sanitizer_options_file_path = (
          android.sanitizer.get_options_file_path('ASAN'))
      environment_string += (
          '[Environment] ASAN options file "%s" with contents:\n%s\n' %
          (sanitizer_options_file_path, asan_options))

  else:
    # For desktop platforms, add |*_OPTIONS| variables from environment.
    for tool in SUPPORTED_MEMORY_TOOLS_FOR_OPTIONS:
      environment_variable = tool + '_OPTIONS'
      environment_variable_value = os.getenv(environment_variable)
      if not environment_variable_value:
        continue

      environment_string += '[Environment] %s="%s"\n' % (
          environment_variable, quote(environment_variable_value))

  return environment_string


def get_llvm_symbolizer_path():
  """Get the path of the llvm-symbolizer binary."""
  llvm_symbolizer_path = get_value('LLVM_SYMBOLIZER_PATH')

  # Use default llvm symbolizer for the following:
  # 1. If we don't have |LLVM_SYMBOLIZER_PATH| env variable set.
  # 2. If this build is deleted, then our own llvm symbolizer.
  if not llvm_symbolizer_path or not os.path.exists(llvm_symbolizer_path):
    llvm_symbolizer_path = get_default_tool_path('llvm-symbolizer')

  # Make sure that llvm symbolizer binary exists.
  if not os.path.exists(llvm_symbolizer_path):
    return None

  # Make sure that llvm symbolizer binary is executable.
  os.chmod(llvm_symbolizer_path, 0o750)
  return llvm_symbolizer_path


def get_root_directory():
  """Return root directory."""
  return get_value('ROOT_DIR')


def get_startup_scripts_directory():
  """Return path to startup scripts."""
  return os.path.join(get_value('ROOT_DIR'), 'src', 'python', 'bot', 'startup')


def get_config_directory():
  """Return the path to the configs directory."""
  config_dir = get_value('CONFIG_DIR_OVERRIDE')
  if config_dir:
    return config_dir

  if is_running_on_app_engine():
    # Root is already src/appengine.
    return 'config'

  # Running on bot, give path to config folder inside appengine dir.
  return os.path.join(get_root_directory(), 'src', 'appengine', 'config')


def get_gae_config_directory():
  """Return the path to the google appengine configs directory."""
  return os.path.join(get_config_directory(), 'gae')


def get_gce_config_directory():
  """Return the path to the google compute engine configs directory."""
  return os.path.join(get_config_directory(), 'gce')


def get_resources_directory():
  """Return the path to the resources directory."""
  return os.path.join(get_root_directory(), 'resources')


def get_platform_resources_directory(platform_override=None):
  """Return the path to platform-specific resources directory."""
  return os.path.join(get_resources_directory(), 'platform',
                      platform_override or platform().lower())


def get_suppressions_directory():
  """Return the path to the suppressions directory."""
  return os.path.join(get_config_directory(), 'suppressions')


def get_suppressions_file(sanitizer, suffix='suppressions'):
  """Return the path to sanitizer suppressions file, if exists."""
  sanitizer_suppressions_filename = '{sanitizer}_{suffix}.txt'.format(
      sanitizer=sanitizer, suffix=suffix)
  sanitizer_suppressions_file_path = os.path.join(
      get_suppressions_directory(), sanitizer_suppressions_filename)

  if not os.path.exists(sanitizer_suppressions_file_path):
    return None

  if not os.path.getsize(sanitizer_suppressions_file_path):
    return None

  return sanitizer_suppressions_file_path


def get_lsan_options():
  """Generates default LSAN options."""
  symbolizer_path = get_llvm_symbolizer_path()
  lsan_suppressions_path = get_suppressions_file('lsan')
  lsan_options = {
      'external_symbolizer_path': symbolizer_path,
      'print_suppressions': '0',
      'symbolize': '1',
  }

  # Add common sanitizer options.
  lsan_options.update(COMMON_SANITIZER_OPTIONS)

  if lsan_suppressions_path:
    lsan_options['suppressions'] = lsan_suppressions_path

  return lsan_options


def get_msan_options():
  """Generates default MSAN options."""
  msan_options = {'symbolize': 0}

  # Add common sanitizer options.
  msan_options.update(COMMON_SANITIZER_OPTIONS)

  return msan_options


def get_platform_id():
  """Return a platform id as a lowercase string."""
  bot_platform = platform()
  if bot_platform == 'ANDROID':
    # FIXME: Handle this import in a cleaner way.
    from platforms import android

    platform_id = get_value('PLATFORM_ID', android.settings.get_platform_id())
    return platform_id.lower()

  return bot_platform.lower()


def get_platform_group():
  """Return the platform group (specified via QUEUE_OVERRIDE) if it
  exists, otherwise platform()."""
  platform_group = get_value('QUEUE_OVERRIDE')
  if platform_group:
    return platform_group

  return platform()


def get_memory_tool_name(job_name):
  """Figures out name of memory debugging tool."""
  for tool in SUPPORTED_MEMORY_TOOLS_FOR_OPTIONS:
    if tool_matches(tool, job_name):
      return tool

  # If no tool specified, assume it is ASAN. Also takes care of LSAN job type.
  return 'ASAN'


def get_memory_tool_display_string(job_name):
  """Return memory tool string for a testcase."""
  memory_tool_name = get_memory_tool_name(job_name)
  sanitizer_name = SANITIZER_NAME_MAP.get(memory_tool_name)
  if not sanitizer_name:
    return 'Memory Tool: %s' % memory_tool_name

  return 'Sanitizer: %s (%s)' % (sanitizer_name, memory_tool_name)


def get_executable_filename(executable_name):
  """Return the filename for the given executable."""
  if platform() != 'WINDOWS':
    return executable_name

  extension = '.exe'
  if executable_name.endswith(extension):
    return executable_name

  return executable_name + extension


def get_tsan_options(symbolize_inline_frames):
  """Generates default TSAN options."""
  symbolizer_path = get_llvm_symbolizer_path()
  tsan_suppressions_path = get_suppressions_file('tsan')

  tsan_options = {
      'atexit_sleep_ms': '200',
      'flush_memory_ms': '2000',
      'history_size': '3',
      'print_suppressions': '0',
      'report_thread_leaks': '0',
      'report_signal_unsafe': '0',
      'stack_trace_format': 'DEFAULT',
      'symbolize': '1',
      'external_symbolizer_path': symbolizer_path,
      'symbolize_inline_frames': str(symbolize_inline_frames).lower()
  }

  # Add common sanitizer options.
  tsan_options.update(COMMON_SANITIZER_OPTIONS)

  if tsan_suppressions_path:
    tsan_options['suppressions'] = tsan_suppressions_path

  return tsan_options


def get_ubsan_options():
  """Generates default UBSAN options."""
  # Note that UBSAN can work together with ASAN as well.
  symbolizer_path = get_llvm_symbolizer_path()
  ubsan_suppressions_path = get_suppressions_file('ubsan')

  ubsan_options = {
      'external_symbolizer_path': symbolizer_path,
      'halt_on_error': '1',
      'print_stacktrace': '1',
      'print_suppressions': '0',

      # We use -fsanitize=unsigned-integer-overflow as an additional coverage
      # signal and do not want those errors to be reported by UBSan as bugs.
      # See https://github.com/google/oss-fuzz/issues/910 for additional info.
      'silence_unsigned_overflow': '1',
      'symbolize': '1',
  }

  # Add common sanitizer options.
  ubsan_options.update(COMMON_SANITIZER_OPTIONS)

  # TODO(crbug.com/877070): Make this code configurable on a per job basis.
  if ubsan_suppressions_path and not is_chromeos_system_job():
    ubsan_options['suppressions'] = ubsan_suppressions_path

  return ubsan_options


def get_value(environment_variable, default_value=None):
  """Return an environment variable value."""
  value_string = os.getenv(environment_variable)

  # value_string will be None if the variable is not defined.
  if value_string is None:
    return default_value

  # Exception for ANDROID_SERIAL. Sometimes serial can be just numbers,
  # so we don't want to it eval it.
  if environment_variable == 'ANDROID_SERIAL':
    return value_string

  # Evaluate the value of the environment variable with string fallback.
  return _eval_value(value_string)


def _job_substring_match(search_string, job_name):
  """Return a bool on whether a string exists in a provided job name or
  use from environment if available (case insensitive)."""
  job_name = job_name or get_value('JOB_NAME')
  if not job_name:
    return False

  return search_string in job_name.lower()


def is_afl_job(job_name=None):
  """Return true if the current job uses AFL."""
  # Prefix matching is not sufficient.
  return _job_substring_match('afl', job_name)


def is_chromeos_job(job_name=None):
  """Return True if the current job is for ChromeOS."""
  return _job_substring_match('chromeos', job_name)


def is_chromeos_system_job(job_name=None):
  """Return True if the current job is for ChromeOS system (i.e. not libFuzzer
  or entire Chrome browser for Chrome on ChromeOS)."""
  return is_chromeos_job(job_name) and get_value('CHROMEOS_SYSTEM')


def is_libfuzzer_job(job_name=None):
  """Return true if the current job uses libFuzzer."""
  # Prefix matching is not sufficient.
  return _job_substring_match('libfuzzer', job_name)


def is_engine_fuzzer_job(job_name=None):
  """Return if true is this is an engine fuzzer."""
  return is_afl_job(job_name) or is_libfuzzer_job(job_name)


def is_posix():
  """Return true if we are on a posix platform (linux/unix and mac os)."""
  return os.name == 'posix'


def is_trusted_host(ensure_connected=True):
  """Return whether or not the current bot is a trusted host."""
  return get_value('TRUSTED_HOST') and (not ensure_connected or
                                        get_value('WORKER_BOT_NAME'))


def is_untrusted_worker():
  """Return whether or not the current bot is an untrusted worker."""
  return get_value('UNTRUSTED_WORKER')


def is_running_on_app_engine():
  """Return True if we are running on appengine (local or production)."""
  return (is_running_on_app_engine_development() or
          os.getenv('SERVER_SOFTWARE', '').startswith('Google App Engine/'))


def is_running_on_app_engine_development():
  """Return True if running on the local development appengine server."""
  return os.getenv('SERVER_SOFTWARE', '').startswith('Development/')


def parse_environment_definition(environment_string):
  """Parses a job's environment definition."""
  definitions = [environment_string.splitlines()]
  values = {}
  for definition in definitions:
    for line in definition:
      if line.startswith('#') or not line.strip():
        continue

      m = re.match('([^ =]+)[ ]*=[ ]*(.*)', line)
      if m:
        key = m.group(1).strip()
        value = m.group(2).strip()
        values[key] = value

  return values


def platform():
  """Return the operating system type, unless an override is provided."""
  environment_override = get_value('OS_OVERRIDE')
  if environment_override:
    return environment_override.upper()

  if sys.platform.startswith('win'):
    return 'WINDOWS'
  elif sys.platform.startswith('linux'):
    return 'LINUX'
  elif sys.platform == 'darwin':
    return 'MAC'

  raise ValueError('Unsupported platform "%s".' % sys.platform)


def remove_key(key_name):
  """Remove environment |key| and its associated value."""
  if not key_name:
    return

  if key_name not in os.environ:
    return

  del os.environ[key_name]


# Used by reset_environment to store the initial environment.
_initial_environment = None


def reset_environment():
  """Resets environment variables to their initial state. Saves the initial
    state on first call."""
  global _initial_environment
  if _initial_environment is None:
    _initial_environment = copy()
    # There is nothing to reset if we are initializing for the first time.
  else:
    # Clean current environment.
    os.environ.clear()

    # Add shared variables with values from _initial_environment.
    os.environ.update(_initial_environment)

  if is_trusted_host():
    from bot.untrusted_runner import environment as untrusted_env
    untrusted_env.reset_environment()


def set_common_environment_variables():
  """Sets environment variables common for different memory debugging tools."""
  # G_SLICE = always-malloc: make glib use system malloc.
  # NSS_DISABLE_UNLOAD = 1: make nss skip dlclosing dynamically loaded modules,
  # which would result in "obj:*" in backtraces.
  # NSS_DISABLE_ARENA_FREE_LIST = 1: make nss use system malloc.
  set_value('G_SLICE', 'always-malloc')
  set_value('NSS_DISABLE_UNLOAD', '1')
  set_value('NSS_DISABLE_ARENA_FREE_LIST', '1')
  set_value('NACL_DANGEROUS_SKIP_QUALIFICATION_TEST', '1')


def set_memory_tool_options(env_var, options_dict):
  """Set current memory tool options."""
  set_value(env_var, _join_memory_tool_options(options_dict))


def set_environment_parameters_from_file(file_path):
  """Set environment variables from a file."""
  if not os.path.exists(file_path):
    return

  with open(file_path, 'r') as f:
    file_data = f.read()

  for line in file_data.splitlines():
    if line.startswith('#') or not line.strip():
      continue

    m = re.match('([^ =]+)[ ]*=[ ]*(.*)', line)
    if m:
      environment_variable = m.group(1)
      environment_variable_value = m.group(2)
      set_value(environment_variable, environment_variable_value)


def reset_current_memory_tool_options(redzone_size=0,
                                      malloc_context_size=0,
                                      leaks=True,
                                      symbolize_inline_frames=False,
                                      quarantine_size_mb=None):
  """Resets environment variables for memory debugging tool to default
  values."""
  # FIXME: Handle these imports in a cleaner way.
  from platforms import android

  # Set common environment variable useful for memory debugging tools.
  set_common_environment_variables()

  # Set memory tool name in our environment for easy access.
  job_name = get_value('JOB_NAME')
  tool_name = get_memory_tool_name(job_name)
  set_value('MEMORY_TOOL', tool_name)

  bot_platform = platform()

  # Default options for memory debuggin tool used.
  if tool_name == 'ASAN':
    tool_options = get_asan_options(redzone_size, malloc_context_size,
                                    quarantine_size_mb, bot_platform, leaks)
  elif tool_name == 'MSAN':
    tool_options = get_msan_options()
  elif tool_name == 'TSAN':
    tool_options = get_tsan_options(symbolize_inline_frames)
  elif tool_name == 'UBSAN' or tool_name == 'CFI':
    tool_options = get_ubsan_options()

  # Additional options. These override the defaults.
  additional_tool_options = get_value('ADDITIONAL_%s_OPTIONS' % tool_name)
  if additional_tool_options:
    tool_options.update(_parse_memory_tool_options(additional_tool_options))

  # If symbolize flag is set, add path to external symbolizer.
  if (tool_options.get('symbolize', 0) == 1 and
      'external_symbolizer_path' not in tool_options):
    llvm_symbolizer_path_arg = _quote_value_if_needed(
        get_llvm_symbolizer_path())
    tool_options.update({'external_symbolizer_path': llvm_symbolizer_path_arg})

  # Join the options.
  joined_tool_options = _join_memory_tool_options(tool_options)
  tool_options_variable_name = '%s_OPTIONS' % tool_name
  set_value(tool_options_variable_name, joined_tool_options)

  # CFI handles various signals through the UBSan runtime, so need to set
  # UBSAN_OPTIONS explicitly. See crbug.com/716235#c25
  if tool_name == 'CFI':
    set_value('UBSAN_OPTIONS', joined_tool_options)

  # For Android, we need to set shell property |asan.options|.
  if bot_platform == 'ANDROID':
    android.sanitizer.set_options(tool_name, joined_tool_options)


def set_default_vars():
  """Set default environment vars and values."""
  env_file_path = os.path.join(get_value('ROOT_DIR'), 'bot', 'env.yaml')
  with open(env_file_path) as file_handle:
    env_file_contents = file_handle.read()

  env_vars_and_values = yaml.safe_load(env_file_contents)
  for variable, value in six.iteritems(env_vars_and_values):
    # We cannot call set_value here.
    os.environ[variable] = str(value)


def set_bot_environment():
  """Set environment for the bots."""
  root_dir = get_value('ROOT_DIR')

  if not root_dir:
    # Error, bail out.
    return False

  # Reset our current working directory. Our's last job might
  # have left us in a non-existent temp directory.
  # Or ROOT_DIR might be deleted and recreated.
  os.chdir(root_dir)

  # Set some default directories. These can be overriden by config files below.
  bot_dir = os.path.join(root_dir, 'bot')
  if is_trusted_host(ensure_connected=False):
    worker_root_dir = os.environ['WORKER_ROOT_DIR']
    os.environ['BUILDS_DIR'] = os.path.join(worker_root_dir, 'bot', 'builds')
  else:
    os.environ['BUILDS_DIR'] = os.path.join(bot_dir, 'builds')

  os.environ['BUILD_URLS_DIR'] = os.path.join(bot_dir, 'build-urls')
  os.environ['LOG_DIR'] = os.path.join(bot_dir, 'logs')
  os.environ['CACHE_DIR'] = os.path.join(bot_dir, 'cache')
  os.environ['RESOURCES_DIR'] = os.path.join(bot_dir, 'resources')

  inputs_dir = os.path.join(bot_dir, 'inputs')
  os.environ['INPUT_DIR'] = inputs_dir
  os.environ['CRASH_STACKTRACES_DIR'] = os.path.join(inputs_dir, 'crash-stacks')
  os.environ['FUZZERS_DIR'] = os.path.join(inputs_dir, 'fuzzers')
  os.environ['DATA_BUNDLES_DIR'] = os.path.join(inputs_dir, 'data-bundles')
  os.environ['FUZZ_INPUTS'] = os.path.join(inputs_dir, 'fuzzer-testcases')
  os.environ['FUZZ_INPUTS_MEMORY'] = os.environ['FUZZ_INPUTS']
  os.environ['FUZZ_INPUTS_DISK'] = os.path.join(inputs_dir,
                                                'fuzzer-testcases-disk')
  os.environ['MUTATOR_PLUGINS_DIR'] = os.path.join(inputs_dir,
                                                   'mutator-plugins')
  os.environ['FUZZ_DATA'] = os.path.join(inputs_dir,
                                         'fuzzer-common-data-bundles')
  os.environ['IMAGES_DIR'] = os.path.join(inputs_dir, 'images')
  os.environ['SYMBOLS_DIR'] = os.path.join(inputs_dir, 'symbols')
  os.environ['USER_PROFILE_ROOT_DIR'] = os.path.join(inputs_dir,
                                                     'user-profile-dirs')

  # Set bot name.
  if not get_value('BOT_NAME'):
    # If not defined, default to host name.
    os.environ['BOT_NAME'] = socket.gethostname().lower()

  # Set BOT_TMPDIR if not already set.
  if not get_value('BOT_TMPDIR'):
    os.environ['BOT_TMPDIR'] = os.path.join(bot_dir, 'tmp')

  # Sets the default configuration. Can be overridden by job environment.
  set_default_vars()

  # Set environment variable from local project configuration.
  from config import local_config
  local_config.ProjectConfig().set_environment()

  # Success.
  return True


def set_tsan_max_history_size():
  """Sets maximum history size for TSAN tool."""
  tsan_options = get_value('TSAN_OPTIONS')
  if not tsan_options:
    return

  tsan_max_history_size = 7
  for i in range(tsan_max_history_size):
    tsan_options = (
        tsan_options.replace('history_size=%d' % i,
                             'history_size=%d' % tsan_max_history_size))

  set_value('TSAN_OPTIONS', tsan_options)


def set_value(environment_variable, value):
  """Set an environment variable."""
  value_str = str(value)
  environment_variable_str = str(environment_variable)
  value_str = value_str.replace('%ROOT_DIR%', os.environ['ROOT_DIR'])
  os.environ[environment_variable_str] = value_str

  if is_trusted_host():
    from bot.untrusted_runner import environment as untrusted_env
    untrusted_env.forward_environment_variable(environment_variable_str,
                                               value_str)


def tool_matches(tool_name, job_name):
  """Return if the memory debugging tool is used in this job."""
  match_prefix = '(.*[^a-zA-Z]|^)%s'
  matches_tool = re.match(match_prefix % tool_name.lower(), job_name.lower())
  return bool(matches_tool)


def appengine_noop(func):
  """Wrap a function into no-op and return None if running on App Engine."""

  @functools.wraps(func)
  def wrapper(*args, **kwargs):
    if is_running_on_app_engine():
      return None

    return func(*args, **kwargs)

  return wrapper


def bot_noop(func):
  """Wrap a function into no-op and return None if running on bot."""

  @functools.wraps(func)
  def wrapper(*args, **kwargs):
    is_bot = not is_running_on_app_engine()
    if is_bot:
      return None

    return func(*args, **kwargs)

  return wrapper


def is_local_development():
  """Return true if running in local development environment (e.g. running
  a bot locally, excludes tests)."""
  return bool(get_value('LOCAL_DEVELOPMENT') and not get_value('PY_UNITTESTS'))


def local_noop(func):
  """Wrap a function into no-op and return None if running in local
  development environment."""

  @functools.wraps(func)
  def wrapper(*args, **kwargs):
    if (is_local_development() or is_running_on_app_engine_development()):
      return None

    return func(*args, **kwargs)

  return wrapper
