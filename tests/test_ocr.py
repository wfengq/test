from pathlib import Path

import httpx
import pytest

from attention_budget.ocr import image_text
from tests.fakes import FakeOcr

FIX = Path(__file__).parent / "fixtures"


def test_concatenates_in_order_and_caps_images():
    engine = FakeOcr({b"a": ["第一行", "第二行"], b"b": ["第三行"]})
    out = image_text(["u1", "u2", "u3"], max_images=2,
                     fetch={"u1": b"a", "u2": b"b", "u3": b"c"}.__getitem__, engine=engine)
    assert out == "[图 1]\n第一行\n第二行\n[图 2]\n第三行" and engine.calls == 2


def test_all_failures_marked():
    def boom(url):
        raise httpx.ConnectError("x")

    assert image_text(["u1"], fetch=boom, engine=FakeOcr({})) == "图片文字未识别"


def test_no_images_returns_empty():
    assert image_text([], engine=FakeOcr({})) == ""


def test_image_without_text_is_skipped_not_failed():
    engine = FakeOcr({b"a": ["有字"]})
    out = image_text(["u1", "u2"], fetch={"u1": b"blank", "u2": b"a"}.__getitem__, engine=engine)
    assert out == "[图 2]\n有字"


def test_real_engine_reads_fixture():
    pytest.importorskip("rapidocr_onnxruntime")
    png = (FIX / "ocr_sample.png").read_bytes()
    assert "注意力" in image_text(["f"], fetch=lambda u: png)
