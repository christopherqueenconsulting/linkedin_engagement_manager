"""`create_ppt` writes its deck to the writable assets volume, never the source tree (#2305).

The prod image's `/app/src` is read-only, so the old `utilities/generated_designs/` save raised
`OSError: [Errno 30] Read-only file system` on every carousel the content plan built.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

import cqc_lem
from cqc_lem.utilities import carousel_creator as cc


def _render(tmp_assets: str, **kwargs) -> tuple[str, MagicMock]:
    prs = MagicMock()
    with patch.object(cqc_lem, "assets_dir", tmp_assets), \
            patch.object(cc, "Presentation", return_value=prs), \
            patch.object(cc, "convert_ppt_theme_colors") as theme:
        path = cc.create_ppt("carousel_128", MagicMock(), **kwargs)
    assert theme.call_args.args[0] == path
    return path, prs


@pytest.mark.unit
class TestCreatePptOutputDir:
    def test_the_deck_is_saved_under_assets_dir_not_beside_the_module(self, tmp_path):
        path, prs = _render(str(tmp_path))

        assert path == os.path.join(str(tmp_path), "generated_designs", "carousel_128.pptx")
        assert os.path.isdir(os.path.join(str(tmp_path), "generated_designs"))
        prs.save.assert_called_once_with(path)
        module_dir = os.path.dirname(os.path.abspath(cc.__file__))
        assert not path.startswith(module_dir)

    def test_an_explicit_output_dir_wins(self, tmp_path):
        out = tmp_path / "decks"
        path, prs = _render(str(tmp_path / "assets"), output_dir=str(out))

        assert path == os.path.join(str(out), "carousel_128.pptx")
        assert out.is_dir()
        prs.save.assert_called_once_with(path)
