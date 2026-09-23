import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from backend.web.account_exports import build_account_auth_archive, build_sso_archive, read_sso_token
from backend.web import application
from backend.web.application import (
    MAX_BATCH_ACCOUNT_IDS,
    _batch_account_ids,
    _account_has_sso,
    _find_account_auth_file,
    _find_account_sso_file,
    _load_account_auth_json,
    _stream_file,
)


class WebAuthJsonTests(unittest.TestCase):
    def test_batch_ids_validate_deduplicate_and_preserve_order(self):
        self.assertEqual(_batch_account_ids([3, 1, 3, 2]), [3, 1, 2])
        with self.assertRaisesRegex(HTTPException, "正整数"):
            _batch_account_ids([0])
        with self.assertRaisesRegex(HTTPException, f"最多操作 {MAX_BATCH_ACCOUNT_IDS}"):
            _batch_account_ids(list(range(1, MAX_BATCH_ACCOUNT_IDS + 2)))

    def test_loads_cpa_and_grok2api_json_from_configured_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cpa_dir = root / "cpa"
            g2a_dir = root / "g2a"
            cpa_dir.mkdir()
            g2a_dir.mkdir()
            cpa_path = cpa_dir / "xai-user@outlook.com.json"
            g2a_path = g2a_dir / "g2a-user@outlook.com.json"
            cpa_path.write_text(json.dumps({"type": "xai", "email": "user@outlook.com"}), encoding="utf-8")
            g2a_path.write_text(json.dumps({"accounts": [{"email": "user@outlook.com"}]}), encoding="utf-8")
            record = {
                "email": "user@outlook.com",
                "auth_info": (
                    "CPA 本地: /stale/root/xai-user@outlook.com.json\n"
                    "Grok2API: /stale/root/g2a-user@outlook.com.json"
                ),
            }
            config = {
                "cpa_auth_dir": str(cpa_dir),
                "grok2api_auth_dir": str(g2a_dir),
            }

            cpa = _load_account_auth_json(record, config, "cpa")
            g2a = _load_account_auth_json(record, config, "grok2api")
            self.assertEqual(Path(cpa["path"]), cpa_path)
            self.assertEqual(Path(g2a["path"]), g2a_path)
            self.assertEqual(json.loads(cpa["content"])["email"], "user@outlook.com")
            self.assertEqual(json.loads(g2a["content"])["accounts"][0]["email"], "user@outlook.com")
            self.assertEqual(_find_account_auth_file(record, config, "cpa"), cpa_path)

    def test_rejects_unknown_kind(self):
        with self.assertRaises(ValueError):
            _load_account_auth_json({}, {}, "other")

    def test_file_finder_does_not_parse_content_for_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            invalid_json = root / "xai-user@example.com.json"
            invalid_json.write_text("not parsed by download path", encoding="utf-8")
            record = {"email": "user@example.com", "cpa_auth_path": str(invalid_json)}
            config = {"cpa_auth_dir": str(root)}

            self.assertEqual(_find_account_auth_file(record, config, "cpa"), invalid_json)
            with self.assertRaises(ValueError):
                _load_account_auth_json(record, config, "cpa")

    def test_stream_file_uses_incremental_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_bytes(b"abcdefghij")
            self.assertEqual(list(_stream_file(path, chunk_size=4)), [b"abcd", b"efgh", b"ij"])

    def test_batch_archive_exports_available_files_and_reports_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            auth_file = root / "xai-user@example.com.json"
            auth_file.write_text(json.dumps({"email": "user@example.com"}), encoding="utf-8")
            records = [
                {"id": 7, "email": "user@example.com", "cpa_auth_path": str(auth_file)},
                {"id": 8, "email": "missing@example.com"},
            ]

            content, exported, skipped = build_account_auth_archive(
                records,
                {"cpa_auth_dir": str(root)},
                "cpa",
                _find_account_auth_file,
            )

            self.assertEqual((exported, skipped), (1, 1))
            with zipfile.ZipFile(BytesIO(content)) as archive:
                self.assertEqual(archive.namelist(), ["7-xai-user@example.com.json"])
                payload = json.loads(archive.read(archive.namelist()[0]))
                self.assertEqual(payload["email"], "user@example.com")

    def test_sso_export_only_contains_token_segment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            account_file = root / "user@example.com.txt"
            token = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.fixture.signature"
            account_file.write_text(f"user@example.com----password-value----{token}\n", encoding="utf-8")
            self.assertEqual(read_sso_token(account_file), token)
            content, exported, skipped = build_sso_archive(
                [{"id": 9, "email": "user@example.com", "account_file": str(account_file)}],
                lambda record: Path(str(record["account_file"])),
            )
            self.assertEqual((exported, skipped), (1, 0))
            with zipfile.ZipFile(BytesIO(content)) as archive:
                self.assertEqual(archive.namelist(), ["9-user@example.com.sso.txt"])
                self.assertEqual(archive.read(archive.namelist()[0]).decode().strip(), token)

    def test_sso_file_finder_stays_inside_data_accounts(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(application, "DATA_DIR", Path(tmp)):
            root = Path(tmp) / "accounts"
            root.mkdir()
            account_file = root / "user@example.com.txt"
            account_file.write_text("email----password----token", encoding="utf-8")
            self.assertEqual(
                _find_account_sso_file({"email": "user@example.com", "account_file": str(account_file)}),
                account_file.resolve(),
            )
            outside = Path(tmp) / "outside.txt"
            outside.write_text("email----password----token", encoding="utf-8")
            with self.assertRaises(FileNotFoundError):
                _find_account_sso_file({"account_file": str(outside)})

    def test_account_has_sso_requires_a_parseable_token(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(application, "DATA_DIR", Path(tmp)):
            root = Path(tmp) / "accounts"
            root.mkdir()
            valid = root / "valid@example.com.txt"
            invalid = root / "invalid@example.com.txt"
            valid.write_text("valid@example.com----password----token", encoding="utf-8")
            invalid.write_text("invalid@example.com----password----", encoding="utf-8")

            self.assertTrue(_account_has_sso({"account_file": str(valid)}))
            self.assertFalse(_account_has_sso({"account_file": str(invalid)}))
            self.assertFalse(_account_has_sso({"account_file": str(root / "missing.txt")}))

    def test_actionable_auth_export_ids_only_include_matching_available_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            available = root / "xai-user@example.com.json"
            available.write_text("{}", encoding="utf-8")
            store = mock.Mock()
            store.list_result_ids.return_value = [1, 2]
            store.get_results_by_ids.return_value = [
                {"id": 1, "email": "user@example.com", "cpa_auth_path": str(available)},
                {"id": 2, "email": "missing@example.com"},
            ]
            gr = mock.Mock()
            gr.config = {"cpa_auth_dir": str(root)}
            gr.get_registration_repository.return_value = store
            endpoint = next(
                route.endpoint
                for route in application.create_app().routes
                if route.path == "/api/accounts/actionable-ids"
            )

            with mock.patch.object(application, "_gr", return_value=gr):
                result = endpoint(action="auth_export", q="user", bot_risk="", kind="cpa")

            self.assertEqual(result, {"ok": True, "ids": [1], "total": 1})
            store.list_result_ids.assert_called_once_with(keyword="user")


if __name__ == "__main__":
    unittest.main()
