import ast
import asyncio
import re
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
METHOD_NAMES = {
    "_find_attachment_entry",
    "_resolve_wake_image_ref",
    "_cleanup_wake_snapshots",
    "_reserve_wake_signature",
    "_release_wake_signature",
    "_commit_wake_signature",
}
source_tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
plugin_class = next(
    node
    for node in source_tree.body
    if isinstance(node, ast.ClassDef) and node.name == "GossipSharer"
)
test_class = ast.ClassDef(
    name="TestableGossipSharer",
    bases=[],
    keywords=[],
    decorator_list=[],
    body=[
        node
        for node in plugin_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in METHOD_NAMES
    ],
)
module = ast.fix_missing_locations(ast.Module(body=[test_class], type_ignores=[]))


class _Logger:
    def info(self, *_args, **_kwargs):
        pass

    def warning(self, *_args, **_kwargs):
        pass


def _is_file_uri(value: str) -> bool:
    return value.startswith("file://")


def _file_uri_to_path(value: str) -> str:
    return value[7:] if value.startswith("file://") else value


namespace = {
    "asyncio": asyncio,
    "Path": Path,
    "re": re,
    "time": time,
    "logger": _Logger(),
    "is_file_uri": _is_file_uri,
    "file_uri_to_path": _file_uri_to_path,
    "WAKE_DEDUP_WINDOW_SECONDS": 30,
}
exec(compile(module, str(PLUGIN_PATH), "exec"), namespace)
GossipSharer = namespace["TestableGossipSharer"]


class WakeImageRefTests(unittest.TestCase):
    def setUp(self):
        self.plugin = GossipSharer.__new__(GossipSharer)

    @staticmethod
    def _entry(entry_id: str, raw_ref: str, *aliases: str) -> dict:
        return {
            "id": entry_id,
            "kind": "image",
            "raw_ref": raw_ref,
            "aliases": {entry_id, raw_ref, *aliases},
            "component": object(),
            "name": Path(raw_ref).name,
        }

    def test_short_ref_and_exact_alias_are_canonicalized(self):
        entry = self._entry("image_1", "/tmp/current.jpg", "current.jpg")
        registry = {"image_1": entry}

        self.assertEqual(
            self.plugin._resolve_wake_image_ref(registry, "image_1"),
            ("image_1", entry),
        )
        self.assertEqual(
            self.plugin._resolve_wake_image_ref(registry, "current.jpg"),
            ("image_1", entry),
        )

    def test_stale_media_path_is_not_mapped_to_only_image(self):
        entry = self._entry("image_1", "/tmp/current.jpg")
        registry = {"image_1": entry}

        with self.assertRaisesRegex(ValueError, "无法精确映射"):
            self.plugin._resolve_wake_image_ref(
                registry, "/root/AstrBot/data/temp/media_image_stale.jpg"
            )

    def test_media_path_requires_unique_exact_match(self):
        ref = "/root/AstrBot/data/temp/media_image_match.jpg"
        entry = self._entry("image_1", ref)
        registry = {"image_1": entry}

        self.assertEqual(
            self.plugin._resolve_wake_image_ref(registry, ref),
            ("image_1", entry),
        )

        duplicate = self._entry("image_2", f"/other/{Path(ref).name}")
        with self.assertRaisesRegex(ValueError, "多个匹配"):
            self.plugin._resolve_wake_image_ref(
                {"image_1": entry, "image_2": duplicate}, ref
            )


class WakeSignatureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = GossipSharer.__new__(GossipSharer)
        self.plugin._wake_signature_lock = asyncio.Lock()
        self.plugin._inflight_wake_signatures = set()
        self.plugin._recent_wake_signatures = {}

    async def test_inflight_then_recent_success(self):
        signature = "requester|session|task"
        self.assertEqual(
            await self.plugin._reserve_wake_signature(signature), "reserved"
        )
        self.assertEqual(
            await self.plugin._reserve_wake_signature(signature), "inflight"
        )

        await self.plugin._commit_wake_signature(signature)
        self.assertEqual(await self.plugin._reserve_wake_signature(signature), "recent")

    async def test_release_allows_immediate_retry(self):
        signature = "requester|session|task"
        self.assertEqual(
            await self.plugin._reserve_wake_signature(signature), "reserved"
        )
        await self.plugin._release_wake_signature(signature)
        self.assertEqual(
            await self.plugin._reserve_wake_signature(signature), "reserved"
        )


class WakeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.plugin = GossipSharer.__new__(GossipSharer)

    def test_cleanup_is_idempotent(self):
        with tempfile.TemporaryDirectory(dir="/root/.codex_claude_temp") as temp_dir:
            snapshot = Path(temp_dir) / "snapshot.bin"
            snapshot.write_bytes(b"payload")
            prepared = {"cleanup_paths": [snapshot]}

            self.plugin._cleanup_wake_snapshots(prepared)
            self.plugin._cleanup_wake_snapshots(prepared)

            self.assertFalse(snapshot.exists())
            self.assertEqual(prepared["cleanup_paths"], [])


if __name__ == "__main__":
    unittest.main()
