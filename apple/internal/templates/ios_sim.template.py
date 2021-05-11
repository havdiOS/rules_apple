#!/usr/bin/env python3

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

"""Invoked by `bazel run` to launch ios_application targets in the simulator."""

# This script works in one of two modes.
#
# If either --ios_simulator_version or --ios_simulator_device were not
# passed to bazel:
#
# 1. Discovers a simulator compatible with the minimum_os of the
#    ios_application target, preferring already-booted simulators
#    if possible
# 2. Boots the simulator if needed
# 3. Installs and launches the application
# 4. Displays the application's output on the console
#
# This mode does not kill running simulators or shutdown or delete the simulator
# after it completes.
#
# If --ios_simulator_version and --ios_simulator_device were both passed
# to bazel:
#
# 1. Creates a new temporary simulator by running "simctl create ..."
# 2. Boots the new temporary simulator
# 3. Installs and launches the application
# 4. Displays the application's output on the console
# 5. When done, shuts down and deletes the newly-created simulator
#
# All environment variables with names starting with "IOS_" are passed to the
# application, after stripping the prefix "IOS_".

import collections.abc
import contextlib
import json
import logging
import os
import os.path
import platform
import plistlib
import shutil
import subprocess
import tempfile
import time
import zipfile

logging.basicConfig(
    format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO)
logger = logging.getLogger(__name__)

if platform.system() != "Darwin":
  raise Exception("Cannot run iOS targets on a non-mac machine.")


class DeviceType(collections.abc.Mapping):
  """Wraps the `devicetype` dictionary from `simctl list -j`.

  Provides an ordering so iPhones > iPads. In addition, maintains the
  original order from `simctl list` as `simctl_list_index` to ensure
  newer device types are sorted after older device types.
  """

  def __init__(self, device_type, simctl_list_index):
    self.device_type = device_type
    self.simctl_list_index = simctl_list_index

  def __getitem__(self, name):
    return self.device_type[name]

  def __iter__(self):
    return iter(self.device_type)

  def __len__(self):
    return len(self.device_type)

  def __repr__(self):
    return self["name"] + " (" + self["identifier"] + ")"

  def __lt__(self, other):
    # Order iPhones ahead of (later in the list than) iPads.
    if self.is_ipad() and other.is_iphone():
      return True
    elif self.is_iphone() and other.is_ipad():
      return False
    # Order device types from the same product family in the same order
    # as `simctl list`.
    return self.simctl_list_index < other.simctl_list_index

  def is_iphone(self):
    return self.has_product_family_or_identifier("iPhone")

  def is_ipad(self):
    return self.has_product_family_or_identifier("iPad")

  def has_product_family_or_identifier(self, device_type):
    product_family = self.get("productFamily")
    if product_family:
      return product_family == device_type
    # Some older simulators are missing `productFamily`. Try to guess from the
    # identifier.
    return device_type in self["identifier"]


class Device(collections.abc.Mapping):
  """Wraps the `device` dictionary from `simctl list -j`.

  Provides an ordering so booted devices > shutdown devices, delegating
  to `DeviceType` order when both devices have the same state.
  """

  def __init__(self, device, device_type):
    self.device = device
    self.device_type = device_type

  def is_shutdown(self):
    return self["state"] == "Shutdown"

  def is_booted(self):
    return self["state"] == "Booted"

  def __getitem__(self, name):
    return self.device[name]

  def __iter__(self):
    return iter(self.device)

  def __len__(self):
    return len(self.device)

  def __repr__(self):
    return self["name"] + "(" + self["udid"] + ")"

  def __lt__(self, other):
    if self.is_shutdown() and other.is_booted():
      return True
    elif self.is_booted() and other.is_shutdown():
      return False
    else:
      return self.device_type < other.device_type


def minimum_os_to_simctl_runtime_version(minimum_os):
  """Converts a minimum OS string to a simctl RuntimeVersion integer.

  Args:
    minimum_os: A string in the form '12.2' or '13.2.3'.

  Returns:
    An integer in the form 0xAABBCC, where AA is the major version, BB is
    the minor version, and CC is the micro version.
  """
  # Pad the minimum OS version to major.minor.micro.
  minimum_os_components = (minimum_os.split(".") + ["0"] * 3)[:3]
  result = 0
  for component in minimum_os_components:
    result = (result << 8) | int(component)
  return result


def discover_best_compatible_simulator(simctl_path, minimum_os, sim_device,
                                       sim_os_version):
  """Discovers the best compatible simulator device type and device.

  Args:
    simctl_path: The path to the `simctl` binary.
    minimum_os: The minimum OS version required by the ios_application() target.
    sim_device: Optional name of the device (e.g. "iPhone 8 Plus").
    sim_os_version: Optional version of the iOS runtime (e.g. "13.2").

  Returns:
    A tuple (device_type, device) containing the DeviceType and Device
    of the best compatible simulator (might be None if no match was found).

  Raises:
    subprocess.SubprocessError: if `simctl list` fails or times out.
  """
  # The `simctl list` CLI provides only very basic case-insensitive description
  # matching search term functionality.
  #
  # This code needs to enforce a numeric floor on `minimum_os`, so it directly
  # parses the JSON output by `simctl list` instead of repeatedly invoking
  # `simctl list` with search terms.
  cmd = [simctl_path, "list", "-j"]
  with subprocess.Popen(cmd, stdout=subprocess.PIPE) as process:
    simctl_data = json.load(process.stdout)
    if process.wait() != os.EX_OK:
      raise subprocess.CalledProcessError(process.returncode, cmd)
  compatible_device_types = []
  minimum_runtime_version = minimum_os_to_simctl_runtime_version(minimum_os)
  # Prepare the device name for case-insensitive matching.
  sim_device = sim_device and sim_device.casefold()
  # `simctl list` orders device types from oldest to newest. Remember
  # the index of each device type to preserve that ordering when
  # sorting device types.
  for (simctl_list_index, device_type) in enumerate(simctl_data["devicetypes"]):
    device_type = DeviceType(device_type, simctl_list_index)
    if not (device_type.is_iphone() or device_type.is_ipad()):
      continue
    # Some older simulators are missing `maxRuntimeVersion`. Assume those
    # simulators support all OSes (even though it's not true).
    max_runtime_version = device_type.get("maxRuntimeVersion")
    if max_runtime_version and max_runtime_version < minimum_runtime_version:
      continue
    if sim_device and device_type["name"].casefold().find(sim_device) == -1:
      continue
    compatible_device_types.append(device_type)
  compatible_device_types.sort()
  logger.debug("Found %d compatible device types.",
               len(compatible_device_types))
  compatible_runtime_identifiers = set()
  for runtime in simctl_data["runtimes"]:
    if not runtime["isAvailable"]:
      continue
    if sim_os_version and runtime["version"] != sim_os_version:
      continue
    compatible_runtime_identifiers.add(runtime["identifier"])
  compatible_devices = []
  for runtime_identifier, devices in simctl_data["devices"].items():
    if runtime_identifier not in compatible_runtime_identifiers:
      continue
    for device in devices:
      if not device["isAvailable"]:
        continue
      compatible_device = None
      for device_type in compatible_device_types:
        if device["deviceTypeIdentifier"] == device_type["identifier"]:
          compatible_device = Device(device, device_type)
          break
      if not compatible_device:
        continue
      compatible_devices.append(compatible_device)
  compatible_devices.sort()
  logger.debug("Found %d compatible devices.", len(compatible_devices))
  if compatible_device_types:
    best_compatible_device_type = compatible_device_types[-1]
  else:
    best_compatible_device_type = None
  if compatible_devices:
    best_compatible_device = compatible_devices[-1]
  else:
    best_compatible_device = None
  return (best_compatible_device_type, best_compatible_device)


def persistent_ios_simulator(simctl_path, minimum_os, sim_device,
                             sim_os_version):
  """Finds or creates a persistent compatible iOS simulator.

  Boots the simulator if needed. Does not shut down or delete the simulator when
  done.

  Args:
    simctl_path: The path to the `simctl` binary.
    minimum_os: The minimum OS version required by the ios_application() target.
    sim_device: Optional name of the device (e.g. "iPhone 8 Plus").
    sim_os_version: Optional version of the iOS runtime (e.g. "13.2").

  Returns:
    The UDID of the compatible iOS simulator.

  Raises:
    Exception: if a compatible simulator was not found.
  """
  (best_compatible_device_type,
   best_compatible_device) = discover_best_compatible_simulator(
       simctl_path, minimum_os, sim_device, sim_os_version)
  if best_compatible_device:
    udid = best_compatible_device["udid"]
    if best_compatible_device.is_shutdown():
      logger.debug("Booting compatible device: %s", best_compatible_device)
      subprocess.run([simctl_path, "boot", udid], check=True)
    else:
      logger.debug("Using compatible device: %s", best_compatible_device)
    return udid
  if best_compatible_device_type:
    device_name = best_compatible_device_type["name"]
    device_id = best_compatible_device_type["identifier"]
    logger.info("Creating new %s simulator", device_name)
    create_result = subprocess.run(
        [simctl_path, "create", device_name, device_id],
        encoding="utf-8",
        stdout=subprocess.PIPE,
        check=True)
    udid = create_result.stdout.rstrip()
    logger.debug("Created new simulator: %s", udid)
    return udid
  raise Exception(
      "Could not find or create a simulator compatible with minimum OS version %s (device name %s, OS version %s)"
      % (minimum_os, sim_device, sim_os_version))


def wait_for_sim_to_boot(simctl_path, udid):
  """Blocks until the given simulator is booted.

  Args:
    simctl_path: The path to the `simctl` binary.
    udid: The identifier of the simulator to wait for.

  Returns:
    True if the simulator boots within 60 seconds, False otherwise.
  """
  logger.info("Waiting for simulator to boot...")
  for _ in range(0, 60):
    # The expected output of "simctl list" is like:
    # -- iOS 8.4 --
    # iPhone 5s (E946FA1C-26AB-465C-A7AC-24750D520BEA) (Shutdown)
    # TestDevice (8491C4BC-B18E-4E2D-934A-54FA76365E48) (Booted)
    # So if there's any booted simulator, $booted_device will not be empty.
    simctl_list_result = subprocess.run([simctl_path, "list", "devices"],
                                        encoding="utf-8",
                                        check=True,
                                        stdout=subprocess.PIPE)
    for line in simctl_list_result.stdout.split("\n"):
      if line.find(udid) != -1 and line.find("Booted") != -1:
        logger.debug("Simulator is booted.")
        # Simulator is booted.
        return True
    logger.debug("Simulator not booted, still waiting...")
    time.sleep(1)
  return False


def boot_simulator(developer_path, simctl_path, udid):
  """Launches the iOS simulator for the given identifier.

  Ensures the Simulator process is in the foreground.

  Args:
    developer_path: The path to /Applications/Xcode.app/Contents/Developer.
    simctl_path: The path to the `simctl` binary.
    udid: The identifier of the simulator to wait for.

  Raises:
    Exception: if the simulator did not launch within 60 seconds.
  """
  logger.info("Launching simulator with udid: %s", udid)
  # Using subprocess.Popen() to launch Simulator.app and then
  # `osascript -e "tell application \"Simulator\" to activate" is racy
  # and can fail with:
  #
  #   Simulator got an error: Connection is invalid. (-609)
  #
  # This is likely because the newly-spawned Simulator.app process
  # hasn't had time to connect to the Apple Events system which
  # `osascript` relies on.
  simulator_path = os.path.join(developer_path, "Applications/Simulator.app")
  subprocess.run(
      ["open", "-a", simulator_path, "--args", "-CurrentDeviceUDID", udid],
      check=True)
  logger.debug("Simulator launched.")
  if not wait_for_sim_to_boot(simctl_path, udid):
    raise Exception("Failed to launch simulator with UDID: " + udid)


@contextlib.contextmanager
def temporary_ios_simulator(simctl_path, device, version):
  """Creates a temporary iOS simulator, cleaned up automatically upon close.

  Args:
    simctl_path: The path to the `simctl` binary.
    device: The name of the device (e.g. "iPhone 8 Plus").
    version: The version of the iOS runtime (e.g. "13.2").

  Yields:
    The UDID of the newly-created iOS simulator.
  """
  runtime_version_name = version.replace(".", "-")
  logger.info("Creating simulator, device=%s, version=%s", device, version)
  simctl_create_result = subprocess.run([
      simctl_path, "create", "TestDevice", device,
      "com.apple.CoreSimulator.SimRuntime.iOS-" + runtime_version_name
  ],
                                        encoding="utf-8",
                                        check=True,
                                        stdout=subprocess.PIPE)
  udid = simctl_create_result.stdout.rstrip()
  try:
    logger.info("Killing all running simulators...")
    subprocess.run(["pkill", "Simulator"],
                   stderr=subprocess.DEVNULL,
                   check=False)
    yield udid
  finally:
    logger.info("Shutting down simulator with udid: %s", udid)
    subprocess.run([simctl_path, "shutdown", udid],
                   stderr=subprocess.DEVNULL,
                   check=False)
    logger.info("Deleting simulator with udid: %s", udid)
    subprocess.run([simctl_path, "delete", udid], check=True)


@contextlib.contextmanager
def extracted_app(ios_application_output_path, app_name):
  """Extracts Foo.app from an ios_application() rule's output.

  Args:
    ios_application_output_path: Path to the output of an `ios_application()`.
      If the path is an .ipa archive, unzips it to a temporary directory.
    app_name: The name of the application (e.g. "Foo" for "Foo.app").

  Yields:
    Path to Foo.app.
  """
  if os.path.isdir(ios_application_output_path):
    logger.debug("Found app directory: %s", ios_application_output_path)
    with tempfile.TemporaryDirectory(prefix="bazel_temp") as temp_dir:
      temp_app_path = os.path.join(temp_dir, app_name + ".app")
      shutil.copytree(ios_application_output_path, temp_app_path)
      for root, dirs, _ in os.walk(temp_app_path):
        for directory in dirs:
          os.chmod(os.path.join(root, directory), 0o777)
      os.chmod(temp_app_path, 0o777)
      yield temp_app_path
  else:
    with tempfile.TemporaryDirectory(prefix="bazel_temp") as temp_dir:
      logger.debug("Unzipping IPA from %s to %s", ios_application_output_path,
                   temp_dir)
      with zipfile.ZipFile(ios_application_output_path) as ipa_zipfile:
        ipa_zipfile.extractall(temp_dir)
        yield os.path.join(temp_dir, "Payload", app_name + ".app")


def bundle_id(bundle_path):
  """Returns the bundle ID given a bundle directory path."""
  info_plist_path = os.path.join(bundle_path, "Info.plist")
  with open(info_plist_path, mode="rb") as plist_file:
    plist = plistlib.load(plist_file)
    return plist["CFBundleIdentifier"]


def simctl_launch_environ():
  """Calculates an environment dictionary for running `simctl launch`."""
  # Pass environment variables prefixed with "IOS_" to the simulator, replace
  # the prefix with "SIMCTL_CHILD_". bazel adds "IOS_" to the env vars which
  # will be passed to the app as prefix to differentiate from other env vars. We
  # replace the prefix "IOS_" with "SIMCTL_CHILD_" here, because "simctl" only
  # pass the env vars prefixed with "SIMCTL_CHILD_" to the app.
  result = {}
  for k, v in os.environ.items():
    if not k.startswith("IOS_"):
      continue
    new_key = k.replace("IOS_", "SIMCTL_CHILD_", 1)
    result[new_key] = v
  if 'IDE_DISABLED_OS_ACTIVITY_DT_MODE' not in os.environ:
    # Ensure os_log() mirrors writes to stderr. (lldb and Xcode set this
    # environment variable as well.)
    result["SIMCTL_CHILD_OS_ACTIVITY_DT_MODE"] = "enable"
  return result


@contextlib.contextmanager
def ios_simulator(simctl_path, minimum_os, sim_device, sim_os_version):
  """Finds either a temporary or persistent iOS simulator based on args.

  Args:
    simctl_path: The path to the `simctl` binary.
    minimum_os: The minimum OS version required by the ios_application() target.
    sim_device: Optional name of the device (e.g. "iPhone 8 Plus").
    sim_os_version: Optional version of the iOS runtime (e.g. "13.2").

  Yields:
    The UDID of the simulator.
  """
  yield persistent_ios_simulator(simctl_path, minimum_os, sim_device,
                                   sim_os_version)
  # if sim_device and sim_os_version:
  #   with temporary_ios_simulator(simctl_path, sim_device,
  #                                sim_os_version) as udid:
  #     yield udid
  # else:
  #   yield persistent_ios_simulator(simctl_path, minimum_os, sim_device,
  #                                  sim_os_version)


def run_app_in_simulator(simulator_udid, developer_path, simctl_path,
                         ios_application_output_path, app_name):
  """Installs and runs an app in the specified simulator.

  Args:
    simulator_udid: The UDID of the simulator in which to run the app.
    developer_path: The path to /Applications/Xcode.app/Contents/Developer.
    simctl_path: The path to the `simctl` binary.
    ios_application_output_path: Path to the output of an `ios_application()`.
    app_name: The name of the application (e.g. "Foo" for "Foo.app").
  """
  boot_simulator(developer_path, simctl_path, simulator_udid)
  with extracted_app(ios_application_output_path, app_name) as app_path:
    logger.debug("Installing app %s to simulator %s", app_path, simulator_udid)
    subprocess.run([simctl_path, "install", simulator_udid, app_path],
                   check=True)
    app_bundle_id = bundle_id(app_path)
    logger.info("Launching app %s in simulator %s", app_bundle_id,
                simulator_udid)
    args = [
        simctl_path, "launch", "--console-pty", simulator_udid, app_bundle_id
    ]
    subprocess.run(args, env=simctl_launch_environ(), check=False)


def main(sim_device, sim_os_version, ios_application_output_path, app_name,
         minimum_os):
  """Main entry point to `bazel run` for ios_application() targets.

  Args:
    sim_device: The name of the device (e.g. "iPhone 8 Plus").
    sim_os_version: The version of the iOS runtime (e.g. "13.2").
    ios_application_output_path: Path to the output of an `ios_application()`.
    app_name: The name of the application (e.g. "Foo" for "Foo.app").
    minimum_os: The minimum OS version required by the ios_application() target.
  """
  xcode_select_result = subprocess.run(["xcode-select", "-p"],
                                       encoding="utf-8",
                                       check=True,
                                       stdout=subprocess.PIPE)
  developer_path = xcode_select_result.stdout.rstrip()
  simctl_path = os.path.join(developer_path, "usr", "bin", "simctl")

  with ios_simulator(simctl_path, minimum_os, sim_device,
                     sim_os_version) as simulator_udid:
    run_app_in_simulator(simulator_udid, developer_path, simctl_path,
                         ios_application_output_path, app_name)


if __name__ == "__main__":
  try:
    # Tempate values filled in by rules_apple/apple/internal/run_support.bzl.
    main("%sim_device%", "%sim_os_version%", "%ipa_file%", "%app_name%",
         "%minimum_os%")
  except subprocess.CalledProcessError as e:
    logger.error("%s exited with error code %d", e.cmd, e.returncode)
  except KeyboardInterrupt:
    pass
