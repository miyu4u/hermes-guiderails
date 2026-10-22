import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from guiderails.engine import InspectionError
from guiderails.host import local_file_context


class HostContextTests(unittest.TestCase):
    def modules(self, config):
        return {
            "hermes_constants": SimpleNamespace(get_subprocess_home=lambda: "/fixture/home"),
            "hermes_cli.config": SimpleNamespace(load_config_readonly=lambda: config),
        }

    def test_public_configuration_does_not_authorize_task_cwd(self):
        config = {"terminal": {"backend": "local", "cwd": "/private/task"}}
        with patch.dict(sys.modules, self.modules(config)), patch.dict(os.environ, {"TERMINAL_ENV": "local"}):
            self.assertEqual(local_file_context(), {"local": True, "base": None, "home": "/fixture/home"})
            for backend in ("ssh", "docker", "unknown"):
                config["terminal"]["backend"] = backend
                self.assertEqual(local_file_context(), {"local": False, "base": None, "home": None})
            config["terminal"]["backend"] = "local"
            with patch.dict(os.environ, {"TERMINAL_ENV": "docker"}):
                self.assertFalse(local_file_context()["local"])

    def test_missing_and_failing_host_configuration_is_content_free(self):
        for config in (None, [], {}, {"terminal": None}, {"terminal": {"backend": None}}):
            with self.subTest(config=config), patch.dict(sys.modules, self.modules(config)):
                with self.assertRaisesRegex(InspectionError, "^file_context_unavailable$"):
                    local_file_context()
        modules = self.modules({"terminal": {"backend": "local"}})
        modules["hermes_cli.config"].load_config_readonly = Mock(side_effect=OSError("private detail"))
        with patch.dict(sys.modules, modules):
            with self.assertRaisesRegex(InspectionError, "^file_context_unavailable$"):
                local_file_context()


if __name__ == "__main__":
    unittest.main()
