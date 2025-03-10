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
"""Helper functions to file issues."""

import itertools
import re

from base import external_users
from base import utils
from crash_analysis import severity_analyzer
from datastore import data_handler
from datastore import data_types
from libs.issue_management import issue_tracker_policy
from system import environment

NON_CRASH_TYPES = [
    'Data race',
    'Direct-leak',
    'Float-cast-overflow',
    'Incorrect-function-pointer-type',
    'Integer-overflow',
    'Non-positive-vla-bound-value',
    'RUNTIME_ASSERT',
    'Undefined-shift',
    'Unsigned-integer-overflow',
]

MEMORY_TOOLS_LABELS = [
    {
        'token': 'AddressSanitizer',
        'label': 'Memory-AddressSanitizer'
    },
    {
        'token': 'LeakSanitizer',
        'label': 'Memory-LeakSanitizer'
    },
    {
        'token': 'MemorySanitizer',
        'label': 'Memory-MemorySanitizer'
    },
    {
        'token': 'ThreadSanitizer',
        'label': 'ThreadSanitizer'
    },
    {
        'token': 'UndefinedBehaviorSanitizer',
        'label': 'UndefinedBehaviorSanitizer'
    },
    {
        'token': 'afl',
        'label': 'AFL'
    },
    {
        'token': 'libfuzzer',
        'label': 'LibFuzzer'
    },
]

STACKFRAME_LINE_REGEX = re.compile(r'\s*#\d+\s+0x[0-9A-Fa-f]+\s*')


def platform_substitution(label, testcase, _):
  """Platform substitution."""
  platform = None
  if environment.is_chromeos_job(testcase.job_type):
    # ChromeOS fuzzers run on Linux platform, so use correct OS-Chrome for
    # tracking.
    platform = 'Chrome'
  elif testcase.platform_id:
    platform = testcase.platform_id.split(':')[0].capitalize()

  if not platform:
    return []

  return [label.replace('%PLATFORM%', platform)]


def current_date():
  """Date format."""
  return utils.utcnow().date().isoformat()


def date_substitution(label, *_):
  """Date substitution."""
  return [label.replace('%YYYY-MM-DD%', current_date())]


def sanitizer_substitution(label, testcase, _):
  """Sanitizer substitution."""
  stacktrace = data_handler.get_stacktrace(testcase)
  memory_tool_labels = get_memory_tool_labels(stacktrace)

  return [
      label.replace('%SANITIZER%', memory_tool)
      for memory_tool in memory_tool_labels
  ]


def severity_substitution(label, testcase, security_severity):
  """Severity substitution."""
  # Use severity from testcase if one is not available.
  if security_severity is None:
    security_severity = testcase.security_severity

  # Set to default high severity if we can't determine it automatically.
  if not data_types.SecuritySeverity.is_valid(security_severity):
    security_severity = data_types.SecuritySeverity.HIGH

  security_severity_string = severity_analyzer.severity_to_string(
      security_severity)
  return [label.replace('%SEVERITY%', security_severity_string)]


def impact_to_string(impact):
  """Convert an impact value to a human-readable string."""
  impact_map = {
      data_types.SecurityImpact.STABLE: 'Stable',
      data_types.SecurityImpact.BETA: 'Beta',
      data_types.SecurityImpact.HEAD: 'Head',
      data_types.SecurityImpact.NONE: 'None',
      data_types.SecurityImpact.MISSING: data_types.MISSING_VALUE_STRING,
  }

  return impact_map[impact]


def _get_impact_from_labels(labels):
  """Get the impact from the label list."""
  labels = [label.lower() for label in labels]
  if 'security_impact-stable' in labels:
    return data_types.SecurityImpact.STABLE
  elif 'security_impact-beta' in labels:
    return data_types.SecurityImpact.BETA
  elif 'security_impact-head' in labels:
    return data_types.SecurityImpact.HEAD
  elif 'security_impact-none' in labels:
    return data_types.SecurityImpact.NONE
  return data_types.SecurityImpact.MISSING


def update_issue_impact_labels(testcase, issue):
  """Update impact labels on issue."""
  if testcase.one_time_crasher_flag:
    return

  existing_impact = _get_impact_from_labels(issue.labels)

  if testcase.regression.startswith('0:'):
    # If the regression range starts from the start of time,
    # then we assume that the bug impacts stable.
    new_impact = data_types.SecurityImpact.STABLE
  elif testcase.is_impact_set_flag:
    # Add impact label based on testcase's impact value.
    if testcase.impact_stable_version:
      new_impact = data_types.SecurityImpact.STABLE
    elif testcase.impact_beta_version:
      new_impact = data_types.SecurityImpact.BETA
    elif testcase.is_crash():
      new_impact = data_types.SecurityImpact.HEAD
    else:
      # Testcase is unreproducible and does not impact stable and beta branches.
      # In this case, there is no impact information.
      return
  else:
    # No impact information.
    return

  if existing_impact == new_impact:
    # Correct impact already set.
    return

  if existing_impact != data_types.SecurityImpact.MISSING:
    issue.labels.remove('Security_Impact-' + impact_to_string(existing_impact))

  issue.labels.add('Security_Impact-' + impact_to_string(new_impact))


def apply_substitutions(policy, label, testcase, security_severity=None):
  """Apply label substitutions."""
  if label is None:
    # If the label is not configured, then nothing to subsitute.
    return []

  label_substitutions = (
      ('%PLATFORM%', platform_substitution),
      ('%YYYY-MM-DD%', date_substitution),
      ('%SANITIZER%', sanitizer_substitution),
      ('%SEVERITY%', severity_substitution),
  )

  for marker, handler in label_substitutions:
    if marker in label:
      return [
          policy.substitution_mapping(label)
          for label in handler(label, testcase, security_severity)
      ]

  # No match found. Return unmodified label.
  return [label]


def get_label_pattern(label):
  """Get the label pattern regex."""
  return re.compile('^' + re.sub(r'%.*?%', r'(.*)', label) + '$', re.IGNORECASE)


def get_memory_tool_labels(stacktrace):
  """Distinguish memory tools used and return corresponding labels."""
  # Remove stack frames and paths to source code files. This helps to avoid
  # confusion when function names or source paths contain a memory tool token.
  data = ''
  for line in stacktrace.split('\n'):
    if STACKFRAME_LINE_REGEX.match(line):
      continue
    data += line + '\n'

  labels = [t['label'] for t in MEMORY_TOOLS_LABELS if t['token'] in data]
  return labels


def file_issue(testcase,
               issue_tracker,
               security_severity=None,
               user_email=None,
               additional_ccs=None):
  """File an issue for the given test case."""
  issue = issue_tracker.new_issue()
  issue.title = data_handler.get_issue_summary(testcase)
  issue.body = data_handler.get_issue_description(
      testcase, reporter=user_email, show_reporter=True)

  policy = issue_tracker_policy.get(issue_tracker.project)

  # Add reproducibility flag label.
  if testcase.one_time_crasher_flag:
    issue.labels.add(policy.label('unreproducible'))
  else:
    issue.labels.add(policy.label('reproducible'))

  # Chromium-specific labels.
  if issue_tracker.project == 'chromium' and testcase.security_flag:
    # Add reward labels if this is from an external fuzzer contribution.
    fuzzer = data_types.Fuzzer.query(
        data_types.Fuzzer.name == testcase.fuzzer_name).get()
    if fuzzer and fuzzer.external_contribution:
      issue.labels.add('reward-topanel')
      issue.labels.add('External-Fuzzer-Contribution')

    update_issue_impact_labels(testcase, issue)

  # Add additional labels from the job definition and fuzzer.
  additional_labels = data_handler.get_additional_values_for_variable(
      'AUTOMATIC_LABELS', testcase.job_type, testcase.fuzzer_name)
  for label in additional_labels:
    issue.labels.add(label)

  # Add additional components from the job definition and fuzzer.
  automatic_components = data_handler.get_additional_values_for_variable(
      'AUTOMATIC_COMPONENTS', testcase.job_type, testcase.fuzzer_name)
  for component in automatic_components:
    issue.components.add(component)

  is_crash = not utils.sub_string_exists_in(NON_CRASH_TYPES,
                                            testcase.crash_type)
  properties = policy.get_new_issue_properties(
      is_security=testcase.security_flag, is_crash=is_crash)

  issue.status = properties.status

  # Add additional ccs from the job definition and fuzzer.
  ccs = data_handler.get_additional_values_for_variable(
      'AUTOMATIC_CCS', testcase.job_type, testcase.fuzzer_name)

  # For externally contributed fuzzers, potentially cc the author.
  # Use fully qualified fuzzer name if one is available.
  fully_qualified_fuzzer_name = (
      testcase.overridden_fuzzer_name or testcase.fuzzer_name)
  ccs += external_users.cc_users_for_fuzzer(fully_qualified_fuzzer_name,
                                            testcase.security_flag)
  ccs += external_users.cc_users_for_job(testcase.job_type,
                                         testcase.security_flag)

  # Add the user as a cc if requested, and any default ccs for this job.
  # Check for additional ccs or labels from the job definition.
  if additional_ccs:
    ccs += [cc for cc in additional_ccs if cc not in ccs]

  # For user uploads, we assume the uploader is interested in the issue.
  if testcase.uploader_email and testcase.uploader_email not in ccs:
    ccs.append(testcase.uploader_email)

  ccs.extend(properties.ccs)

  # Get view restriction rules for the job.
  issue_restrictions = data_handler.get_value_from_job_definition(
      testcase.job_type, 'ISSUE_VIEW_RESTRICTIONS', 'security')
  should_restrict_issue = (
      issue_restrictions == 'all' or
      (issue_restrictions == 'security' and testcase.security_flag))

  has_accountable_people = bool(ccs)

  # Check for labels with special logic.
  additional_labels = []
  if should_restrict_issue:
    additional_labels.append(policy.label('restrict_view'))

  if has_accountable_people:
    additional_labels.append(policy.label('reported'))

  if testcase.security_flag:
    additional_labels.append(policy.label('security_severity'))

  additional_labels.append(policy.label('os'))

  # Apply label substitutions.
  for label in itertools.chain(properties.labels, additional_labels):
    for result in apply_substitutions(policy, label, testcase,
                                      security_severity):
      issue.labels.add(result)

  issue.body += properties.issue_body_footer
  if (should_restrict_issue and has_accountable_people and
      policy.deadline_policy_message):
    issue.body += '\n\n' + policy.deadline_policy_message

  for cc in ccs:
    issue.ccs.add(cc)

  # Add additional labels from testcase metadata.
  metadata_labels = utils.parse_delimited(
      testcase.get_metadata('issue_labels', ''),
      delimiter=',',
      strip=True,
      remove_empty=True)
  for label in metadata_labels:
    issue.labels.add(label)

  # TODO(ochang): Add additional components from testcase metadata once ready.

  issue.reporter = user_email
  issue.save()

  # Update the testcase with this newly created issue.
  testcase.bug_information = str(issue.id)
  testcase.put()

  data_handler.update_group_bug(testcase.group_id)
  return issue.id
