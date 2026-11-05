import unittest
from unittest import mock

from docker import install_camoufox


class CamoufoxDockerInstallTests(unittest.TestCase):
    def test_resolve_release_asset_supports_linux_x86_64_and_arm64(self):
        x64 = install_camoufox.resolve_pinned_asset("x86_64")
        arm64 = install_camoufox.resolve_pinned_asset("arm64")

        self.assertEqual(x64["version"], "152.0.4")
        self.assertEqual(x64["build"], "beta.29")
        self.assertIn("lin.x86_64.zip", x64["url"])
        self.assertEqual(
            x64["sha256"],
            "1bea4b55a51c88e82dc7d426d9c75093d942d2afc8c911cb8fc78ebf723d686c",
        )

        self.assertEqual(arm64["version"], "152.0.4")
        self.assertEqual(arm64["build"], "beta.29")
        self.assertIn("lin.arm64.zip", arm64["url"])
        self.assertEqual(
            arm64["sha256"],
            "c8844c03f3c233d6a466d59fd3f4e6f21470bd685e1b7cc82d650379a9b23e35",
        )

    def test_install_pinned_camoufox_does_not_require_github_api_sync(self):
        fake_installed = mock.MagicMock()
        fake_installed.relative_path = "browsers/official/152.0.4-beta.29-c8844c03"

        with (
            mock.patch.object(install_camoufox, "RepoConfig") as repo_config_cls,
            mock.patch.object(install_camoufox, "CamoufoxUpdate") as updater_cls,
            mock.patch.object(install_camoufox, "save_repo_cache") as save_repo_cache,
            mock.patch.object(install_camoufox, "list_installed", return_value=[fake_installed]),
            mock.patch.object(install_camoufox, "find_install", return_value=fake_installed),
            mock.patch.object(install_camoufox, "_set_pinned") as set_pinned,
            mock.patch.object(install_camoufox, "ALLOW_GEOIP", True),
            mock.patch.object(install_camoufox, "download_mmdb") as download_mmdb,
            mock.patch.object(install_camoufox, "maybe_download_addons") as download_addons,
            mock.patch.object(install_camoufox, "installed_verstr", return_value="152.0.4-beta.29"),
            mock.patch.object(
                install_camoufox,
                "launch_path",
                return_value="/opt/camoufox-cache/browsers/official/152.0.4-beta.29-c8844c03/camoufox-bin",
            ),
        ):
            repo_cfg = mock.MagicMock()
            repo_cfg.name = "Official"
            repo_cfg.repo = "daijro/camoufox"
            repo_cfg.repos = ["daijro/camoufox", "camoufox/camoufox"]
            repo_cfg.get_arch.return_value = "arm64"
            repo_config_cls.find_by_name.return_value = repo_cfg

            install_camoufox.install_pinned_camoufox()

            updater_cls.assert_called_once()
            updater_cls.return_value.update.assert_called_once_with()
            save_repo_cache.assert_called_once()
            set_pinned.assert_called_once()
            download_mmdb.assert_called_once_with()
            download_addons.assert_called_once()

    def test_install_pinned_camoufox_falls_back_to_mirror_repo_on_download_error(self):
        fake_installed = mock.MagicMock()
        fake_installed.relative_path = "browsers/official/152.0.4-beta.29-c8844c03"
        attempted_urls = []

        def fake_updater_factory(repo_config=None, selected_version=None):
            attempted_urls.append(selected_version.url)
            updater = mock.MagicMock()
            if len(attempted_urls) == 1:
                updater.update.side_effect = RuntimeError("connection reset")
            return updater

        with (
            mock.patch.object(install_camoufox, "RepoConfig") as repo_config_cls,
            mock.patch.object(install_camoufox, "CamoufoxUpdate", side_effect=fake_updater_factory),
            mock.patch.object(install_camoufox, "save_repo_cache"),
            mock.patch.object(install_camoufox, "list_installed", return_value=[fake_installed]),
            mock.patch.object(install_camoufox, "find_install", return_value=fake_installed),
            mock.patch.object(install_camoufox, "_set_pinned"),
            mock.patch.object(install_camoufox, "ALLOW_GEOIP", False),
            mock.patch.object(install_camoufox, "maybe_download_addons"),
            mock.patch.object(install_camoufox, "installed_verstr", return_value="152.0.4-beta.29"),
            mock.patch.object(
                install_camoufox,
                "launch_path",
                return_value="/opt/camoufox-cache/browsers/official/152.0.4-beta.29-c8844c03/camoufox-bin",
            ),
            mock.patch.object(install_camoufox.time, "sleep"),
        ):
            repo_cfg = mock.MagicMock()
            repo_cfg.name = "Official"
            repo_cfg.repo = "daijro/camoufox"
            repo_cfg.repos = ["daijro/camoufox", "camoufox/camoufox"]
            repo_cfg.get_arch.return_value = "arm64"
            repo_config_cls.find_by_name.return_value = repo_cfg

            install_camoufox.install_pinned_camoufox()

            self.assertEqual(len(attempted_urls), 2)
            self.assertIn("daijro/camoufox", attempted_urls[0])
            self.assertIn("camoufox/camoufox", attempted_urls[1])


if __name__ == "__main__":
    unittest.main()
