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
"""Constants that are meaningful to libFuzzer.
Should not have any dependancies.
Note that libFuzzers arguments take the form -flag=value. Thus any variables
defined in this function that end with the suffix "_FLAG" should contain
"-flag=". Any variable that ends with the suffix "_ARGUMENT" should contain
"-flag=value".
"""

# libFuzzer flags.
ARTIFACT_PREFIX_FLAG = '-artifact_prefix='

COLLECT_DATA_FLOW_FLAG = '-collect_data_flow='

DICT_FLAG = '-dict='

FORK_FLAG = '-fork='

MAX_LEN_FLAG = '-max_len='

MAX_TOTAL_TIME_FLAG = '-max_total_time='

RSS_LIMIT_FLAG = '-rss_limit_mb='

RUNS_FLAG = '-runs='

TIMEOUT_FLAG = '-timeout='

EXACT_ARTIFACT_PATH_FLAG = '-exact_artifact_path='

# libFuzzer arguments.
ANALYZE_DICT_ARGUMENT = '-analyze_dict=1'

CLEANSE_CRASH_ARGUMENT = '-cleanse_crash=1'

MERGE_ARGUMENT = '-merge=1'

MINIMIZE_CRASH_ARGUMENT = '-minimize_crash=1'

PRINT_FINAL_STATS_ARGUMENT = '-print_final_stats=1'

TMP_ARTIFACT_PREFIX_ARGUMENT = ARTIFACT_PREFIX_FLAG + '/tmp/'

VALUE_PROFILE_ARGUMENT = '-use_value_profile=1'

# Value for RSS_LIMIT_FLAG to catch OOM.
DEFAULT_RSS_LIMIT_MB = 2048

DEFAULT_TIMEOUT_LIMIT = 25

# libFuzzer's exit code if a bug occurred in libFuzzer.
LIBFUZZER_ERROR_EXITCODE = 1

# Defines value of runs argument when loading a testcase.
RUNS_TO_REPRODUCE = 100

# libFuzzer's exit code if a bug was found in the target code.
TARGET_ERROR_EXITCODE = 77
