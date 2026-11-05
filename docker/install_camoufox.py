"""Install and pin the baked Camoufox browser without hitting GitHub REST API rate limits."""

from __future__ import annotations

import time
from typing import Any, Dict, List

from camoufox.__main__ import CamoufoxUpdate, _set_pinned
from camoufox.addons import DefaultAddons, maybe_download_addons
from camoufox.geolocation import ALLOW_GEOIP, download_mmdb
from camoufox.multiversion import find_install, list_installed, save_repo_cache
from camoufox.pkgman import AvailableVersion, RepoConfig, Version, installed_verstr, launch_path

PINNED_VERSION = "152.0.4"
PINNED_BUILD = "beta.29"
EXPECTED_VERSTR = f"{PINNED_VERSION}-{PINNED_BUILD}"

LINUX_RELEASE_ASSETS: Dict[str, Dict[str, Any]] = {
    "x86_64": {
        "version": PINNED_VERSION,
        "build": PINNED_BUILD,
        "url": (
            f"https://github.com/daijro/camoufox/releases/download/"
            f"v{EXPECTED_VERSTR}/camoufox-{EXPECTED_VERSTR}-lin.x86_64.zip"
        ),
        "is_prerelease": False,
        "asset_id": 522725366,
        "asset_size": 663434141,
        "asset_updated_at": "2026-08-20T19:57:00Z",
        "sha256": "1bea4b55a51c88e82dc7d426d9c75093d942d2afc8c911cb8fc78ebf723d686c",
        "created_at": "2026-08-20T19:56:44Z",
    },
    "arm64": {
        "version": PINNED_VERSION,
        "build": PINNED_BUILD,
        "url": (
            f"https://github.com/daijro/camoufox/releases/download/"
            f"v{EXPECTED_VERSTR}/camoufox-{EXPECTED_VERSTR}-lin.arm64.zip"
        ),
        "is_prerelease": False,
        "asset_id": 522725372,
        "asset_size": 653821558,
        "asset_updated_at": "2026-08-20T19:57:00Z",
        "sha256": "c8844c03f3c233d6a466d59fd3f4e6f21470bd685e1b7cc82d650379a9b23e35",
        "created_at": "2026-08-20T19:56:44Z",
    },
}


def resolve_pinned_asset(arch: str) -> Dict[str, Any]:
    if arch not in LINUX_RELEASE_ASSETS:
        raise RuntimeError(f"Unsupported Linux architecture for pinned Camoufox: {arch}")
    return dict(LINUX_RELEASE_ASSETS[arch])


def _candidate_urls(repos: List[str], arch: str) -> List[str]:
    filename = f"camoufox-{EXPECTED_VERSTR}-lin.{arch}.zip"
    urls = [
        f"https://github.com/{repo}/releases/download/v{EXPECTED_VERSTR}/{filename}"
        for repo in repos
        if repo
    ]
    return urls or [resolve_pinned_asset(arch)["url"]]


def install_pinned_camoufox(max_attempts_per_repo: int = 2) -> None:
    repo_config = RepoConfig.find_by_name("Official") or RepoConfig.get_default()
    arch = repo_config.get_arch()
    ver_data = resolve_pinned_asset(arch)

    save_repo_cache(
        {
            "repos": [
                {
                    "name": repo_config.name,
                    "repo": repo_config.repo,
                    "versions": [ver_data],
                }
            ],
            "spoof_os": None,
            "spoof_arch": None,
        }
    )

    candidate_urls = _candidate_urls(list(getattr(repo_config, "repos", []) or []), arch)
    last_error: Exception | None = None
    for attempt in range(1, max_attempts_per_repo + 1):
        for url in candidate_urls:
            selected = AvailableVersion(
                version=Version(build=ver_data["build"], version=ver_data["version"]),
                url=url,
                is_prerelease=bool(ver_data.get("is_prerelease", False)),
                asset_id=ver_data.get("asset_id"),
                asset_size=ver_data.get("asset_size"),
                asset_updated_at=ver_data.get("asset_updated_at"),
                sha256=ver_data.get("sha256"),
                asset_created_at=ver_data.get("created_at"),
            )
            try:
                CamoufoxUpdate(repo_config=repo_config, selected_version=selected).update()
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(attempt * 2)
        if last_error is None:
            break

    if last_error is not None:
        raise last_error

    installed = list_installed()
    inst = find_install(EXPECTED_VERSTR, ver_data.get("sha256"), installed, 1)
    if inst is None:
        raise RuntimeError(f"Camoufox {EXPECTED_VERSTR} was not found after installation")

    _set_pinned(repo_config.name, "stable", ver_data, inst, sha=ver_data.get("sha256"))

    if ALLOW_GEOIP:
        download_mmdb()
    maybe_download_addons(list(DefaultAddons))

    ver = installed_verstr()
    if ver != EXPECTED_VERSTR:
        raise AssertionError(f"expected {EXPECTED_VERSTR}, got {ver}")
    print(f"Camoufox installed: {ver} ({launch_path()})")


if __name__ == "__main__":
    install_pinned_camoufox()
