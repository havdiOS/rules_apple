# Lint as: python2, python3
# Copyright 2020 The Bazel Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from build_bazel_rules_apple.tools.wrapper_common import execute


def invoke_bitcode_strip(binary_path, output_path):
  """Wraps bitcode_strip with given arguments for inputs and outputs."""
  cmd = ["xcrun", "bitcode_strip", binary_path, "-r", "-o", output_path]

  _, stdout, stderr = execute.execute_and_filter_output(cmd,
                                                        raise_on_failure=True)
  if stdout:
    print(stdout)
  if stderr:
    print(stderr)
