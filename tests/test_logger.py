import importlib
import logging


def test_configure_logging_creates_file(tmp_path, monkeypatch):
    log_path = tmp_path / "parser.log"
    monkeypatch.setenv("LOG_FILE_PATH", str(log_path))

    import app.logger as logger_module

    logger_module = importlib.reload(logger_module)
    logger_module.configure_logging("INFO")

    logger = logger_module.get_logger("test.logger")
    logger.info("file sink ready")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_path.exists()
    assert "file sink ready" in log_path.read_text()
