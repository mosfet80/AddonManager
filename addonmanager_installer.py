# SPDX-License-Identifier: LGPL-2.1-or-later
# ***************************************************************************
# *                                                                         *
# *   Copyright (c) 2022 FreeCAD Project Association                        *
# *                                                                         *
# *   This file is part of FreeCAD.                                         *
# *                                                                         *
# *   FreeCAD is free software: you can redistribute it and/or modify it    *
# *   under the terms of the GNU Lesser General Public License as           *
# *   published by the Free Software Foundation, either version 2.1 of the  *
# *   License, or (at your option) any later version.                       *
# *                                                                         *
# *   FreeCAD is distributed in the hope that it will be useful, but        *
# *   WITHOUT ANY WARRANTY; without even the implied warranty of            *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU      *
# *   Lesser General Public License for more details.                       *
# *                                                                         *
# *   You should have received a copy of the GNU Lesser General Public      *
# *   License along with FreeCAD. If not, see                               *
# *   <https://www.gnu.org/licenses/>.                                      *
# *                                                                         *
# ***************************************************************************

"""Contains the classes to manage Addon installation: intended as a stable API, safe for external
code to call and to rely upon existing. See classes AddonInstaller and MacroInstaller for details.
"""
import json
from datetime import datetime, timezone
from enum import IntEnum, auto
import os
import shutil
from typing import List, Optional
import tempfile
from urllib.parse import urlparse
import zipfile

import addonmanager_freecad_interface as fci

from PySideWrapper import QtCore

from Addon import Addon
import addonmanager_utilities as utils
from addonmanager_metadata import get_branch_from_metadata
from addonmanager_git import initialize_git, GitFailed

if fci.FreeCADGui:
    import NetworkManager  # Requires an event loop

translate = fci.translate

# pylint: disable=too-few-public-methods


class InstallationMethod(IntEnum):
    """For packages installed from a git repository, in most cases it is possible to either use git
    or to download a zip archive of the addon. For a local repository, a direct copy may be used
    instead. If "ANY" is given, the internal code decides which to use."""

    GIT = auto()
    COPY = auto()
    ZIP = auto()
    ANY = auto()


class AddonInstaller(QtCore.QObject):
    """The core, non-GUI installer class. Usually instantiated and moved to its own thread,
    otherwise it will block the GUI (if the GUI is running). In all cases in this class, the
    generic Python 'object' is intended to be an Addon-like object that provides, at a minimum,
    a 'name', 'url', and 'branch' attribute. The Addon manager uses the Addon class for this
    purpose, but external code may use any other class that meets those criteria.

    Recommended Usage (when running with the GUI up, so you don't block the GUI thread):

        import functools # With the rest of your imports, for functools.partial

        ...

        addon_to_install = MyAddon() # Some class with name, url, and branch attributes

        self.worker_thread = QtCore.QThread()
        self.installer = AddonInstaller(addon_to_install)
        self.installer.moveToThread(self.worker_thread)
        self.installer.success.connect(self.installation_succeeded)
        self.installer.failure.connect(self.installation_failed)
        self.installer.finished.connect(self.worker_thread.quit)
        self.worker_thread.started.connect(self.installer.run)
        self.worker_thread.start() # Returns immediately

        # On success, the connections above result in self.installation_succeeded being called, and
        # on failure, self.installation_failed is called.


    Recommended non-GUI usage (blocks until complete):

        addon_to_install = MyAddon() # Some class with name, url, and branch attributes
        installer = AddonInstaller(addon_to_install)
        installer.run()

    """

    # Signal: progress_update
    # In GUI mode this signal is emitted periodically during long downloads. The two integers are
    # the number of bytes downloaded, and the number of bytes expected, respectively. Note that the
    # number of bytes expected might be set to 0 to indicate an unknown download size.
    progress_update = QtCore.Signal(int, int)

    # Signals: success and failure
    # Emitted when the installation process is complete. The object emitted is the object that the
    # installation was requested for (usually of class Addon, but any class that provides a name,
    # url, and branch attribute can be used).
    success = QtCore.Signal(object)
    failure = QtCore.Signal(object, str)

    # Finished: regardless of the outcome, this is emitted when all work that is going to be done
    # is done (i.e. whatever thread this is running in can quit).
    finished = QtCore.Signal()

    allowed_packages = set()

    def __init__(self, addon: Addon, allow_list: List[str] = None):
        """Initialize the installer with an optional list of addons. If provided, then installation
        by name is supported, as long as the objects in the list contain a "name" and "url"
        property. In most use cases it is expected that addons is a List of Addon objects, but that
        is not a requirement. An optional allow_list lets calling code override the allowed Python
        packages list with a custom list. It is mostly for unit testing purposes."""
        super().__init__()
        self.addon_to_install = addon

        self.git_manager = initialize_git()

        if allow_list is not None:
            AddonInstaller.allowed_packages = set(allow_list if allow_list is not None else [])
        elif not AddonInstaller.allowed_packages:
            AddonInstaller._load_local_allowed_packages_list()
            AddonInstaller._update_allowed_packages_list()

        self.installation_path = fci.DataPaths().mod_dir
        self.macro_installation_path = fci.DataPaths().macro_dir
        self.zip_download_index = None

    def run(self, install_method: InstallationMethod = InstallationMethod.ANY) -> bool:
        """Install an addon. Returns True if the addon was installed, or False if not. Emits
        either success or failure prior to returning."""
        success = False
        try:
            addon_url = self.addon_to_install.url.replace(os.path.sep, "/")
            method_to_use = self._determine_install_method(addon_url, install_method)
            if method_to_use == InstallationMethod.ZIP:
                success = self._install_by_zip()
            elif method_to_use == InstallationMethod.GIT:
                success = self._install_by_git()
            elif method_to_use == InstallationMethod.COPY:
                success = self._install_by_copy()
            if (
                hasattr(self.addon_to_install, "contains_workbench")
                and self.addon_to_install.contains_workbench()
            ):
                self.addon_to_install.enable_workbench()
        except utils.ProcessInterrupted:
            # This isn't really an "exception" per se, it's that the user cancelled the operation,
            # so internally we just act like nothing bad happened, but still return false (as in,
            # the addon was not installed).
            pass
        except Exception as e:
            fci.Console.PrintLog(str(e) + "\n")
            success = False
        if success:
            if (
                hasattr(self.addon_to_install, "contains_workbench")
                and self.addon_to_install.contains_workbench()
            ):
                self.addon_to_install.set_status(Addon.Status.PENDING_RESTART)
            else:
                self.addon_to_install.set_status(Addon.Status.NO_UPDATE_AVAILABLE)
        self.finished.emit()
        return success

    @classmethod
    def _load_local_allowed_packages_list(cls) -> None:
        """Read in the local allow-list, in case the remote one is unavailable."""
        cls.allowed_packages.clear()
        allow_file = os.path.join(os.path.dirname(__file__), "ALLOWED_PYTHON_PACKAGES.txt")
        if os.path.exists(allow_file):
            with open(allow_file, encoding="utf8") as f:
                lines = f.readlines()
                for line in lines:
                    if line and len(line) > 0 and line[0] != "#":
                        cls.allowed_packages.add(line.strip().lower())

    @classmethod
    def _update_allowed_packages_list(cls) -> None:
        """Get a new remote copy of the allowed packages list from GitHub."""
        fci.Console.PrintLog("Attempting to fetch remote copy of ALLOWED_PYTHON_PACKAGES.txt...\n")
        p = utils.blocking_get(
            "https://raw.githubusercontent.com/"
            "FreeCAD/FreeCAD-addons/master/ALLOWED_PYTHON_PACKAGES.txt"
        )
        if p:
            fci.Console.PrintLog(
                "Overriding local ALLOWED_PYTHON_PACKAGES.txt with newer remote version\n"
            )
            p = p.decode("utf8")
            lines = p.split("\n")
            cls.allowed_packages.clear()  # Unset the locally-defined list
            for line in lines:
                if line and len(line) > 0 and line[0] != "#":
                    cls.allowed_packages.add(line.strip().lower())
        else:
            fci.Console.PrintLog(
                "Could not fetch remote ALLOWED_PYTHON_PACKAGES.txt, using local copy\n"
            )

    def _determine_install_method(
        self, addon_url: str, install_method: InstallationMethod
    ) -> Optional[InstallationMethod]:
        """Given a URL and preferred installation method, determine the actual installation method
        to use. Will return either None, if installation is not possible for the given url and
        method, or a specific concrete method (GIT, ZIP, or COPY) based on the inputs."""

        # If we don't have access to git, and that is the method selected, return early
        if not self.git_manager and install_method == InstallationMethod.GIT:
            return None

        parse_result = urlparse(addon_url)
        is_git_only = parse_result.scheme in ["git", "ssh", "rsync"]
        is_remote = parse_result.scheme in ["http", "https", "git", "ssh", "rsync"]
        is_zipfile = parse_result.path.lower().endswith(".zip")

        # Can't use "copy" for a remote URL
        if is_remote and install_method == InstallationMethod.COPY:
            return None

        if is_git_only:
            if (
                install_method in (InstallationMethod.GIT, InstallationMethod.ANY)
            ) and self.git_manager:
                # If it's a git-only URL, only git can be used for the installation
                return InstallationMethod.GIT
            # So if it's not a git installation, return None
            return None

        if is_zipfile:
            if install_method == InstallationMethod.GIT:
                # Can't use git on zip files
                return None
            return InstallationMethod.ZIP  # Copy just becomes zip
        if not is_remote and install_method == InstallationMethod.ZIP:
            return None  # Can't use zip on local paths that aren't zip files

        # Whatever scheme was passed in appears to be reasonable, return it
        if install_method != InstallationMethod.ANY:
            return install_method

        # Prefer to copy, if it's local:
        if not is_remote:
            return InstallationMethod.COPY

        # Prefer git if we have git
        if self.git_manager:
            return InstallationMethod.GIT

        # Fall back to ZIP in other cases, though this relies on remote hosts falling
        # into one of a few particular patterns
        return InstallationMethod.ZIP

    def _install_by_copy(self) -> bool:
        """Installs the specified url by copying directly from it into the installation
        location. addon_url must be copyable using filesystem operations. Any existing files at
        that location are overwritten."""
        addon_url = self.addon_to_install.url
        if addon_url.startswith("file://"):
            addon_url = addon_url[len("file://") :]  # Strip off the file:// part
        name = self.addon_to_install.name
        shutil.copytree(
            addon_url, str(os.path.join(self.installation_path, name)), dirs_exist_ok=True
        )
        self._finalize_successful_installation()
        return True

    def _can_use_update(self) -> bool:
        addon = self.addon_to_install
        install_path = os.path.join(self.installation_path, self.addon_to_install.name)
        if not os.path.isdir(install_path):
            return False
        if addon.metadata is None or addon.installed_metadata is None:
            return True  # We can't check if the branch name changed, but the install path exists
        old_branch = get_branch_from_metadata(self.addon_to_install.installed_metadata)
        new_branch = get_branch_from_metadata(self.addon_to_install.metadata)
        if old_branch != new_branch:
            return False  # Branch name changed, we have to re-clone
        return True  # Checkout exists, same branch as last time, update OK

    def _install_by_git(self) -> bool:
        """Installs the specified url by using git to clone from it. The URL can be local or remote,
        but must represent a git repository, and the url must be in a format that git can handle
        (git, ssh, rsync, file, or a bare filesystem path)."""
        install_path = str(os.path.join(self.installation_path, self.addon_to_install.name))
        try:
            if self._can_use_update():
                self.git_manager.update(install_path)
            else:
                if os.path.isdir(install_path):
                    utils.rmdir(install_path)
                self.git_manager.clone(self.addon_to_install.url, install_path)
            self.git_manager.checkout(install_path, self.addon_to_install.branch)
        except GitFailed as e:
            self.failure.emit(self.addon_to_install, str(e))
            return False
        self._finalize_successful_installation()
        return True

    def _install_by_zip(self) -> bool:
        """Installs the specified url by downloading the file (if it is remote) and unzipping it
        into the appropriate installation location. If the GUI is running the download is
        asynchronous, and issues periodic updates about how much data has been downloaded."""
        if self.addon_to_install.url.endswith(".zip"):
            zip_url = self.addon_to_install.url
        else:
            zip_url = utils.get_zip_url(self.addon_to_install)

        fci.Console.PrintLog(f"Downloading ZIP file from {zip_url}...\n")
        parse_result = urlparse(zip_url)
        is_remote = parse_result.scheme in ["http", "https"]

        if is_remote:
            if fci.FreeCADGui:
                self._run_zip_downloader_in_event_loop(zip_url)
            else:
                zip_data = utils.blocking_get(zip_url)
                with tempfile.NamedTemporaryFile(delete=False) as f:
                    tempfile_name = f.name
                    f.write(zip_data)
                self._finalize_zip_installation(tempfile_name)
        else:
            self._finalize_zip_installation(zip_url)
        return True

    def _run_zip_downloader_in_event_loop(self, zip_url: str):
        """Runs the zip downloader in a private event loop. This function does not exit until the
        ZIP download is complete. It requires the GUI to be up, and should not be run on the main
        GUI thread."""
        NetworkManager.AM_NETWORK_MANAGER.progress_made.connect(self._update_zip_status)
        NetworkManager.AM_NETWORK_MANAGER.progress_complete.connect(self._finish_zip)
        self.zip_download_index = NetworkManager.AM_NETWORK_MANAGER.submit_monitored_get(zip_url)
        while self.zip_download_index is not None:
            if QtCore.QThread.currentThread().isInterruptionRequested():
                break
            QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.AllEvents, 50)

    def _update_zip_status(self, index: int, bytes_read: int, data_size: int):
        """Called periodically when downloading a zip file, emits a signal to display the
        download progress."""
        if index == self.zip_download_index:
            self.progress_update.emit(bytes_read, data_size)

    def _finish_zip(self, index: int, response_code: int, filename: str):
        """Once the zip download is finished, unzip it into the correct location. Only called if
        the GUI is up, and the NetworkManager was responsible for the download. Do not call
        directly."""
        if index != self.zip_download_index:
            return
        self.zip_download_index = None
        if response_code != 200:
            self.failure.emit(
                self.addon_to_install,
                translate("AddonsInstaller", "Received {} response code from server").format(
                    response_code
                ),
            )
            return
        QtCore.QCoreApplication.processEvents(QtCore.QEventLoop.AllEvents)

        fci.Console.PrintLog("ZIP download complete. Installing...\n")
        self._finalize_zip_installation(filename)

    def _finalize_zip_installation(self, filename: str):
        """Given a path to a zipfile, extract that file and put its contents in the correct
        location. Has special handling for GitHub's zip structure, which places the data in a
        subdirectory of the main directory."""

        destination = str(os.path.join(self.installation_path, self.addon_to_install.name))
        if os.path.exists(destination):
            remove_succeeded = utils.rmdir(destination)
            if not remove_succeeded:
                fci.Console.PrintError(f"Failed to remove {destination}, aborting update")
                raise RuntimeError(f"Failed to remove outdated addon from {destination}")

        with zipfile.ZipFile(filename, "r") as zfile:
            zfile.extractall(destination)

        # GitHub (and possibly other hosts) put all files in the zip into a subdirectory named
        # after the branch. If that is the setup that we just extracted, move all files out of
        # that subdirectory.
        if self._code_in_branch_subdirectory(destination):
            actual_path = os.path.join(
                destination, f"{self.addon_to_install.name}-{self.addon_to_install.branch}"
            )
            fci.Console.PrintLog(
                f"ZIP installation moving code from {actual_path} to {destination}"
            )
            self._move_code_out_of_subdirectory(destination)

        fci.Console.PrintLog("ZIP installation complete.\n")
        self._finalize_successful_installation()

    def _code_in_branch_subdirectory(self, destination: str) -> bool:
        test_path = os.path.join(destination, self._expected_subdirectory_name())
        fci.Console.PrintLog(f"Checking for possible zip sub-path {test_path}...")
        if os.path.isdir(test_path):
            fci.Console.PrintLog(f"path exists.\n")
            return True
        fci.Console.PrintLog(f"path does not exist.\n")
        return False

    def _expected_subdirectory_name(self) -> str:
        url = self.addon_to_install.url
        if url.endswith("/"):
            url = url[:-1]
        if url.endswith(".git"):
            url = url[:-4]
        _, _, name = url.rpartition("/")
        branch = self.addon_to_install.branch
        return f"{name}-{branch}"

    def _move_code_out_of_subdirectory(self, destination):
        subdirectory = os.path.join(destination, self._expected_subdirectory_name())
        for extracted_filename in os.listdir(os.path.join(destination, subdirectory)):
            shutil.move(
                os.path.join(destination, subdirectory, extracted_filename),
                os.path.join(destination, extracted_filename),
            )
        os.rmdir(os.path.join(destination, subdirectory))

    def _finalize_successful_installation(self):
        """Perform any necessary additional steps after installing the addon."""
        self._update_metadata()
        self._install_macros()
        self.success.emit(self.addon_to_install)

    def _update_metadata(self):
        """Loads the package metadata from the Addon's downloaded package.xml file."""
        package_xml = str(
            os.path.join(self.installation_path, self.addon_to_install.name, "package.xml")
        )

        if hasattr(self.addon_to_install, "metadata") and os.path.isfile(package_xml):
            self.addon_to_install.load_metadata_file(package_xml)
            self.addon_to_install.installed_version = self.addon_to_install.metadata.version
            self.addon_to_install.updated_timestamp = os.path.getmtime(package_xml)

    def _install_macros(self):
        """For any workbenches, copy FCMacro files into the macro directory. Exclude packages that
        have preference packs, otherwise we will litter the macro directory with the pre and post
        scripts."""
        if (
            isinstance(self.addon_to_install, Addon)
            and self.addon_to_install.contains_preference_pack()
        ):
            return

        if not os.path.exists(self.macro_installation_path):
            os.makedirs(self.macro_installation_path)

        installed_macro_files = []
        for root, _, files in os.walk(
            str(os.path.join(self.installation_path, self.addon_to_install.name))
        ):
            for f in files:
                if f.lower().endswith(".fcmacro"):
                    src = os.path.join(root, f)
                    dst = os.path.join(self.macro_installation_path, f)
                    shutil.copy2(src, dst)
                    installed_macro_files.append(dst)
        if installed_macro_files:
            with open(
                os.path.join(
                    self.installation_path,
                    self.addon_to_install.name,
                    "AM_INSTALLATION_DIGEST.txt",
                ),
                "a",
                encoding="utf-8",
            ) as f:
                now = datetime.now(timezone.utc)
                f.write(
                    "# The following files were created outside this installation "
                    f"path during the installation of this addon on {now}:\n"
                )
                for fcmacro_file in installed_macro_files:
                    f.write(fcmacro_file + "\n")

    @classmethod
    def _validate_object(cls, addon: object):
        """Make sure the object has the necessary attributes (name, url, and branch) to be
        installed."""

        if not hasattr(addon, "name") or not hasattr(addon, "url") or not hasattr(addon, "branch"):
            raise RuntimeError(
                "Provided object does not provide a name, url, and/or branch attribute"
            )


class MacroInstaller(QtCore.QObject):
    """Install a macro."""

    # Signals: success and failure
    # Emitted when the installation process is complete. The object emitted is the object that the
    # installation was requested for (usually of class Addon, but any class that provides a macro
    # can be used).
    success = QtCore.Signal(object)
    failure = QtCore.Signal(object)

    # Finished: regardless of the outcome, this is emitted when all work that is going to be done
    # is done (i.e. whatever thread this is running in can quit).
    finished = QtCore.Signal()

    def __init__(self, addon):
        """The provided addon object must have an attribute called "macro", and that attribute must
        itself provide a callable "install" method that takes a single string, the path to the
        installation location."""
        super().__init__()
        self._validate_object(addon)
        self.addon_to_install = addon
        self.installation_path = fci.DataPaths().macro_dir

    def run(self) -> bool:
        """Install a macro. Returns True if the macro was installed, or False if not. Emits
        either success or failure prior to returning."""

        # To try to ensure atomicity, perform the installation into a temp directory
        macro = self.addon_to_install.macro
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_install_succeeded, error_list = macro.install(temp_dir)
            if not temp_install_succeeded:
                fci.Console.PrintError(
                    translate("AddonsInstaller", "Failed to install macro {}").format(macro.name)
                    + "\n"
                )
                for e in error_list:
                    fci.Console.PrintError(e + "\n")
                self.failure.emit(self.addon_to_install, "\n".join(error_list))
                self.finished.emit()
                return False

            # If it succeeded, move all the files to the macro install location,
            # keeping a list of all the files we installed, so they can be removed later
            # if this macro is uninstalled.
            manifest = []
            for item in os.listdir(temp_dir):
                src = os.path.join(temp_dir, item)
                dst = os.path.join(self.installation_path, item)
                shutil.move(src, dst)
                manifest.append(dst)
            self._write_installation_manifest(manifest)
        self.success.emit(self.addon_to_install)
        self.addon_to_install.set_status(Addon.Status.NO_UPDATE_AVAILABLE)
        self.finished.emit()
        return True

    def _write_installation_manifest(self, manifest):
        manifest_file = os.path.join(
            self.installation_path, self.addon_to_install.macro.filename + ".manifest"
        )
        try:
            with open(manifest_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(manifest, indent="  "))
        except OSError as e:
            fci.Console.PrintWarning(
                translate("AddonsInstaller", "Failed to create installation manifest " "file:\n")
            )
            fci.Console.PrintWarning(manifest_file)

    @classmethod
    def _validate_object(cls, addon):
        """Make sure this object provides an attribute called "macro" with a method called
        "install" """
        if (
            not hasattr(addon, "macro")
            or addon.macro is None
            or not hasattr(addon.macro, "install")
            or not callable(addon.macro.install)
        ):
            raise RuntimeError("Provided object does not provide a macro with an install method")
