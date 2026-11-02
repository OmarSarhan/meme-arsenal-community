from __future__ import annotations

import logging

from meme_arsenal.logging import RedactShareTokenFilter, uvicorn_log_config


def test_uvicorn_access_log_redacts_public_share_tokens():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:1234", "GET", "/share/secret-token/media?v=2", "1.1", 200),
        exc_info=None,
    )

    assert RedactShareTokenFilter().filter(record) is True
    assert "/share/[redacted]/media?v=2" in record.getMessage()
    assert "secret-token" not in record.getMessage()
    assert "redact_share_tokens" in uvicorn_log_config()["handlers"]["access"]["filters"]
