import unittest
from unittest.mock import Mock

from plugins.cerry_github import GhError, commit_write, make_diff, prepare_write


class GitHubCommitTests(unittest.TestCase):
    def test_changed_destination_blocks_update_and_delete(self):
        gh = Mock()
        gh.branch_sha.return_value = 'head'
        gh.file_sha.return_value = 'newer-blob'
        with self.assertRaises(GhError):
            prepare_write(gh, 'owner', 'repo', 'main', 'file', 'main', 'opened-blob')

    def test_new_file_cannot_replace_existing_file_on_base(self):
        gh = Mock()
        gh.branch_sha.side_effect = [None, 'base-head']
        gh.file_sha.return_value = 'existing-blob'
        with self.assertRaises(GhError):
            prepare_write(gh, 'owner', 'repo', 'edits', 'file', 'main', None)
        gh.create_branch.assert_not_called()

    def test_new_branch_uses_reviewed_base_after_approval(self):
        gh = Mock()
        gh.branch_sha.side_effect = [None, 'base-head']
        gh.file_sha.return_value = 'blob'
        state = prepare_write(gh, 'owner', 'repo', 'edits', 'file', 'main', 'blob')
        gh.file_sha.assert_called_once_with('owner', 'repo', 'file', 'base-head')
        commit_write(gh, 'owner', 'repo', 'edits', 'main', 'file', b'edit', 'update',
                     state['sha'], create_from=state['create_from'])
        gh.create_branch.assert_called_once_with('owner', 'repo', 'edits', 'base-head')
        gh.put_file.assert_called_once_with('owner', 'repo', 'file', b'edit', 'update', 'edits', 'blob')

    def test_direct_default_branch_requires_opt_in(self):
        gh = Mock()
        args = (gh, 'owner', 'repo', 'main', 'main', 'file', b'edit', 'update', 'blob')
        with self.assertRaises(PermissionError):
            commit_write(*args, default_branch='main')
        gh.put_file.assert_not_called()
        commit_write(*args, default_branch='main', allow_default=True)
        gh.put_file.assert_called_once()
        gh.create_branch.assert_not_called()

    def test_review_includes_final_line_and_newline_change(self):
        text = 'line\n' * 100 + 'last'
        added, removed, diff = make_diff('', text)
        self.assertEqual((added, removed), (101, 0))
        self.assertIn('+last\n\\ No newline', diff)
        self.assertEqual(make_diff('last', 'last\n')[:2], (1, 1))
