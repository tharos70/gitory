"""
InMemoryGit — a fully in-memory, content-addressable Git-like version control
system implemented in pure Python.

Mirrors real Git's core object model (blobs, trees, commits) and porcelain
commands (add, commit, branch, checkout, log) without ever touching disk.
"""

import hashlib
import time
from collections import defaultdict


class InMemoryGit:
    def __init__(self):
        # Maps SHA-1 hex strings to raw object data (bytes)
        self.objects = {}

        # Maps ref names (e.g. 'refs/heads/main') to SHA-1 hashes
        self.refs = {'refs/heads/main': None}

        # Pointer to the current active branch (symbolic) or a detached commit
        self.HEAD = 'ref: refs/heads/main'

        # The staging area: maps file paths to their blob SHA-1 hashes
        self.index = {}

        # Simulates a working directory: filepath -> file content (str)
        self.working_directory = {}

    # ------------------------------------------------------------------
    # Phase 1: Object database & hashing
    # ------------------------------------------------------------------
    def hash_object(self, data, obj_type="blob"):
        """Hash and store an object exactly as Git does: "type len\\0data"."""
        if isinstance(data, str):
            data = data.encode('utf-8')

        header = f"{obj_type} {len(data)}\0".encode('utf-8')
        full_data = header + data

        sha1 = hashlib.sha1(full_data).hexdigest()
        self.objects[sha1] = full_data
        return sha1

    def _read_object(self, sha1):
        """Return (obj_type, content_bytes) for a stored object."""
        if sha1 not in self.objects:
            raise KeyError(f"Object {sha1} not found")
        raw = self.objects[sha1]
        header, _, content = raw.partition(b'\0')
        obj_type, _ = header.decode('utf-8').split(' ')
        return obj_type, content

    # ------------------------------------------------------------------
    # Phase 2: Index / staging (git add)
    # ------------------------------------------------------------------
    def add(self, filepath, content=None):
        """Stage a file. If content is omitted, read from working_directory."""
        if content is None:
            if filepath not in self.working_directory:
                raise FileNotFoundError(f"{filepath} not in working directory")
            content = self.working_directory[filepath]
        else:
            self.working_directory[filepath] = content

        blob_hash = self.hash_object(content, "blob")
        self.index[filepath] = blob_hash
        return blob_hash

    # ------------------------------------------------------------------
    # Phase 3: Trees & snapshots (git write-tree)
    # ------------------------------------------------------------------
    def write_tree(self):
        """Build a (possibly nested) tree object from the staging area."""
        return self._write_tree_from_paths(self.index)

    def _write_tree_from_paths(self, path_map):
        """
        path_map: dict of filepath -> blob_hash (flat, may contain '/').
        Recursively builds tree objects for subdirectories.
        """
        # Group entries by their top-level component
        top_level_files = {}          # name -> blob_hash
        subdirs = defaultdict(dict)   # dirname -> {rest_of_path: blob_hash}

        for path, blob_hash in path_map.items():
            if '/' in path:
                dirname, rest = path.split('/', 1)
                subdirs[dirname][rest] = blob_hash
            else:
                top_level_files[path] = blob_hash

        entries = []  # (mode, type, hash, name) for deterministic ordering

        for name, blob_hash in top_level_files.items():
            entries.append(("100644", "blob", blob_hash, name))

        for dirname, nested_paths in subdirs.items():
            subtree_hash = self._write_tree_from_paths(nested_paths)
            entries.append(("40000", "tree", subtree_hash, dirname))

        # Git sorts tree entries by name for a canonical, content-addressable hash
        entries.sort(key=lambda e: e[3])

        tree_data = "".join(
            f"{mode} {obj_type} {obj_hash}\t{name}\n"
            for mode, obj_type, obj_hash, name in entries
        )
        return self.hash_object(tree_data, "tree")

    def _read_tree(self, tree_hash, prefix=""):
        """Flatten a tree object back into {filepath: blob_hash}, recursively."""
        obj_type, content = self._read_object(tree_hash)
        assert obj_type == "tree"

        result = {}
        for line in content.decode('utf-8').splitlines():
            if not line:
                continue
            meta, name = line.split('\t')
            mode, entry_type, entry_hash = meta.split(' ')
            full_path = f"{prefix}{name}"
            if entry_type == "blob":
                result[full_path] = entry_hash
            elif entry_type == "tree":
                result.update(self._read_tree(entry_hash, prefix=f"{full_path}/"))
        return result

    # ------------------------------------------------------------------
    # Phase 4: Committing (git commit)
    # ------------------------------------------------------------------
    def _resolve_ref(self, ref_or_hash):
        """Resolve HEAD/branch names down to a raw commit hash (or None)."""
        if ref_or_hash is None:
            return None
        if ref_or_hash.startswith('ref: '):
            target_ref = ref_or_hash[len('ref: '):]
            return self.refs.get(target_ref)
        if ref_or_hash in self.refs:
            return self.refs[ref_or_hash]
        return ref_or_hash  # assume it's already a raw commit hash

    def _current_branch(self):
        if self.HEAD.startswith('ref: '):
            return self.HEAD[len('ref: '):]
        return None  # detached HEAD

    def commit(self, message, author="Anonymous <anon@example.com>"):
        tree_hash = self.write_tree()
        parent_hash = self._resolve_ref(self.HEAD)

        timestamp = int(time.time())
        lines = [f"tree {tree_hash}"]
        if parent_hash:
            lines.append(f"parent {parent_hash}")
        lines.append(f"author {author} {timestamp}")
        lines.append(f"committer {author} {timestamp}")
        lines.append("")
        lines.append(message)
        commit_data = "\n".join(lines)

        commit_hash = self.hash_object(commit_data, "commit")

        branch = self._current_branch()
        if branch is not None:
            self.refs[branch] = commit_hash
        else:
            self.HEAD = commit_hash  # detached HEAD moves directly

        return commit_hash

    # ------------------------------------------------------------------
    # Phase 5: Branching and checkout
    # ------------------------------------------------------------------
    def branch(self, branch_name):
        current_commit = self._resolve_ref(self.HEAD)
        self.refs[f'refs/heads/{branch_name}'] = current_commit
        return f'refs/heads/{branch_name}'

    def checkout(self, target):
        ref_name = f'refs/heads/{target}'

        if ref_name in self.refs:
            self.HEAD = f'ref: {ref_name}'
            commit_hash = self.refs[ref_name]
        elif target in self.objects:
            # Detached HEAD onto a raw commit hash
            self.HEAD = target
            commit_hash = target
        else:
            raise ValueError(f"Unknown branch or commit: {target}")

        if commit_hash is None:
            # Branch has no commits yet (e.g. brand-new empty repo)
            self.index = {}
            self.working_directory = {}
            return

        obj_type, content = self._read_object(commit_hash)
        assert obj_type == "commit"
        tree_hash = content.decode('utf-8').splitlines()[0].split(' ')[1]

        flat_tree = self._read_tree(tree_hash)
        self.index = dict(flat_tree)
        self.working_directory = {
            path: self._read_object(blob_hash)[1].decode('utf-8')
            for path, blob_hash in flat_tree.items()
        }

    # ------------------------------------------------------------------
    # Phase 6: History (git log)
    # ------------------------------------------------------------------
    def log(self, verbose=True):
        current_hash = self._resolve_ref(self.HEAD)
        history = []

        while current_hash:
            obj_type, content = self._read_object(current_hash)
            text = content.decode('utf-8')
            lines = text.splitlines()

            parent_hash = None
            author_line = ""
            message_lines = []
            in_message = False

            for line in lines:
                if line.startswith("parent "):
                    parent_hash = line.split(' ')[1]
                elif line.startswith("author "):
                    author_line = line[len("author "):]
                elif line == "" and not in_message:
                    in_message = True
                elif in_message:
                    message_lines.append(line)

            entry = {
                "hash": current_hash,
                "author": author_line,
                "message": "\n".join(message_lines),
            }
            history.append(entry)

            if verbose:
                print(f"commit {current_hash}")
                print(f"Author: {author_line}")
                print(f"\n    {entry['message']}\n")

            current_hash = parent_hash

        return history


# ----------------------------------------------------------------------
# Demo / self-test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    repo = InMemoryGit()

    # First commit
    repo.add("README.md", "# My Project\nHello world.")
    repo.add("src/main.py", "print('hello')")
    c1 = repo.commit("Initial commit", author="Ada <ada@example.com>")
    print(f"Created commit: {c1}\n")

    # Branch and second commit
    repo.branch("feature")
    repo.checkout("feature")
    repo.add("src/main.py", "print('hello, world!')")
    repo.add("src/utils/helpers.py", "def helper(): pass")
    c2 = repo.commit("Add greeting + helper module", author="Ada <ada@example.com>")
    print(f"Created commit: {c2}\n")

    print("=== Log on 'feature' ===")
    repo.log()

    # Switch back to main — working directory should revert
    repo.checkout("main")
    print("=== Working directory on 'main' ===")
    for path, content in repo.working_directory.items():
        print(f"{path}: {content!r}")

    print("\n=== Working directory on 'feature' ===")
    repo.checkout("feature")
    for path, content in repo.working_directory.items():
        print(f"{path}: {content!r}")

    # Sanity checks
    assert "src/utils/helpers.py" in repo.working_directory
    assert repo.working_directory["src/main.py"] == "print('hello, world!')"
    repo.checkout("main")
    assert "src/utils/helpers.py" not in repo.working_directory
    assert repo.working_directory["src/main.py"] == "print('hello')"
    print("\nAll sanity checks passed.")
