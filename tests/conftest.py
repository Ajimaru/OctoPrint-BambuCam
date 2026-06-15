"""Shared fixtures for BambuCam tests."""

# pylint: disable=protected-access,import-outside-toplevel
# pylint: disable=redefined-outer-name

import logging
import sys
import types
from enum import Enum
from unittest.mock import MagicMock

import pytest

from octoprint_bambucam.daemon import WebcamdManager  # noqa: E402


def _make_octoprint_stubs() -> None:
    """Create minimal OctoPrint stubs for import without OctoPrint installed.

    Uses SimpleNamespace (not ModuleType) so that arbitrary attribute
    assignment is type-safe and does not trigger Pylance/Pylint warnings.
    """

    def _mixin(name: str) -> type:
        return type(name, (), {"__init__": lambda self: None})

    # octoprint.plugin -------------------------------------------------------
    op_plugin = types.SimpleNamespace(
        StartupPlugin=_mixin("StartupPlugin"),
        ShutdownPlugin=_mixin("ShutdownPlugin"),
        SettingsPlugin=_mixin("SettingsPlugin"),
        TemplatePlugin=_mixin("TemplatePlugin"),
        AssetPlugin=_mixin("AssetPlugin"),
        SimpleApiPlugin=_mixin("SimpleApiPlugin"),
        WebcamProviderPlugin=_mixin("WebcamProviderPlugin"),
    )

    # SettingsPlugin.on_settings_save stub used by on_settings_save()
    op_plugin.SettingsPlugin.on_settings_save = (  # type: ignore[attr-defined]
        staticmethod(lambda *_: None)
    )

    # octoprint.access.permissions -------------------------------------------
    class _Perm:
        def can(self) -> bool:
            """Stub permission check that always grants access."""
            return True

    class _Perms:
        SETTINGS = _Perm()
        ADMIN = _Perm()

    op_access_permissions = types.SimpleNamespace(Permissions=_Perms)

    # octoprint.schema.webcam ------------------------------------------------
    class _RatioEnum(str, Enum):
        # pylint: disable=invalid-name
        sixteen_nine = "16:9"
        four_three = "4:3"

    class _WebcamCompatibility:
        def __init__(self, **kwargs: object) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    class _Webcam:
        def __init__(self, **kwargs: object) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    op_schema_webcam = types.SimpleNamespace(
        RatioEnum=_RatioEnum,
        WebcamCompatibility=_WebcamCompatibility,
        Webcam=_Webcam,
    )

    # octoprint.webcams ------------------------------------------------------
    class _WebcamNotAbleToTakeSnapshotException(Exception):
        pass

    op_webcams = types.SimpleNamespace(
        WebcamNotAbleToTakeSnapshotException=(
            _WebcamNotAbleToTakeSnapshotException
        ),
    )

    # octoprint (top-level) --------------------------------------------------
    op = types.SimpleNamespace(plugin=op_plugin)

    # Register all stubs in sys.modules so imports resolve them
    stubs: dict = {
        "octoprint": op,
        "octoprint.plugin": op_plugin,
        "octoprint.access": types.SimpleNamespace(),
        "octoprint.access.permissions": op_access_permissions,
        "octoprint.schema": types.SimpleNamespace(),
        "octoprint.schema.webcam": op_schema_webcam,
        "octoprint.webcams": op_webcams,
    }
    for mod_name, stub in stubs.items():
        sys.modules.setdefault(mod_name, stub)  # type: ignore[arg-type]


_make_octoprint_stubs()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def logger() -> logging.Logger:
    """Return a Logger scoped to the test suite."""
    return logging.getLogger("test.bambucam")


@pytest.fixture()
def manager(logger: logging.Logger) -> WebcamdManager:
    """Return a bare WebcamdManager with no callback."""
    return WebcamdManager(logger)


@pytest.fixture()
def manager_with_callback(
    logger: logging.Logger,
) -> tuple:
    """Return (WebcamdManager, callback mock) for state-change tests."""
    cb = MagicMock()
    return WebcamdManager(logger, on_state_change=cb), cb


@pytest.fixture()
def valid_config() -> dict:
    """Return a fully-populated valid daemon configuration."""
    return {
        "hostname": "192.168.1.10",
        "access_code": "12345678",
        "port": 18181,
        "bind_address": "127.0.0.1",
        "override_resolution": True,
        "width": 1920,
        "height": 1080,
        "rotate": -1,
        "encodewait": 0.5,
        "flashred": False,
        "showfps": False,
        "loghttp": False,
        "autorestart": True,
        "max_restarts": 5,
        "restart_window": 300,
    }


@pytest.fixture()
def plugin():
    """BambucamPlugin with all OctoPrint injected attributes mocked."""
    from octoprint_bambucam import BambucamPlugin

    p = BambucamPlugin.__new__(BambucamPlugin)
    p._manager = None
    p._webcam_name = "bambucam"
    p._logger = logging.getLogger("test.plugin")
    p._identifier = "bambucam"
    p._plugin_version = "0.0.2"

    settings = MagicMock()
    settings.get_boolean = MagicMock(return_value=True)
    settings.get = MagicMock(return_value="")
    settings.get_int = MagicMock(return_value=8181)
    settings.get_float = MagicMock(return_value=0.5)
    settings.get_plugin_logfile_path = MagicMock(
        return_value="/tmp/bambucam_http.log"  # noqa: S108
    )
    p._settings = settings
    p._plugin_manager = MagicMock()
    return p
