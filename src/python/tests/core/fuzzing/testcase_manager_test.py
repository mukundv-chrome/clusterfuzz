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
"""Tests testcase_manager."""
import datetime
import mock
import os
import unittest

from pyfakefs import fake_filesystem_unittest

from bot import testcase_manager
from bot.fuzzers import engine
from crash_analysis.crash_result import CrashResult
from crash_analysis.stack_parsing import stack_analyzer
from datastore import data_types
from system import environment
from tests.test_libs import helpers as test_helpers
from tests.test_libs import test_utils


class CreateTestcaseListFileTest(fake_filesystem_unittest.TestCase):
  """Tests for create_testcase_list_file."""

  def setUp(self):
    test_helpers.patch_environ(self)
    test_utils.set_up_pyfakefs(self)
    environment.set_value('FAIL_RETRIES', 1)

  def test_create(self):
    """Tests creation of testcase list file."""
    self.fs.create_file('/test/aa/bb.txt', contents='')
    self.fs.create_file('/test/aa/cc.txt', contents='')
    self.fs.create_file('/test/aa/aa/dd.txt', contents='')
    self.fs.create_file('/test/aa/aa/files.info', contents='')
    self.fs.create_file('/test/aa/bb/files.chrome.info', contents='')

    testcase_manager.create_testcase_list_file('/test/aa')

    testcase_list_file_path = '/test/aa/files.info'
    self.assertTrue(os.path.exists(testcase_list_file_path))

    expected_files_list = set([
        'bb.txt',
        'cc.txt',
        'aa/dd.txt',
    ])
    actual_files_list = set(open(testcase_list_file_path).read().splitlines())
    self.assertEqual(expected_files_list, actual_files_list)


# pylint: disable=protected-access
class UploadTestcaseOutputTest(fake_filesystem_unittest.TestCase):
  """Tests for logs uploading."""

  def setUp(self):
    test_helpers.patch_environ(self)
    test_utils.set_up_pyfakefs(self)
    self.testcase_path = '/test/fuzzer/testcase'
    self.fs.create_file(self.testcase_path, contents='')

    environment.set_value('FUZZ_LOGS_BUCKET', 'fake-gcs-logs')
    environment.set_value('FUZZER_NAME', 'fuzzer')
    environment.set_value('JOB_NAME', 'job')
    environment.set_value('APP_REVISION', '123')

    # To be used for generation of date and time when uploading a log.
    fake_utcnow = datetime.datetime(2017, 5, 15, 16, 10, 28, 374119)

    # Original utcfromtimestamp needs to be preserved, as test_helpres does not
    # allow to patch only datetime.datetime.utcnow.
    orig_utcfromtimestamp = datetime.datetime.utcfromtimestamp

    test_helpers.patch(self, [
        'build_management.revisions.get_component_range_list',
        'google_cloud_utils.storage.write_data',
        'datetime.datetime',
    ])

    self.mock.datetime.utcnow.return_value = fake_utcnow
    self.mock.datetime.utcfromtimestamp.side_effect = orig_utcfromtimestamp
    self.mock.get_component_range_list.return_value = [{
        'component': 'Component',
        'link_text': 'REVISION',
    }, {
        'component': 'Component2',
        'link_text': 'REVISION2',
    }]

  def test_upload_with_timestamp_from_stats(self):
    """Log name should be generated using timestamp value from the stats."""
    mock_gsutil = mock.MagicMock()
    self.mock.write_data.return_value = mock_gsutil

    self.fs.create_file(
        self.testcase_path + '.stats2',
        contents='{"stat": 1000, "timestamp": 1472846341.017923, "kind": '
        '"TestcaseRun", "job": "job", "fuzzer": "fuzzer", '
        '"build_revision": 123}\n')

    crash_result = CrashResult(
        return_code=1, crash_time=5, output='fake output')

    log = testcase_manager.prepare_log_for_upload(crash_result.get_stacktrace(),
                                                  crash_result.return_code)
    log_time = testcase_manager._get_testcase_time(self.testcase_path)
    testcase_manager.upload_log(log, log_time)

    # Date and time below is derived from 1472846341 timestamp value.
    self.mock.write_data.assert_called_once_with(
        'Component revisions (build r123):\n'
        'Component: REVISION\nComponent2: REVISION2\n\n'
        'Return code: 1\n\nfake output',
        'gs://fake-gcs-logs/fuzzer/job/2016-09-02/19:59:01:017923.log')

  def test_upload_without_timestamp(self):
    """Log name should be generated using current (mocked) timestamp value."""
    mock_gsutil = mock.MagicMock()
    self.mock.write_data.return_value = mock_gsutil

    self.fs.create_file(
        self.testcase_path + '.stats2',
        contents='{"stat": 1000, "kind": "TestcaseRun", "job": "job", '
        '"fuzzer": "fuzzer", "build_revision": 123}\n')

    crash_result = CrashResult(return_code=None, crash_time=None, output=None)
    log = testcase_manager.prepare_log_for_upload(crash_result.get_stacktrace(),
                                                  crash_result.return_code)
    log_time = testcase_manager._get_testcase_time(self.testcase_path)
    testcase_manager.upload_log(log, log_time)
    self.mock.write_data.assert_called_once_with(
        'Component revisions (build r123):\n'
        'Component: REVISION\nComponent2: REVISION2\n\n'
        'Return code: None\n\nNo output!',
        'gs://fake-gcs-logs/fuzzer/job/2017-05-15/16:10:28:374119.log')

  def test_upload_without_component_revisions(self):
    """Log should contain message on empty component revisions."""
    self.mock.get_component_range_list.return_value = []

    mock_gsutil = mock.MagicMock()
    self.mock.write_data.return_value = mock_gsutil

    self.fs.create_file(
        self.testcase_path + '.stats2',
        contents='{"stat": 1000, "timestamp": 1472846341.017923, "kind": '
        '"TestcaseRun", "job": "job", "fuzzer": "fuzzer", '
        '"build_revision": 123}\n')

    crash_result = CrashResult(
        return_code=1, crash_time=5, output='fake output')
    log = testcase_manager.prepare_log_for_upload(crash_result.get_stacktrace(),
                                                  crash_result.return_code)
    log_time = testcase_manager._get_testcase_time(self.testcase_path)
    testcase_manager.upload_log(log, log_time)

    # Date and time below is derived from 1472846341 timestamp value.
    self.mock.write_data.assert_called_once_with(
        'Component revisions (build r123):\n'
        'Not available.\n\n'
        'Return code: 1\n\nfake output',
        'gs://fake-gcs-logs/fuzzer/job/2016-09-02/19:59:01:017923.log')


class ConvertDependencyUrlToLocalPathTest(unittest.TestCase):
  """Tests convert_dependency_url_to_local_path."""

  def setUp(self):
    test_helpers.patch_environ(self)
    environment.set_value('FUZZ_INPUTS', '/mnt/scratch0')

    test_helpers.patch(self, [
        'bot.webserver.http_server.get_absolute_testcase_file',
        'system.environment.platform',
        'base.utils.normalize_path',
    ])
    self.mock.normalize_path.side_effect = lambda x: x

  def test_file_match_android(self):
    """Tests matching a file URL."""
    self.mock.platform.return_value = 'ANDROID'
    self.assertEqual(
        '/mnt/scratch0/test.html',
        testcase_manager.convert_dependency_url_to_local_path(
            'file:///sdcard/fuzzer-testcases/test.html'))
    self.mock.normalize_path.assert_called_once_with('/mnt/scratch0/test.html')

  def test_file_match_linux(self):
    """Tests matching a file URL."""
    self.mock.platform.return_value = 'LINUX'
    self.assertEqual(
        '/mnt/scratch0/test.html',
        testcase_manager.convert_dependency_url_to_local_path(
            'file:///mnt/scratch0/test.html'))
    self.mock.normalize_path.assert_called_once_with('/mnt/scratch0/test.html')

  def test_file_match_windows(self):
    """Tests matching a file URL."""
    self.mock.platform.return_value = 'WINDOWS'
    self.assertEqual(
        'C:/test/test.html',
        testcase_manager.convert_dependency_url_to_local_path(
            'file:///C:/test/test.html'))
    self.mock.normalize_path.assert_called_once_with('C:/test/test.html')

  def test_url_match_any_ip_and_port(self):
    """Tests matching a URL (any ip and port)."""
    self.mock.get_absolute_testcase_file.return_value = '/path'
    self.assertEqual(
        '/path',
        testcase_manager.convert_dependency_url_to_local_path(
            'http://10.240.1.237:8002/test/test.html'))
    self.mock.get_absolute_testcase_file.assert_called_once_with(
        '/test/test.html')
    self.mock.normalize_path.assert_called_once_with('/path')

  def test_url_match_localhost(self):
    """Tests matching a URL (localhost)."""
    self.mock.get_absolute_testcase_file.return_value = '/path'
    self.assertEqual(
        '/path',
        testcase_manager.convert_dependency_url_to_local_path(
            'http://localhost/test/test.html'))
    self.mock.get_absolute_testcase_file.assert_called_once_with(
        '/test/test.html')
    self.mock.normalize_path.assert_called_once_with('/path')

  def test_url_match_127_0_0_1(self):
    """Tests matching a URL (127.0.0.1)."""
    self.mock.get_absolute_testcase_file.return_value = '/path'
    self.assertEqual(
        '/path',
        testcase_manager.convert_dependency_url_to_local_path(
            'http://127.0.0.1/test/test.html'))
    self.mock.get_absolute_testcase_file.assert_called_once_with(
        '/test/test.html')
    self.mock.normalize_path.assert_called_once_with('/path')

  def test_url_match_127_0_0_1_with_port(self):
    """Tests matching a URL (127.0.0.1)."""
    self.mock.get_absolute_testcase_file.return_value = '/path'
    self.assertEqual(
        '/path',
        testcase_manager.convert_dependency_url_to_local_path(
            'http://127.0.0.1:8000/test/test.html'))
    self.mock.get_absolute_testcase_file.assert_called_once_with(
        '/test/test.html')
    self.mock.normalize_path.assert_called_once_with('/path')

  def test_not_match(self):
    """Tests not matching."""
    self.assertIsNone(
        testcase_manager.convert_dependency_url_to_local_path(
            'http://www.google.com/test.'))
    self.assertIsNone(
        testcase_manager.convert_dependency_url_to_local_path('random'))
    self.assertEqual(0, self.mock.normalize_path.call_count)


class GetResourcePathsTest(unittest.TestCase):
  """Tests get_resource_paths."""

  def setUp(self):
    test_helpers.patch(self, [
        'bot.testcase_manager.convert_dependency_url_to_local_path',
    ])
    self.mock.convert_dependency_url_to_local_path.side_effect = lambda x: x

  def test_get(self):
    """Tests getting resource paths."""
    # pylint: disable=line-too-long
    output = (
        '[54110:54110:0802/103321.557701:VERBOSE1:gles2_cmd_decoder.cc(3412)] '
        'GL_EXT_packed_depth_stencil '
        'supported.\n[54110:54110:0802/103321.577720:VERBOSE1:gles2_cmd_decoder.cc(3412)]'
        ' GL_EXT_packed_depth_stencil '
        'supported.\n[54041:54075:0802/103321.618566:VERBOSE1:network_delegate.cc(31)]'
        ' NetworkDelegate::NotifyBeforeURLRequest: '
        'file:///projects/chromium/src/third_party/WebKit/LayoutTests/paint/invalidation/resources/text-based-repaint.js\n[54041:54075:0802/103321.618677:VERBOSE1:network_delegate.cc(31)]'
        ' NetworkDelegate::NotifyBeforeURLRequest: '
        'file:///projects/chromium/src/third_party/WebKit/LayoutTests/paint/invalidation/resources/text-based-repaint.js\n[54041:54075:0802/103321.618765:VERBOSE1:network_delegate.cc(31)]'
        ' NetworkDelegate::NotifyBeforeURLRequest: '
        'file:///projects/chromium/src/third_party/WebKit/LayoutTests/resources/test-harness.js\n[1:1:0802/103321.658122:VERBOSE1:script_context.cc(110)]'
        ' Created '
        'context:\r\n[3408:4060:0807/053137.661:54090359:VERBOSE1:network_delegate.cc(31)]'
        ' NetworkDelegate::NotifyBeforeURLRequest: '
        'http://127.0.0.1:8000/fuzzer-common-data-bundles/webkit/layouttests/fast/canvas/fuzz-http-11.html\r\nV/chromium('
        ' 8530): [VERBOSE1:network_delegate.cc(31)] '
        'NetworkDelegate::NotifyBeforeURLRequest: https://en.m.wikipedia.org  '
        'extension id:           (none)\n')

    result = testcase_manager.get_resource_paths(output)
    self.assertEqual(4, len(result))
    self.assertEqual(
        set([
            'file:///projects/chromium/src/third_party/WebKit/LayoutTests/paint/invalidation/resources/text-based-repaint.js',
            'file:///projects/chromium/src/third_party/WebKit/LayoutTests/resources/test-harness.js',
            'http://127.0.0.1:8000/fuzzer-common-data-bundles/webkit/layouttests/fast/canvas/fuzz-http-11.html',
            'https://en.m.wikipedia.org'
        ]), set(result))
    self.mock.convert_dependency_url_to_local_path.assert_has_calls([
        mock.call(
            'file:///projects/chromium/src/third_party/WebKit/LayoutTests/paint/invalidation/resources/text-based-repaint.js'
        ),
        mock.call(
            'file:///projects/chromium/src/third_party/WebKit/LayoutTests/resources/test-harness.js'
        ),
        mock.call(
            'http://127.0.0.1:8000/fuzzer-common-data-bundles/webkit/layouttests/fast/canvas/fuzz-http-11.html'
        ),
        mock.call('https://en.m.wikipedia.org')
    ])
    # pylint: enable=line-too-long


class GetCrashOutputTest(unittest.TestCase):
  """Tests _get_crash_output."""

  def test_none(self):
    self.assertEqual(None, testcase_manager._get_crash_output(None))  # pylint: disable=protected-access

  def test_no_end_marker(self):
    self.assertEqual('abc\ndef', testcase_manager._get_crash_output('abc\ndef'))  # pylint: disable=protected-access

  def test_end_marker(self):
    self.assertEqual(
        'abc\ndef\n',
        testcase_manager._get_crash_output(  # pylint: disable=protected-access
            'abc\ndef\nCRASH OUTPUT ENDS HERE\nghi'))


def mock_get_crash_data(output, symbolize_flag=True):  # pylint: disable=unused-argument
  """Mock get_crash_data."""
  if output == 'crash':
    stack_analyzer_state = stack_analyzer.StackAnalyzerState()
    stack_analyzer_state.crash_state = 'state'
    stack_analyzer_state.crash_type = 'Null-dereference'
    return stack_analyzer_state

  return stack_analyzer.StackAnalyzerState()


class TestcaseRunningTest(fake_filesystem_unittest.TestCase):
  """Tests for running testcases."""

  def setUp(self):
    test_helpers.patch_environ(self)
    test_utils.set_up_pyfakefs(self)

    test_helpers.patch(self, [
        'bot.fuzzers.engine.get',
        'bot.fuzzers.engine_common.find_fuzzer_path',
        'crash_analysis.stack_parsing.stack_analyzer.get_crash_data',
        'system.process_handler.run_process',
        'system.process_handler.terminate_stale_application_instances',
    ])

    os.environ['CRASH_RETRIES'] = '3'
    os.environ['FAIL_RETRIES'] = '3'
    os.environ['BOT_TMPDIR'] = '/bot/tmp'
    os.environ['USER_PROFILE_ROOT_DIR'] = '/user-profiles'
    os.environ['APP_NAME'] = 'app_name'
    os.environ['APP_PATH'] = '/build_dir/app_name'
    os.environ['BUILD_DIR'] = os.environ['APP_DIR'] = '/build_dir'
    os.environ['CRASH_STACKTRACES_DIR'] = '/crashes'
    os.environ['INPUT_DIR'] = '/input'
    os.environ['FUZZER_DIR'] = '/fuzzer'
    os.environ['WARMUP_TIMEOUT'] = '120'

    self.testcase = data_types.Testcase(crash_state='state')
    self.mock.find_fuzzer_path.return_value = '/build_dir/target'
    self.mock.run_process.return_value = (0, 0, 'output')
    self.mock.get.return_value = None

    self.mock.get_crash_data.side_effect = mock_get_crash_data
    self.fs.create_file('/flags-testcase', contents='target -arg1 -arg2')
    self.fs.create_dir('/bot/tmp')

  def test_test_for_crash_with_retries_blackbox_fail(self):
    """Test test_for_crash_with_retries failing to reproduce a crash
    (blackbox)."""
    crash_result = testcase_manager.test_for_crash_with_retries(
        self.testcase, '/fuzz-testcase', 10)
    self.assertEqual(0, crash_result.return_code)
    self.assertEqual(0, crash_result.crash_time)
    self.assertEqual('output', crash_result.output)
    self.assertEqual(3, self.mock.run_process.call_count)
    self.mock.run_process.assert_has_calls([
        mock.call(
            '/build_dir/app_name target -arg1 -arg2',
            current_working_directory='/build_dir',
            gestures=[],
            timeout=120),
        mock.call(
            '/build_dir/app_name target -arg1 -arg2',
            current_working_directory='/build_dir',
            gestures=[],
            timeout=10),
        mock.call(
            '/build_dir/app_name target -arg1 -arg2',
            current_working_directory='/build_dir',
            gestures=[],
            timeout=10),
    ])

  def test_test_for_crash_with_retries_greybox_fail(self):
    """Test test_for_crash_with_retries failing to reproduce a crash
    (greybox)."""
    mock_engine = mock.Mock()
    mock_engine.reproduce.return_value = engine.ReproduceResult(0, 0, 'output')
    self.mock.get.return_value = mock_engine

    crash_result = testcase_manager.test_for_crash_with_retries(
        self.testcase, '/fuzz-testcase', 10)
    self.assertEqual(0, crash_result.return_code)
    self.assertEqual(0, crash_result.crash_time)
    self.assertEqual('output', crash_result.output)
    self.assertEqual(3, mock_engine.reproduce.call_count)
    mock_engine.reproduce.assert_has_calls([
        mock.call('/build_dir/target', '/fuzz-testcase', ['-arg1', '-arg2'],
                  120),
        mock.call('/build_dir/target', '/fuzz-testcase', ['-arg1', '-arg2'],
                  10),
        mock.call('/build_dir/target', '/fuzz-testcase', ['-arg1', '-arg2'],
                  10),
    ])

  def test_test_for_crash_with_retries_blackbox_succeed(self):
    """Test test_for_crash_with_retries reproducing a crash (blackbox)."""
    self.mock.run_process.side_effect = [
        (0, 0, 'nope'),
        (1, 1, 'crash'),
    ]

    crash_result = testcase_manager.test_for_crash_with_retries(
        self.testcase, '/fuzz-testcase', 10)
    self.assertEqual(1, crash_result.return_code)
    self.assertEqual(1, crash_result.crash_time)
    self.assertEqual('crash', crash_result.output)
    self.assertEqual(2, self.mock.run_process.call_count)

    self.mock.run_process.assert_has_calls([
        mock.call(
            '/build_dir/app_name target -arg1 -arg2',
            current_working_directory='/build_dir',
            gestures=[],
            timeout=120),
        mock.call(
            '/build_dir/app_name target -arg1 -arg2',
            current_working_directory='/build_dir',
            gestures=[],
            timeout=10),
    ])

  def test_test_for_crash_with_retries_greybox_succeed(self):
    """Test test_for_crash_with_retries reproducing a crash (greybox)."""
    mock_engine = mock.Mock()
    mock_engine.reproduce.side_effect = [
        engine.ReproduceResult(0, 0, 'output'),
        engine.ReproduceResult(1, 1, 'crash'),
    ]
    self.mock.get.return_value = mock_engine

    crash_result = testcase_manager.test_for_crash_with_retries(
        self.testcase, '/fuzz-testcase', 10)
    self.assertEqual(1, crash_result.return_code)
    self.assertEqual(1, crash_result.crash_time)
    self.assertEqual('crash', crash_result.output)
    self.assertEqual(2, mock_engine.reproduce.call_count)
    mock_engine.reproduce.assert_has_calls([
        mock.call('/build_dir/target', '/fuzz-testcase', ['-arg1', '-arg2'],
                  120),
        mock.call('/build_dir/target', '/fuzz-testcase', ['-arg1', '-arg2'],
                  10),
    ])

  def test_test_for_reproducibility_blackbox(self):
    """Test test_for_reproducibility (blackbox)."""
    self.mock.run_process.return_value = (1, 1, 'crash')
    result = testcase_manager.test_for_reproducibility(
        'fuzzer',
        '/fuzz-testcase',
        'state',
        expected_security_flag=False,
        test_timeout=10,
        http_flag=False,
        gestures=None)
    self.assertTrue(result)

    # Only 2/3 runs needed to verify reproducibility.
    self.assertEqual(2, self.mock.run_process.call_count)

  def test_test_for_reproducibility_greybox(self):
    """Test test_for_reproducibility (greybox)."""
    mock_engine = mock.Mock()
    mock_engine.reproduce.return_value = engine.ReproduceResult(1, 1, 'crash')
    self.mock.get.return_value = mock_engine

    result = testcase_manager.test_for_reproducibility(
        'engine',
        '/fuzz-testcase',
        'state',
        expected_security_flag=False,
        test_timeout=10,
        http_flag=False,
        gestures=None)
    self.assertTrue(result)

    # Only 2/3 runs needed to verify reproducibility.
    self.assertEqual(2, mock_engine.reproduce.call_count)
