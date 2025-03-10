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
"""Handler used for setting up oss-fuzz jobs."""
from __future__ import absolute_import

from builtins import object
from past.builtins import basestring
import base64
import copy
import json
import re
import requests
import six
import yaml

from . import service_accounts

from base import tasks
from base import untrusted
from base import utils
from config import db_config
from config import local_config
from datastore import data_handler
from datastore import data_types
from datastore import ndb
from fuzzing import fuzzer_selection
from google_cloud_utils import pubsub
from google_cloud_utils import storage
from handlers import base_handler
from libs import handler
from metrics import logs
from system import environment

BUILD_BUCKET_PATH_TEMPLATE = (
    'gs://%BUCKET%/%PROJECT%/%PROJECT%-%SANITIZER%-([0-9]+).zip')

BACKUPS_LIFECYCLE = storage.generate_life_cycle_config('Delete', age=100)
LOGS_LIFECYCLE = storage.generate_life_cycle_config('Delete', age=14)
QUARANTINE_LIFECYCLE = storage.generate_life_cycle_config('Delete', age=90)

JOB_TEMPLATE = ('{build_type} = {build_bucket_path}\n'
                'PROJECT_NAME = {project}\n'
                'SUMMARY_PREFIX = {project}\n'
                'MANAGED = True\n')

OBJECT_VIEWER_IAM_ROLE = 'roles/storage.objectViewer'
OBJECT_ADMIN_IAM_ROLE = 'roles/storage.objectAdmin'

VALID_PROJECT_NAME_REGEX = re.compile(r'^[a-zA-Z0-9_-]+$')

REVISION_URL = ('https://commondatastorage.googleapis.com/'
                '{bucket}/{project}/{project}-{sanitizer}-%s.srcmap.json')

REQUEST_TIMEOUT = 60

ALLOWED_VIEW_RESTRICTIONS = ['none', 'security', 'all']

PUBSUB_PLATFORMS = ['linux']


class ProjectSetupError(Exception):
  """Exception."""


class JobInfo(object):
  """Job information."""

  def __init__(self,
               prefix,
               engine,
               memory_tool,
               cf_job_templates,
               architecture='x86_64',
               experimental=False,
               minimize_job_override=None):
    self.prefix = prefix
    self.engine = engine
    self.memory_tool = memory_tool
    self.architecture = architecture
    self.cf_job_templates = cf_job_templates
    self.experimental = experimental
    self.minimize_job_override = minimize_job_override

  def job_name(self, project_name):
    return self.prefix + data_types.normalized_name(project_name)


# The order of templates is important here. Later templates override settings in
# the earlier ones. An engine template may override vars set for a sanitizer.
LIBFUZZER_ASAN_JOB = JobInfo('libfuzzer_asan_', 'libfuzzer', 'address',
                             ['engine_asan', 'libfuzzer', 'prune'])
LIBFUZZER_MSAN_JOB = JobInfo('libfuzzer_msan_', 'libfuzzer', 'memory',
                             ['engine_msan', 'libfuzzer'])
LIBFUZZER_UBSAN_JOB = JobInfo('libfuzzer_ubsan_', 'libfuzzer', 'undefined',
                              ['engine_ubsan', 'libfuzzer'])
AFL_ASAN_JOB = JobInfo(
    'afl_asan_',
    'afl',
    'address', ['engine_asan', 'afl'],
    minimize_job_override=LIBFUZZER_ASAN_JOB)
NO_ENGINE_ASAN_JOB = JobInfo('asan_', 'none', 'address', [])

LIBFUZZER_ASAN_I386_JOB = JobInfo(
    'libfuzzer_asan_i386_',
    'libfuzzer',
    'address', ['engine_asan', 'libfuzzer'],
    architecture='i386')

JOB_MAP = {
    'libfuzzer': {
        'x86_64': {
            'address': LIBFUZZER_ASAN_JOB,
            'memory': LIBFUZZER_MSAN_JOB,
            'undefined': LIBFUZZER_UBSAN_JOB,
        },
        'i386': {
            'address': LIBFUZZER_ASAN_I386_JOB,
        },
    },
    'afl': {
        'x86_64': {
            'address': AFL_ASAN_JOB,
        }
    },
    'none': {
        'x86_64': {
            'address': NO_ENGINE_ASAN_JOB,
        }
    }
}

DEFAULT_ARCHITECTURES = ['x86_64']
DEFAULT_SANITIZERS = ['address', 'undefined']
DEFAULT_ENGINES = ['libfuzzer', 'afl']


def _to_experimental_job(job_info):
  job_info = copy.copy(job_info)
  job_info.experimental = True
  return job_info


def get_github_url(url):
  """Return contents of URL."""
  github_credentials = db_config.get_value('github_credentials')
  if not github_credentials:
    raise ProjectSetupError('No github credentials.')

  client_id, client_secret = github_credentials.strip().split(';')

  response = requests.get(
      url, params={
          'client_id': client_id,
          'client_secret': client_secret
      })
  if response.status_code != 200:
    logs.log_error(
        'Failed to get github url: %s' % url, status_code=response.status_code)
    response.raise_for_status()

  return json.loads(response.text)


def find_github_item_url(github_json, name):
  """Get url of a blob/tree from a github json response."""
  for item in github_json['tree']:
    if item['path'] == name:
      return item['url']

  return None


def get_oss_fuzz_projects():
  """Return list of projects for oss-fuzz."""
  ossfuzz_tree_url = ('https://api.github.com/repos/google/oss-fuzz/'
                      'git/trees/master')
  tree = get_github_url(ossfuzz_tree_url)
  projects = []

  projects_url = find_github_item_url(tree, 'projects')
  if not projects_url:
    logs.log_error('No projects found.')
    return []

  tree = get_github_url(projects_url)
  for item in tree['tree']:
    if item['type'] != 'tree':
      continue

    item_json = get_github_url(item['url'])
    project_yaml_url = find_github_item_url(item_json, 'project.yaml')
    if not project_yaml_url:
      continue

    projects_yaml = get_github_url(project_yaml_url)
    info = yaml.safe_load(base64.b64decode(projects_yaml['content']))

    has_dockerfile = (
        find_github_item_url(item_json, 'Dockerfile') or 'dockerfile' in info)
    if not has_dockerfile:
      continue

    projects.append((item['path'], info))

  return projects


def get_projects_from_gcs(gcs_url):
  """Get projects from GCS path."""
  data = json.loads(storage.read_data(gcs_url))
  return [(project['name'], project) for project in data['projects']]


def _process_sanitizers_field(sanitizers):
  """Pre-process sanitizers field into a map from sanitizer name -> dict of
  options."""
  processed_sanitizers = {}
  if not isinstance(sanitizers, list):
    return None

  # each field can either be a Map or a String:
  # sanitizers:
  #   - undefined:
  #       experimental: true
  #   - address
  #   - memory
  for sanitizer in sanitizers:
    if isinstance(sanitizer, basestring):
      processed_sanitizers[sanitizer] = {}
    elif isinstance(sanitizer, dict):
      for key, value in six.iteritems(sanitizer):
        processed_sanitizers[key] = value
    else:
      return None

  return processed_sanitizers


def get_jobs_for_project(project, info):
  """Return jobs for the project."""
  sanitizers = _process_sanitizers_field(
      info.get('sanitizers', DEFAULT_SANITIZERS))
  if not sanitizers:
    logs.log_error('Invalid sanitizers field for %s.' % project)
    return []

  engines = info.get('fuzzing_engines', DEFAULT_ENGINES)
  architectures = info.get('architectures', DEFAULT_ARCHITECTURES)

  jobs = []
  for engine in engines:
    if engine not in JOB_MAP:
      continue

    for architecture in architectures:
      if architecture not in JOB_MAP[engine]:
        continue

      for sanitizer, options in six.iteritems(sanitizers):
        experimental = (
            options.get('experimental', False) or
            info.get('experimental', False))
        if sanitizer in JOB_MAP[engine][architecture]:
          job = JOB_MAP[engine][architecture][sanitizer]
          if experimental:
            job = _to_experimental_job(job)

          jobs.append(job)

  return jobs


def convert_googlemail_to_gmail(email):
  """Convert @googlemail.com to @gmail.com."""
  # TODO(ochang): Investiate if we can/need to do this in general, and not just
  # for cloud storage bucket IAMs.
  if email.endswith('@googlemail.com'):
    return email.split('@')[0] + '@gmail.com'

  return email


def _add_users_to_bucket(info, client, bucket_name, iam_policy):
  """Add user account to bucket."""
  ccs = sorted(
      ['user:' + convert_googlemail_to_gmail(cc) for cc in ccs_from_info(info)])
  binding = storage.get_bucket_iam_binding(iam_policy, OBJECT_VIEWER_IAM_ROLE)

  if binding:
    # buckets.getIamPolicy can return duplicate members when we add a @gmail.com
    # as well as @googlemail.com address for the same account.
    binding['members'] = sorted(list(set(binding['members'])))
    if binding['members'] == ccs:
      return iam_policy

    filtered_members = [
        member for member in binding['members'] if member in ccs
    ]

    if len(filtered_members) != len(binding['members']):
      # Remove old members.
      binding['members'] = filtered_members
      iam_policy = storage.set_bucket_iam_policy(client, bucket_name,
                                                 iam_policy)

  # We might have no binding either from start or after filtering members above.
  # Create a new one in those cases.
  binding = storage.get_or_create_bucket_iam_binding(iam_policy,
                                                     OBJECT_VIEWER_IAM_ROLE)

  for cc in ccs:
    if cc in binding['members']:
      continue

    logs.log('Adding %s to bucket IAM for %s' % (cc, bucket_name))
    # Add CCs one at a time since the API does not work with invalid or
    # non-Google emails.
    modified_iam_policy = storage.add_single_bucket_iam(
        client, iam_policy, OBJECT_VIEWER_IAM_ROLE, bucket_name, cc)
    if modified_iam_policy:
      iam_policy = modified_iam_policy
      binding = storage.get_bucket_iam_binding(iam_policy,
                                               OBJECT_VIEWER_IAM_ROLE)

  if not binding['members']:
    # Check that the final binding has members. Empty bindings are not valid.
    storage.remove_bucket_iam_binding(iam_policy, OBJECT_VIEWER_IAM_ROLE)

  return iam_policy


def _set_bucket_service_account(service_account, client, bucket_name,
                                iam_policy):
  """Set service account for a bucket."""
  # Add service account as objectAdmin.
  binding = storage.get_or_create_bucket_iam_binding(iam_policy,
                                                     OBJECT_ADMIN_IAM_ROLE)

  members = ['serviceAccount:' + service_account['email']]
  if members == binding['members']:
    # No changes required.
    return iam_policy

  binding['members'] = members
  return storage.set_bucket_iam_policy(client, bucket_name, iam_policy)


def add_bucket_iams(info, client, bucket_name, service_account):
  """Add CC'ed users to storage bucket IAM."""
  iam_policy = storage.get_bucket_iam_policy(client, bucket_name)
  if not iam_policy:
    return

  iam_policy = _add_users_to_bucket(info, client, bucket_name, iam_policy)
  _set_bucket_service_account(service_account, client, bucket_name, iam_policy)


def add_service_account_to_bucket(client, bucket_name, service_account, role):
  """Add service account to the gcr.io images bucket."""
  iam_policy = storage.get_bucket_iam_policy(client, bucket_name)
  if not iam_policy:
    return

  binding = storage.get_or_create_bucket_iam_binding(iam_policy, role)

  member = 'serviceAccount:' + service_account['email']
  if member in binding['members']:
    # No changes required.
    return

  binding['members'].append(member)
  storage.set_bucket_iam_policy(client, bucket_name, iam_policy)


def sync_user_permissions(project, info):
  """Sync permissions of project based on project.yaml."""
  ccs = ccs_from_info(info)

  for template in get_jobs_for_project(project, info):
    job_name = template.job_name(project)

    # Delete removed CCs.
    existing_ccs = data_types.ExternalUserPermission.query(
        data_types.ExternalUserPermission.entity_kind ==
        data_types.PermissionEntityKind.JOB,
        data_types.ExternalUserPermission.entity_name == job_name)
    ndb.delete_multi([
        permission.key
        for permission in existing_ccs
        if permission.email not in ccs
    ])

    for cc in ccs:
      query = data_types.ExternalUserPermission.query(
          data_types.ExternalUserPermission.email == cc,
          data_types.ExternalUserPermission.entity_kind ==
          data_types.PermissionEntityKind.JOB,
          data_types.ExternalUserPermission.entity_name == job_name)

      existing_permission = query.get()
      if existing_permission:
        continue

      data_types.ExternalUserPermission(
          email=cc,
          entity_kind=data_types.PermissionEntityKind.JOB,
          entity_name=job_name,
          is_prefix=False,
          auto_cc=data_types.AutoCCType.ALL).put()


def ccs_from_info(info):
  """Get list of CC's from project info."""
  ccs = []
  if 'primary_contact' in info:
    primary_contact = info['primary_contact']
    if isinstance(primary_contact, basestring):
      ccs.append(primary_contact)
    else:
      raise ProjectSetupError('Bad primary_contact %s.' % primary_contact)

  if 'auto_ccs' in info:
    auto_ccs = info.get('auto_ccs')
    if isinstance(auto_ccs, list):
      ccs.extend(auto_ccs)
    elif isinstance(auto_ccs, basestring):
      ccs.append(auto_ccs)
    else:
      raise ProjectSetupError('Bad auto_ccs %s.' % auto_ccs)

  return [utils.normalize_email(cc) for cc in ccs]


def update_fuzzer_jobs(fuzzer_entities, project_names):
  """Update fuzzer job mappings."""
  to_delete = []

  for job in data_types.Job.query():
    if not job.environment_string:
      continue

    job_environment = job.get_environment()
    if not job_environment.get('MANAGED', False):
      continue

    job_project = job_environment['PROJECT_NAME']
    if job_project in project_names:
      continue

    to_delete.append(job.key)
    for fuzzer_entity in fuzzer_entities:
      try:
        fuzzer_entity.jobs.remove(job.name)
      except ValueError:
        pass

  for fuzzer_entity in fuzzer_entities:
    fuzzer_entity.put()
    fuzzer_selection.update_mappings_for_fuzzer(fuzzer_entity)

  if to_delete:
    ndb.delete_multi(to_delete)


def cleanup_old_projects_settings(project_names):
  """Delete old projects that are no longer used or disabled."""
  to_delete = []

  for project in data_types.OssFuzzProject.query():
    if project.name not in project_names:
      to_delete.append(project.key)

  if to_delete:
    ndb.delete_multi(to_delete)


def create_project_settings(project, info, service_account):
  """Setup settings for ClusterFuzz (such as CPU distribution)."""
  key = ndb.Key(data_types.OssFuzzProject, project)
  oss_fuzz_project = key.get()

  # Expecting to run a blackbox fuzzer, so use high end hosts.
  is_high_end = info.get('fuzzing_engines') == ['none']

  ccs = ccs_from_info(info)

  if oss_fuzz_project:
    if oss_fuzz_project.service_account != service_account['email']:
      oss_fuzz_project.service_account = service_account['email']
      oss_fuzz_project.put()

    if oss_fuzz_project.high_end != is_high_end:
      oss_fuzz_project.high_end = is_high_end
      oss_fuzz_project.put()

    if oss_fuzz_project.ccs != ccs:
      oss_fuzz_project.ccs = ccs
      oss_fuzz_project.put()
  else:
    data_types.OssFuzzProject(
        id=project,
        name=project,
        high_end=is_high_end,
        service_account=service_account['email'],
        ccs=ccs).put()


def create_pubsub_topics(project):
  """Create pubsub topics for tasks."""
  for platform in PUBSUB_PLATFORMS:
    name = untrusted.queue_name(project, platform)
    client = pubsub.PubSubClient()
    application_id = utils.get_application_id()

    topic_name = pubsub.topic_name(application_id, name)
    if client.get_topic(topic_name) is None:
      client.create_topic(topic_name)

    subscription_name = pubsub.subscription_name(application_id, name)
    if client.get_subscription(subscription_name) is None:
      client.create_subscription(subscription_name, topic_name)


def cleanup_pubsub_topics(project_names):
  """Delete old pubsub topics and subscriptions."""
  client = pubsub.PubSubClient()
  application_id = utils.get_application_id()

  expected_topics = set()
  for platform in PUBSUB_PLATFORMS:
    expected_topics.update(
        [untrusted.queue_name(project, platform) for project in project_names])

  pubsub_config = local_config.Config('pubsub.queues')
  unmanaged_queues = [queue['name'] for queue in pubsub_config.get('resources')]

  for topic in client.list_topics(pubsub.project_name(application_id)):
    _, name = pubsub.parse_name(topic)

    if (not name.startswith(tasks.JOBS_PREFIX) and
        not name.startswith(tasks.HIGH_END_JOBS_PREFIX)):
      # Some topic created by another service, ignore.
      continue

    if name in unmanaged_queues:
      continue

    if name in expected_topics:
      continue

    for subscription in client.list_topic_subscriptions(topic):
      client.delete_subscription(subscription)

    client.delete_topic(topic)


class ProjectSetup(object):
  """Project setup."""

  def __init__(self,
               build_bucket_path_template,
               revision_url_template,
               build_type,
               segregate_projects=False,
               engine_build_buckets=None,
               fuzzer_entities=None,
               add_info_labels=False,
               add_revision_mappings=False,
               additional_vars=None):
    self._build_type = build_type
    self._build_bucket_path_template = build_bucket_path_template
    self._revision_url_template = revision_url_template
    self._segregate_projects = segregate_projects
    self._engine_build_buckets = engine_build_buckets
    self._fuzzer_entities = fuzzer_entities
    self._add_info_labels = add_info_labels
    self._add_revision_mappings = add_revision_mappings
    self._additional_vars = additional_vars

  def _get_build_bucket(self, engine, architecture):
    """Return the bucket for the given |engine| and |architecture|."""
    if architecture != 'x86_64':
      engine += '-' + architecture

    bucket = self._engine_build_buckets.get(engine)
    if not bucket:
      raise ProjectSetupError('Invalid fuzzing engine ' + engine)

    return bucket

  def _deployment_bucket_name(self):
    """Deployment bucket name."""
    return '{project}-deployment'.format(project=utils.get_application_id())

  def _shared_corpus_bucket_name(self):
    """Shared corpus bucket name."""
    return environment.get_value('SHARED_CORPUS_BUCKET')

  def _mutator_plugins_bucket_name(self):
    """Mutator plugins bucket name."""
    return environment.get_value('MUTATOR_PLUGINS_BUCKET')

  def _backup_bucket_name(self, project_name):
    """Return the backup_bucket_name."""
    return project_name + '-backup.' + data_handler.bucket_domain_suffix()

  def _corpus_bucket_name(self, project_name):
    """Return the corpus_bucket_name."""
    return project_name + '-corpus.' + data_handler.bucket_domain_suffix()

  def _quarantine_bucket_name(self, project_name):
    """Return the quarantine_bucket_name."""
    return project_name + '-quarantine.' + data_handler.bucket_domain_suffix()

  def _logs_bucket_name(self, project_name):
    """Return the logs bucket name."""
    return project_name + '-logs.' + data_handler.bucket_domain_suffix()

  def _create_service_accounts_and_buckets(self, project, info):
    """Create per-project service account and buckets."""
    service_account = service_accounts.get_or_create_service_account(project)
    service_accounts.set_service_account_roles(service_account)

    # Create GCS buckets.
    backup_bucket_name = self._backup_bucket_name(project)
    corpus_bucket_name = self._corpus_bucket_name(project)
    logs_bucket_name = self._logs_bucket_name(project)
    quarantine_bucket_name = self._quarantine_bucket_name(project)

    storage.create_bucket_if_needed(backup_bucket_name, BACKUPS_LIFECYCLE)
    storage.create_bucket_if_needed(corpus_bucket_name)
    storage.create_bucket_if_needed(quarantine_bucket_name,
                                    QUARANTINE_LIFECYCLE)
    storage.create_bucket_if_needed(logs_bucket_name, LOGS_LIFECYCLE)

    client = storage.create_discovery_storage_client()
    try:
      add_bucket_iams(info, client, backup_bucket_name, service_account)
      add_bucket_iams(info, client, corpus_bucket_name, service_account)
      add_bucket_iams(info, client, logs_bucket_name, service_account)
      add_bucket_iams(info, client, quarantine_bucket_name, service_account)
    except Exception as e:
      logs.log_error('Failed to add bucket IAMs for %s: %s' % (project, e))

    # Grant the service account read access to deployment, shared corpus and
    # mutator plugin buckets.
    add_service_account_to_bucket(client, self._deployment_bucket_name(),
                                  service_account, OBJECT_VIEWER_IAM_ROLE)
    add_service_account_to_bucket(client, self._shared_corpus_bucket_name(),
                                  service_account, OBJECT_VIEWER_IAM_ROLE)
    add_service_account_to_bucket(client, self._mutator_plugins_bucket_name(),
                                  service_account, OBJECT_VIEWER_IAM_ROLE)

    data_bundles = set([
        fuzzer_entity.data_bundle_name
        for fuzzer_entity in self._fuzzer_entities.values()
    ])
    for data_bundle in data_bundles:
      # Workers also need to be able to set up these global bundles.
      data_bundle_bucket_name = data_handler.get_data_bundle_bucket_name(
          data_bundle)
      add_service_account_to_bucket(client, data_bundle_bucket_name,
                                    service_account, OBJECT_VIEWER_IAM_ROLE)

    return (service_account, backup_bucket_name, corpus_bucket_name,
            logs_bucket_name, quarantine_bucket_name)

  def _get_build_bucket_path(self, project_name, info, engine, memory_tool,
                             architecture):
    """Returns the build bucket path for the |project|, |engine|, |memory_tool|,
    and |architecture|."""
    build_path = info.get('build_path')
    if not build_path:
      build_path = self._build_bucket_path_template

    build_path = build_path.replace(
        '%BUCKET%', self._get_build_bucket(engine, architecture))
    build_path = build_path.replace('%PROJECT%', project_name)
    build_path = build_path.replace('%ENGINE%', engine)
    build_path = build_path.replace('%SANITIZER%', memory_tool)
    return build_path

  def _sync_job(self, project, info, corpus_bucket_name, quarantine_bucket_name,
                logs_bucket_name, backup_bucket_name):
    """Sync the config with ClusterFuzz."""
    # Create/update ClusterFuzz jobs.
    for template in get_jobs_for_project(project, info):
      if template.engine == 'none':
        # Engine-less jobs are not automatically managed.
        continue

      fuzzer_entity = self._fuzzer_entities.get(template.engine)
      if not fuzzer_entity:
        raise ProjectSetupError('Invalid fuzzing engine ' + template.engine)

      job_name = template.job_name(project)
      job = data_types.Job.query(data_types.Job.name == job_name).get()
      if not job:
        job = data_types.Job()

      if job_name not in fuzzer_entity.jobs and not info.get('disabled', False):
        # Enable new job.
        fuzzer_entity.jobs.append(job_name)

      job.name = job_name
      if self._segregate_projects:
        job.platform = untrusted.platform_name(project, 'linux')
      else:
        # TODO(ochang): Support other platforms?
        job.platform = 'LINUX'

      job.templates = template.cf_job_templates

      job.environment_string = JOB_TEMPLATE.format(
          build_type=self._build_type,
          build_bucket_path=self._get_build_bucket_path(
              project, info, template.engine, template.memory_tool,
              template.architecture),
          engine=template.engine,
          project=project)

      if self._add_revision_mappings:
        revision_vars_url = self._revision_url_template.format(
            project=project,
            bucket=self._get_build_bucket(template.engine,
                                          template.architecture),
            sanitizer=template.memory_tool)

        job.environment_string += (
            'REVISION_VARS_URL = {revision_vars_url}\n'.format(
                revision_vars_url=revision_vars_url))

      if logs_bucket_name:
        job.environment_string += 'FUZZ_LOGS_BUCKET = {logs_bucket}\n'.format(
            logs_bucket=logs_bucket_name)

      if corpus_bucket_name:
        job.environment_string += 'CORPUS_BUCKET = {corpus_bucket}\n'.format(
            corpus_bucket=corpus_bucket_name)

      if quarantine_bucket_name:
        job.environment_string += (
            'QUARANTINE_BUCKET = {quarantine_bucket}\n'.format(
                quarantine_bucket=quarantine_bucket_name))

      if backup_bucket_name:
        job.environment_string += 'BACKUP_BUCKET = {backup_bucket}\n'.format(
            backup_bucket=backup_bucket_name)

      if self._add_info_labels:
        job.environment_string += (
            'AUTOMATIC_LABELS = Proj-{project},Engine-{engine}\n'.format(
                project=project,
                engine=template.engine,
            ))

      help_url = info.get('help_url')
      if help_url:
        job.environment_string += 'HELP_URL = %s\n' % help_url

      if template.experimental:
        job.environment_string += 'EXPERIMENTAL = True\n'

      if template.minimize_job_override:
        minimize_job_override = template.minimize_job_override.job_name(project)
        job.environment_string += (
            'MINIMIZE_JOB_OVERRIDE = %s\n' % minimize_job_override)

      view_restrictions = info.get('view_restrictions')
      if view_restrictions:
        if view_restrictions in ALLOWED_VIEW_RESTRICTIONS:
          job.environment_string += (
              'ISSUE_VIEW_RESTRICTIONS = %s\n' % view_restrictions)
        else:
          logs.log_error('Invalid view restriction setting %s for project %s.' %
                         (view_restrictions, project))

      selective_unpack = info.get('selective_unpack')
      if selective_unpack:
        job.environment_string += 'UNPACK_ALL_FUZZ_TARGETS_AND_FILES = False\n'

      if (template.engine == 'libfuzzer' and
          template.architecture == 'x86_64' and
          'dataflow' in info.get('fuzzing_engines', DEFAULT_ENGINES)):
        # Dataflow binaries are built with dataflow sanitizer, but can be used
        # as an auxiliary build with libFuzzer builds (e.g. with ASan or UBSan).
        dataflow_build_bucket_path = self._get_build_bucket_path(
            project_name=project,
            info=info,
            engine='dataflow',
            memory_tool='dataflow',
            architecture=template.architecture)
        job.environment_string += (
            'DATAFLOW_BUILD_BUCKET_PATH = %s\n' % dataflow_build_bucket_path)

      if self._additional_vars:
        additional_vars = {}
        additional_vars.update(self._additional_vars.get('all', {}))

        engine_vars = self._additional_vars.get(template.engine, {})
        engine_sanitizer_vars = engine_vars.get(template.memory_tool, {})
        additional_vars.update(engine_sanitizer_vars)

        for key, value in six.iteritems(additional_vars):
          job.environment_string += ('{} = {}\n'.format(key, value))

      job.put()

  def set_up(self, projects):
    """Do project setup."""
    for project, info in projects:
      logs.log('Syncing configs for %s.' % project)

      backup_bucket_name = None
      corpus_bucket_name = None
      logs_bucket_name = None
      quarantine_bucket_name = None

      if self._segregate_projects:
        # Create per project service account and GCS buckets.
        (service_account, backup_bucket_name, corpus_bucket_name,
         logs_bucket_name, quarantine_bucket_name) = (
             self._create_service_accounts_and_buckets(project, info))

      # Create CF jobs for project.
      self._sync_job(project, info, corpus_bucket_name, quarantine_bucket_name,
                     logs_bucket_name, backup_bucket_name)

      if self._segregate_projects:
        sync_user_permissions(project, info)

        # Create Pub/Sub topics for tasks.
        create_pubsub_topics(project)

        # Set up projects settings (such as CPU distribution settings).
        if not info.get('disabled', False):
          create_project_settings(project, info, service_account)

    # Delete old jobs.
    project_names = [project[0] for project in projects]
    update_fuzzer_jobs(self._fuzzer_entities.values(), project_names)

    if self._segregate_projects:
      # Delete old pubsub topics.
      cleanup_pubsub_topics(project_names)

    # Delete old/disabled project settings.
    enabled_projects = [
        project for project, info in projects
        if not info.get('disabled', False)
    ]
    cleanup_old_projects_settings(enabled_projects)


class Handler(base_handler.Handler):
  """Setup ClusterFuzz jobs for projects."""

  @handler.check_cron()
  def get(self):
    """Handles a GET request."""
    libfuzzer = data_types.Fuzzer.query(
        data_types.Fuzzer.name == 'libFuzzer').get()
    if not libfuzzer:
      logs.log_error('Failed to get libFuzzer Fuzzer entity.')
      return

    afl = data_types.Fuzzer.query(data_types.Fuzzer.name == 'afl').get()
    if not afl:
      logs.log_error('Failed to get AFL Fuzzer entity.')
      return

    project_setup_config = local_config.ProjectConfig().sub_config(
        'project_setup')
    bucket_config = project_setup_config.sub_config('build_buckets')

    if not bucket_config:
      raise ProjectSetupError('Project setup buckets not specified.')

    config = ProjectSetup(
        BUILD_BUCKET_PATH_TEMPLATE,
        REVISION_URL,
        project_setup_config.get('build_type'),
        segregate_projects=project_setup_config.get(
            'segregate_projects', default=False),
        engine_build_buckets={
            'libfuzzer': bucket_config.get('libfuzzer'),
            'libfuzzer-i386': bucket_config.get('libfuzzer_i386'),
            'afl': bucket_config.get('afl'),
            'none': bucket_config.get('no_engine'),
            'dataflow': bucket_config.get('dataflow'),
        },
        fuzzer_entities={
            'libfuzzer': libfuzzer,
            'afl': afl,
        },
        add_info_labels=project_setup_config.get(
            'add_info_labels', default=False),
        add_revision_mappings=project_setup_config.get(
            'add_revision_mappings', default=False),
        additional_vars=project_setup_config.get('additional_vars'))

    projects_source = project_setup_config.get('source')
    if projects_source == 'oss-fuzz':
      projects = get_oss_fuzz_projects()
    elif projects_source.startswith(storage.GS_PREFIX):
      projects = get_projects_from_gcs(projects_source)
    else:
      raise ProjectSetupError('Invalid projects source: ' + projects_source)

    config.set_up(projects)
