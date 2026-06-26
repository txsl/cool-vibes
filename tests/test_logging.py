"""File-logging setup test."""
import logging
import os
import daikin_server as d


def test_setup_file_logging_creates_dir_and_writes(tmp_path):
    path = str(tmp_path / "nested" / "daikin.log")
    handler = d.setup_file_logging(path)
    logger = logging.getLogger("daikin")
    old_level = logger.level
    logger.setLevel(logging.INFO)   # independent of pytest's root-level capture
    try:
        assert os.path.isdir(os.path.dirname(path))           # parent dir created
        assert handler in logging.getLogger().handlers        # attached to root
        logger.info("hello-logging-test")
        handler.flush()
        assert os.path.exists(path)
        assert "hello-logging-test" in open(path).read()
    finally:
        logger.setLevel(old_level)
        logging.getLogger().removeHandler(handler)
        handler.close()
