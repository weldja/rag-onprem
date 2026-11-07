"""The backend images must switch off telemetry and hub access for every
library that has it. Runs inside the image via scripts/test.sh."""

import os

import pytest

REQUIRED = {
    "HF_HUB_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "ORT_DISABLE_TELEMETRY": "1",  # onnxruntime (reranker) 1DS telemetry
    "DO_NOT_TRACK": "1",
}


@pytest.mark.skipif(
    not os.path.exists("/.dockerenv"), reason="checks the container image environment"
)
@pytest.mark.parametrize(("name", "value"), sorted(REQUIRED.items()))
def test_image_disables_telemetry(name: str, value: str) -> None:
    assert os.environ.get(name) == value
